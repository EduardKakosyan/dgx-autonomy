"""Interactive planning: the front door that produces the frozen brief and checks.

`dgx-autonomy plan` opens a planning session owned by the controller. The operator
and the selected model (the same one the run will use) research the request and
agree on requirements. The model keeps a draft of the agreement in its own
workspace:

    /workspace/draft/brief.md
    /workspace/draft/checks/criteria.yaml    (frozen.py describes the format)
    /workspace/draft/checks/...              one executable check per automated criterion

The planner runs in a sandbox of its own (`dgx-autonomy-plan-<id>`), with the agent
image and hardening, the egress policy for web research, and no demo port. It has
no project: nothing it writes reaches a run except the draft, and only when the
operator launches. Its conversation lives in that workspace, so detaching (or
losing SSH) changes nothing, and `plan --attach` continues it.

`/checks` dry-runs the draft checks in evaluator containers against an empty
target. A check that fails there is fine: it runs, and nothing satisfies it yet. A
check that errors (does not run) or passes (checks nothing) is a problem. The
result goes to the operator and, as context, to the planner.

`/launch` freezes exactly the draft the operator reviewed: the CLI shows the draft
and its digest, and the controller refuses when the draft on disk no longer has
that digest. The run is created then, with its deadline; the plan records which
run it became, and the planner sandbox is removed. Its conversation stays on disk.

The draft is written by the model, so the controller reads it like any file the
agent controls (agent_files.py): no symlinks, plain files only, bounded sizes.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import frozen
from .agent_files import AgentFileError, open_dir, read_bytes_at

PLANNER_WORKDIR = "/workspace"
DRAFT_DIR_NAME = "draft"
DRAFT_DIR = f"{PLANNER_WORKDIR}/{DRAFT_DIR_NAME}"
REFERENCE_DIR_NAME = "reference"
MAX_REQUEST_BYTES = 64 * 1024
# The dry run's target: the evaluator's own loopback, where nothing listens.
EMPTY_TARGET = "http://127.0.0.1:3000"


class PlanningError(ValueError):
    """The planning request cannot be served; the message goes back to the operator."""


def planning_message(request: str, *, model_key: str, max_budget_hours: float) -> str:
    """The planner's opening message: its role, the draft format, and the request."""
    return f"""\
You are the planning partner for an unattended coding run on a DGX Spark. You do not
build the app. With the operator (the user in this conversation) you research the
request, agree on the requirements, and write the agreement that a separate builder
agent receives when the operator launches the run. After launch the builder works
alone for up to {max_budget_hours:g} hours and nobody answers its questions, so the
agreement must stand on its own. The builder is the same model as you ({model_key}).

How to work
1. Research and ask first. Ask the operator a few clarifying questions at a time.
   Use the terminal for web research when it helps (curl works; there is no
   browser). Propose concrete requirements and get the operator's agreement.
2. Write the draft, and keep it current whenever a decision changes:
   - {DRAFT_DIR}/brief.md: what to build and for whom, the required features, the
     constraints (stack, data sources), the non-goals, and a plan that includes
     testing and refinement: the builder must test its own work and polish it
     before it finishes. The builder serves the app with its start_demo tool on
     port 3000; say what should be served there.
   - {DRAFT_DIR}/checks/{frozen.CRITERIA_FILE}: the acceptance criteria.
   - One executable check file per automated criterion, in {DRAFT_DIR}/checks/.
3. Keep replies short and concrete. Do not build the app, and do not say that the
   run has started: only the operator launches it.

What the builder has
- A Linux sandbox with Node 22, pnpm, Python 3.12, and internet access for
  dependencies and research. No GPU, no API keys, no accounts, no credit card:
  the app must work without paid services or keys (free keyless APIs are fine).
- The frozen brief and checks, read-only. It can read the checks.

Acceptance checks
- They run after the builder says it is done, each file in its own evaluator
  container, against the running app at the URL in the environment variable
  APP_URL. They cannot see the builder's files. 60 seconds per test, no retries.
- Playwright Test 1.63 with Chromium: `*.spec.ts` files, `import {{ test, expect }}
  from '@playwright/test'`. baseURL is APP_URL, so `await page.goto('/')` works.
- pytest 9 with httpx: `test_*.py` files; read `os.environ["APP_URL"]`.
- Check what a user can observe, not implementation details. If a check relies on a
  particular route, label, role or data-testid, the brief must require it. Do not
  assert on live third-party data values, which change.
- Playwright locators must match exactly one element. `getByLabel('Total')` also
  matches "Per-Person Total"; pass `{{ exact: true }}` whenever one label, name or
  text is contained in another.
- At least one criterion must be automated. Criteria that need a person (looks,
  feel) are `kind: human_judgment` with no test; the operator judges them later.
- Prove the checks can pass: write a small throwaway reference app, static files
  only (index.html with inline JS; fixed sample data instead of live APIs), in
  {DRAFT_DIR}/{REFERENCE_DIR_NAME}/. The dry run serves it and every automated check
  must pass against it. It is not part of the agreement: the builder never sees it.

{frozen.CRITERIA_FILE} format:

criteria:
  - key: search-city                # [a-z0-9-], unique
    description: Searching a city shows its current temperature
    test: search.spec.ts            # the check file in checks/
  - key: api-health
    description: GET /api/health returns 200
    test: test_api.py
    required: false                 # reported, but does not block completion
  - key: pleasant-on-phones
    description: The layout is pleasant on a phone
    kind: human_judgment

The operator can type /draft to review your draft, /checks to dry-run the checks
(every automated check must fail against an empty target and pass against your
reference app), and /launch to freeze the draft and start the run.

The operator's request:

{request.strip()}
"""


