"""The frozen agreement: the brief and its acceptance checks, fixed at launch.

`dgx-autonomy launch --brief DIR` sends `DIR/brief.md` and every file under
`DIR/checks/`. The controller writes them once into `runs/<id>/frozen/`, which is
controller-owned. The agent sees it read-only at /brief. The evaluator sees only
`checks/`, read-only. Nothing writes to the directory after launch.

    frozen/brief.md
    frozen/checks/criteria.yaml     which criteria exist, and how each is checked
    frozen/checks/...               the checks themselves (pytest or Playwright files)
    frozen/manifest.json            sha256 of every file, and their digest

The digest covers every file's path and content. The CLI computes it from what it
read, and the controller recomputes it from what it wrote; a mismatch refuses the
launch. The controller records the digest on the run and checks it again before
the sandbox is created and before every evaluation.

criteria.yaml:

    criteria:
      - key: home-page                 # [a-z0-9-], unique
        description: The home page greets the visitor
        kind: automated                # or human_judgment
        test: home.spec.ts             # Playwright (*.spec.ts, ...) or pytest (test_*.py)
        required: true                 # the default; optional checks never block completion
      - key: tidy-on-phones
        description: The layout looks tidy on a phone
        kind: human_judgment           # reported as awaiting judgment, never as a pass

This module is standard library + PyYAML only: the CLI imports it too.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

BRIEF_NAME = "brief.md"
CHECKS_DIR = "checks"
CRITERIA_FILE = "criteria.yaml"
MANIFEST_NAME = "manifest.json"
MAX_BRIEF_BYTES = 256 * 1024
MAX_CHECK_FILES = 200
MAX_CHECK_FILE_BYTES = 1024 * 1024
MAX_CHECKS_BYTES = 4 * 1024 * 1024
MAX_CRITERIA = 50
# Files the CLI leaves out of a checks directory: caches and editor litter.
SKIPPED_NAMES = frozenset({"__pycache__", "node_modules", ".pytest_cache", ".DS_Store"})

CriterionKind = Literal["automated", "human_judgment"]
Runner = Literal["pytest", "playwright"]

_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PLAYWRIGHT_TEST = re.compile(r"\.(spec|test)\.(c|m)?[jt]sx?$")
_PYTEST_TEST = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")


class FrozenError(ValueError):
    """The brief or checks cannot be frozen, or the frozen copy no longer matches."""


@dataclass(frozen=True)
class Criterion:
    key: str
    description: str
    kind: CriterionKind
    required: bool = True
    # automated only: the check file, relative to checks/, and how it runs.
    test: str | None = None
    runner: Runner | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Frozen:
    root: Path
    digest: str
    criteria: tuple[Criterion, ...]

    @property
    def brief(self) -> Path:
        return self.root / BRIEF_NAME

    @property
    def checks(self) -> Path:
        return self.root / CHECKS_DIR

    @property
    def automated(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.kind == "automated")


def check_path(rel: str) -> str:
    """A path under checks/, as sent by the CLI: relative, plain names, no dot-dot."""
    parts = rel.split("/")
    if not rel or len(parts) > 8 or not all(_PART.match(p) for p in parts):
        raise FrozenError(f"checks/{rel}: not an acceptable path inside checks/")
    return rel


def bundle_digest(brief: bytes, checks: Mapping[str, bytes]) -> str:
    """sha256 over every file's path and content sha256, in path order."""
    entries = [(BRIEF_NAME, hashlib.sha256(brief).hexdigest())]
    entries += [
        (f"{CHECKS_DIR}/{rel}", hashlib.sha256(data).hexdigest())
        for rel, data in sorted(checks.items())
    ]
    canonical = json.dumps(entries, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def runner_for(test: str) -> Runner | None:
    if _PLAYWRIGHT_TEST.search(test):
        return "playwright"
    if _PYTEST_TEST.search(test):
        return "pytest"
    return None


def parse_criteria(checks: Mapping[str, bytes]) -> tuple[Criterion, ...]:
    """The criteria in checks/criteria.yaml. No checks at all means no criteria."""
    if not checks:
        return ()
    raw_file = checks.get(CRITERIA_FILE)
    if raw_file is None:
        raise FrozenError(f"checks/ has files but no {CRITERIA_FILE} listing the criteria")
    try:
        data = yaml.safe_load(raw_file.decode())
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FrozenError(f"checks/{CRITERIA_FILE}: not YAML ({exc})") from None
    items = data.get("criteria") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise FrozenError(f"checks/{CRITERIA_FILE}: expected a non-empty `criteria` list")
    if len(items) > MAX_CRITERIA:
        raise FrozenError(f"checks/{CRITERIA_FILE}: more than {MAX_CRITERIA} criteria")
    out: list[Criterion] = []
    for i, item in enumerate(items):
        out.append(_criterion(item, i, checks))
    keys = [c.key for c in out]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise FrozenError(f"checks/{CRITERIA_FILE}: duplicate keys {', '.join(dupes)}")
    return tuple(out)


def _criterion(item: object, i: int, checks: Mapping[str, bytes]) -> Criterion:
    where = f"checks/{CRITERIA_FILE}, criterion {i + 1}"
    if not isinstance(item, dict):
        raise FrozenError(f"{where}: expected a mapping")
    unknown = set(item) - {"key", "description", "kind", "test", "required"}
    if unknown:
        raise FrozenError(f"{where}: unknown fields {', '.join(sorted(unknown))}")
    key = item.get("key")
    if not isinstance(key, str) or not _KEY.match(key):
        raise FrozenError(f"{where}: key must match [a-z0-9][a-z0-9-]{{0,39}}, got {key!r}")
    where = f"checks/{CRITERIA_FILE}, criterion {key!r}"
    description = item.get("description")
    if not isinstance(description, str) or not description.strip():
        raise FrozenError(f"{where}: needs a description")
    kind = item.get("kind", "automated")
    required = item.get("required", True)
    if not isinstance(required, bool):
        raise FrozenError(f"{where}: required must be true or false")
    test = item.get("test")
    if kind == "human_judgment":
        if test is not None:
            raise FrozenError(f"{where}: a human_judgment criterion has no test")
        return Criterion(key, description.strip(), "human_judgment", required)
    if kind != "automated":
        raise FrozenError(f"{where}: kind must be automated or human_judgment, got {kind!r}")
    if not isinstance(test, str):
        raise FrozenError(f"{where}: an automated criterion names its test file")
    check_path(test)
    if test not in checks:
        raise FrozenError(f"{where}: checks/{test} does not exist")
    runner = runner_for(test)
    if runner is None:
        raise FrozenError(
            f"{where}: {test} is neither a Playwright test (*.spec.ts, *.test.js, ...)"
            " nor a pytest file (test_*.py, *_test.py)"
        )
    return Criterion(key, description.strip(), "automated", required, test, runner)


def validate(brief: bytes, checks: Mapping[str, bytes]) -> tuple[Criterion, ...]:
    if not brief.strip():
        raise FrozenError("the brief is empty")
    if len(brief) > MAX_BRIEF_BYTES:
        raise FrozenError(f"the brief is larger than {MAX_BRIEF_BYTES} bytes")
    if len(checks) > MAX_CHECK_FILES:
        raise FrozenError(f"checks/ has more than {MAX_CHECK_FILES} files")
    total = 0
    for rel, data in checks.items():
        check_path(rel)
        if len(data) > MAX_CHECK_FILE_BYTES:
            raise FrozenError(f"checks/{rel} is larger than {MAX_CHECK_FILE_BYTES} bytes")
        total += len(data)
    if total > MAX_CHECKS_BYTES:
        raise FrozenError(f"checks/ is larger than {MAX_CHECKS_BYTES} bytes in total")
    return parse_criteria(checks)


def freeze(root: Path, brief: bytes, checks: Mapping[str, bytes]) -> Frozen:
    """Write the agreement to `root` (which must not exist yet) and return it.

    Written into a sibling directory and renamed into place, so `root` is either
    absent or complete. Directories 0755 and files 0644: the agent (another uid)
    and the evaluator read them; only the controller (root) could write them.
    """
    criteria = validate(brief, checks)
    if root.exists():
        raise FrozenError(f"{root} already exists; a frozen agreement is written once")
    tmp = root.with_name(f".{root.name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(mode=0o755)
    files = {BRIEF_NAME: brief, **{f"{CHECKS_DIR}/{rel}": d for rel, d in checks.items()}}
    for rel, data in files.items():
        path = tmp / rel
        for parent in reversed(path.relative_to(tmp).parents[:-1]):
            (tmp / parent).mkdir(mode=0o755, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o644)
    (tmp / CHECKS_DIR).mkdir(mode=0o755, exist_ok=True)
    digest = bundle_digest(brief, checks)
    manifest = {
        "digest": digest,
        "files": {rel: hashlib.sha256(d).hexdigest() for rel, d in sorted(files.items())},
        "criteria": [c.as_dict() for c in criteria],
    }
    (tmp / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(tmp / MANIFEST_NAME, 0o644)
    for d in (tmp, *(p for p in tmp.rglob("*") if p.is_dir())):
        os.chmod(d, 0o755)
    os.replace(tmp, root)
    return Frozen(root, digest, criteria)


def read_tree(root: Path) -> tuple[bytes, dict[str, bytes]]:
    """brief.md and checks/ as they are on disk. Refuses anything but plain files."""
    brief = _plain(root / BRIEF_NAME).read_bytes()
    checks: dict[str, bytes] = {}
    base = root / CHECKS_DIR
    if base.exists():
        _plain_dir(base)
        for dirpath, dirnames, filenames in os.walk(base):
            for name in dirnames:
                _plain_dir(Path(dirpath) / name)
            for name in filenames:
                path = _plain(Path(dirpath) / name)
                checks[path.relative_to(base).as_posix()] = path.read_bytes()
    return brief, checks


def _plain(path: Path) -> Path:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise FrozenError(f"{path} is not a regular file")
    return path


def _plain_dir(path: Path) -> None:
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise FrozenError(f"{path} is not a directory")


def load(root: Path, expected_digest: str | None = None) -> Frozen:
    """The frozen agreement, recomputed from disk. Raises FrozenError when its digest
    is not `expected_digest` (what was recorded at launch)."""
    try:
        brief, checks = read_tree(root)
    except OSError as exc:
        raise FrozenError(f"cannot read the frozen agreement in {root}: {exc}") from None
    digest = bundle_digest(brief, checks)
    if expected_digest is not None and digest != expected_digest:
        raise FrozenError(
            f"the frozen agreement in {root} changed after launch: digest {digest},"
            f" recorded {expected_digest}"
        )
    return Frozen(root, digest, parse_criteria(checks))


def read_bundle(path: Path) -> tuple[str, dict[str, str]]:
    """What the CLI sends for `launch --brief PATH`: a brief file, or a directory with
    brief.md and an optional checks/. Check files must be UTF-8 text."""
    if path.is_file():
        return path.read_text(), {}
    if not path.is_dir():
        raise FrozenError(f"{path}: no such file or directory")
    brief_file = path / BRIEF_NAME
    if not brief_file.is_file():
        raise FrozenError(f"{path}: a brief directory needs {BRIEF_NAME}")
    checks: dict[str, str] = {}
    base = path / CHECKS_DIR
    if base.is_dir():
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_NAMES)
            for name in sorted(filenames):
                if name in SKIPPED_NAMES or name.endswith(".pyc"):
                    continue
                file = Path(dirpath) / name
                rel = file.relative_to(base).as_posix()
                if file.is_symlink() or not file.is_file():
                    raise FrozenError(f"checks/{rel}: only regular files can be frozen")
                try:
                    checks[check_path(rel)] = file.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    raise FrozenError(f"checks/{rel}: not UTF-8 text") from None
    return brief_file.read_text(), checks


def encode(checks: Mapping[str, str]) -> dict[str, bytes]:
    return {rel: text.encode() for rel, text in checks.items()}
