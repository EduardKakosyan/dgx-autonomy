"""Project snapshots: which state of the agent's project an evaluation checked.

A snapshot is a git commit of the project directory, stored in a git directory the
controller owns (`runs/<id>/snapshots.git`). The agent's own repository, if it made
one, is never used: its config and hooks are the agent's, and git would run them as
root. The snapshot's id is the commit's tree sha, so two identical project states
have the same id whenever they were taken. The commits form a chain, for the
report's "what changed between these two evaluations".

git runs with no system or global config, no hooks, no fsmonitor, and no automatic
gc. It reads the working tree without following symlinks (a symlink is stored as a
link), and it honors the project's .gitignore plus a few default excludes for
dependency and build caches, which do not identify the project's state.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .runtime import CommandResult, Runner, subprocess_runner

SNAPSHOT_REF = "refs/heads/snapshots"
DEFAULT_EXCLUDES = (
    "node_modules/",
    ".pnpm-store/",
    ".npm/",
    ".next/",
    ".nuxt/",
    ".svelte-kit/",
    ".turbo/",
    ".cache/",
    ".parcel-cache/",
    ".vite/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
)
_HARDENING = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.symlinks=true",
    "-c", "core.autocrlf=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "safe.directory=*",
    "-c", "user.name=dgx-autonomy",
    "-c", "user.email=controller@dgx-autonomy.invalid",
)  # fmt: skip


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    tree: str  # the snapshot id: same content, same id
    commit: str


class GitSnapshots:
    """SnapshotPort over the git CLI. The runner is injected for tests."""

    def __init__(self, runner: Runner = subprocess_runner, timeout_s: float = 300.0) -> None:
        self._run = runner
        self._timeout = timeout_s

    def _git(self, store: Path, work_tree: Path | None, *args: str) -> CommandResult:
        # -C first: pathspecs are relative to the working directory.
        argv = ["git"] + (["-C", str(work_tree)] if work_tree is not None else [])
        argv.append(f"--git-dir={store}")
        if work_tree is not None:
            argv.append(f"--work-tree={work_tree}")
        argv += [*_HARDENING, *args]
        try:
            res = self._run(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired:
            raise SnapshotError(f"git {args[0]}: no result in {self._timeout:.0f}s") from None
        except FileNotFoundError:
            raise SnapshotError("git is not installed") from None
        if res.returncode != 0:
            raise SnapshotError(f"git {args[0]}: {(res.stderr or res.stdout).strip()[-1000:]}")
        return res

    def _ensure_store(self, store: Path) -> None:
        if (store / "HEAD").exists():
            return
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._git(store, None, "init", "--bare", "--quiet")
        (store / "info").mkdir(exist_ok=True)
        (store / "info" / "exclude").write_text("\n".join(DEFAULT_EXCLUDES) + "\n")

    def _head(self, store: Path) -> str | None:
        try:
            res = self._git(store, None, "rev-parse", "--verify", "--quiet", SNAPSHOT_REF)
        except SnapshotError:
            return None  # no snapshot yet
        return res.stdout.strip()

    def take(self, store: Path, project: Path, label: str) -> Snapshot:
        """Commit the project's current state; its tree sha is the snapshot id."""
        # The agent owns the directory above the project and could swap the project
        # for a symlink; git would then read (never write) whatever it points at.
        if project.is_symlink() or not project.is_dir():
            raise SnapshotError(f"{project} is not a directory")
        self._ensure_store(store)
        # A fresh index every time: git would otherwise trust the previous one's
        # stat data, which the agent controls (it sets mtimes as it likes).
        (store / "index").unlink(missing_ok=True)
        self._git(store, project, "add", "--all", "--", ".")
        tree = self._git(store, project, "write-tree").stdout.strip()
        parent = self._head(store)
        args = ["commit-tree", tree, "-m", label] + (["-p", parent] if parent else [])
        commit = self._git(store, None, *args).stdout.strip()
        self._git(store, None, "update-ref", SNAPSHOT_REF, commit)
        return Snapshot(tree=tree, commit=commit)

    def changed(self, store: Path, before: str, after: str, limit: int = 200) -> list[str]:
        """`git diff --name-status` between two snapshot commits (or trees)."""
        out = self._git(store, None, "diff", "--name-status", "--no-renames", before, after)
        lines = [line for line in out.stdout.splitlines() if line.strip()]
        return lines[:limit] + ([f"… {len(lines) - limit} more"] if len(lines) > limit else [])

    def archive(self, store: Path, commit: str, dest: Path) -> None:
        """The snapshot as a tar file (for a manual review bundle)."""
        self._git(store, None, "archive", "--format=tar", f"--output={dest}", commit)