# --- reading the draft ---------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    """The draft as it is on disk now. `problem` says why it cannot be launched."""

    brief: bytes | None
    checks: Mapping[str, bytes] = field(default_factory=dict)
    digest: str | None = None
    criteria: tuple[frozen.Criterion, ...] = ()
    problem: str | None = None
    # draft/reference/: a throwaway app the dry run checks the checks against. It is
    # not part of the agreement (not in the digest, never frozen, never shown to the
    # builder).
    reference: Mapping[str, bytes] = field(default_factory=dict)

    @property
    def launchable(self) -> bool:
        return self.problem is None

    def view(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "problem": self.problem,
            "brief": self.brief.decode("utf-8", "replace") if self.brief is not None else None,
            "files": sorted(self.checks),
            "reference_files": sorted(self.reference),
            "criteria": [
                {
                    "key": c.key,
                    "kind": c.kind,
                    "required": c.required,
                    "test": c.test,
                    "runner": c.runner,
                    "description": c.description,
                }
                for c in self.criteria
            ],
        }


def read_draft(agent_dir: Path) -> Draft:
    """The planner's draft under `agent_dir/draft`, read without following symlinks."""
    try:
        with open_dir(agent_dir, DRAFT_DIR_NAME) as draft_fd:
            brief = _read_file(draft_fd, frozen.BRIEF_NAME, frozen.MAX_BRIEF_BYTES)
            checks: dict[str, bytes] = {}
            try:
                checks_fd = os.open(
                    frozen.CHECKS_DIR,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=draft_fd,
                )
            except FileNotFoundError:
                checks_fd = -1
            except OSError as exc:
                return Draft(brief, problem=f"draft/checks: not a directory ({exc.strerror})")
            if checks_fd >= 0:
                try:
                    _walk(checks_fd, "", checks)
                finally:
                    os.close(checks_fd)
    except FileNotFoundError:
        return Draft(None, problem=f"there is no draft yet ({DRAFT_DIR}/{frozen.BRIEF_NAME})")
    except (AgentFileError, frozen.FrozenError) as exc:
        return Draft(None, problem=f"the draft cannot be read: {exc}")
    draft = check_draft(brief, checks)
    try:
        reference = read_reference(agent_dir)
    except (AgentFileError, frozen.FrozenError) as exc:
        return replace(draft, problem=draft.problem or f"draft/reference: {exc}")
    return replace(draft, reference=reference)


def read_reference(agent_dir: Path) -> dict[str, bytes]:
    """draft/reference/ (plain files only), or {} when there is none."""
    out: dict[str, bytes] = {}
    try:
        with open_dir(agent_dir, DRAFT_DIR_NAME, REFERENCE_DIR_NAME) as fd:
            _walk(fd, "", out)
    except FileNotFoundError:
        return {}
    return out


def check_draft(brief: bytes | None, checks: Mapping[str, bytes]) -> Draft:
    """Validate a draft the way a launch would (frozen.validate + an automated check)."""
    if brief is None:
        return Draft(None, checks, problem=f"the draft has no {frozen.BRIEF_NAME}")
    digest = frozen.bundle_digest(brief, checks)
    try:
        criteria = frozen.validate(brief, checks)
    except frozen.FrozenError as exc:
        return Draft(brief, checks, digest, problem=str(exc))
    if not any(c.kind == "automated" for c in criteria):
        return Draft(
            brief,
            checks,
            digest,
            criteria,
            problem=(
                "the draft has no automated acceptance criterion: a planned run needs at"
                " least one check the evaluator can run"
            ),
        )
    return Draft(brief, checks, digest, criteria)


def _read_file(dir_fd: int, name: str, max_bytes: int) -> bytes | None:
    try:
        return read_bytes_at(dir_fd, name, max_bytes)
    except FileNotFoundError:
        return None


def _walk(dir_fd: int, prefix: str, out: dict[str, bytes]) -> None:
    """Every plain file below dir_fd; symlinks and other oddities refuse the draft."""
    for entry in sorted(os.scandir(dir_fd), key=lambda e: e.name):
        if entry.name in frozen.SKIPPED_NAMES or entry.name.endswith(".pyc"):
            continue
        rel = f"{prefix}{entry.name}"
        st = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            frozen.check_path(rel)
            sub = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                _walk(sub, f"{rel}/", out)
            finally:
                os.close(sub)
        elif stat.S_ISREG(st.st_mode):
            frozen.check_path(rel)
            if len(out) >= frozen.MAX_CHECK_FILES:
                raise frozen.FrozenError(f"checks/ has more than {frozen.MAX_CHECK_FILES} files")
            out[rel] = read_bytes_at(dir_fd, entry.name, frozen.MAX_CHECK_FILE_BYTES)
        else:
            raise frozen.FrozenError(f"checks/{rel}: only plain files and directories")


# --- the dry run ---------------------------------------------------------------------


def dry_run_verdict(status: str) -> tuple[bool, str]:
    """Whether a check's result against an empty target is what a working check does."""
    if status == "failed":
        return True, "runs, and fails with no app (as it should)"
    if status == "passed":
        return False, "PASSES with no app at all: it does not check anything"
    return False, "does not run"


def reference_verdict(status: str | None) -> tuple[bool | None, str]:
    """Whether a check passes against the planner's reference app."""
    if status is None:
        return None, "no reference app: not shown that it can pass"
    if status == "passed":
        return True, "passes against the reference app"
    if status == "failed":
        return False, "FAILS against the reference app: the check or the reference is wrong"
    return False, "does not run against the reference app"


def dry_run_summary(
    digest: str,
    criteria: Sequence[frozen.Criterion],
    results: Mapping[str, Mapping[str, Any]],
    reference_results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    checks = []
    for c in criteria:
        if c.kind != "automated":
            continue
        r = results.get(c.key) or {"status": "not_run", "summary": "not run"}
        ok, verdict = dry_run_verdict(str(r.get("status")))
        ref = (reference_results or {}).get(c.key) if reference_results is not None else None
        ref_ok, ref_verdict = reference_verdict(str(ref.get("status")) if ref is not None else None)
        checks.append(
            {
                "key": c.key,
                "test": c.test,
                "runner": c.runner,
                "status": r.get("status"),
                "ok": ok and ref_ok is not False,
                "verdict": verdict,
                "summary": r.get("summary"),
                "excerpt": r.get("excerpt"),
                "reference_status": ref.get("status") if ref is not None else None,
                "reference_ok": ref_ok,
                "reference_verdict": ref_verdict,
                "reference_excerpt": ref.get("excerpt") if ref is not None else None,
            }
        )
    return {
        "digest": digest,
        "ok": bool(checks) and all(c["ok"] for c in checks),
        "reference": reference_results is not None,
        "satisfiable": reference_results is not None and all(c["reference_ok"] for c in checks),
        "checks": checks,
    }


def dry_run_message(result: Mapping[str, Any]) -> str:
    """What the planner is told about the operator's dry run."""
    lines = [
        "The operator dry-ran the draft checks against an empty target (no app"
        " running), and against your reference app if there is one. A working check"
        " fails against the empty target and passes against the reference."
    ]
    for c in result.get("checks") or []:
        mark = "ok     " if c["ok"] else "PROBLEM"
        lines.append(f"{mark} {c['key']} ({c['test']}): {c['verdict']}; {c['reference_verdict']}")
        if c["status"] != "failed" and c.get("excerpt"):
            lines.append("    " + str(c["excerpt"])[:1200].replace("\n", "\n    "))
        if c.get("reference_ok") is False and c.get("reference_excerpt"):
            excerpt = str(c["reference_excerpt"])[:1200]
            lines.append("    against the reference: " + excerpt.replace("\n", "\n    "))
    if not result.get("reference"):
        lines.append(
            f"There is no reference app in {DRAFT_DIR}/{REFERENCE_DIR_NAME}/, so nothing"
            " shows that the checks can pass. Write one."
        )
    if not result.get("ok"):
        lines.append(
            "Fix what has a problem (the checks, or the reference), then tell the operator."
        )
    return "\n".join(lines)
