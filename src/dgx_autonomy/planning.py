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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frozen
from .agent_files import AgentFileError, open_dir, read_bytes_at

PLANNER_WORKDIR = "/workspace"
DRAFT_DIR_NAME = "draft"
DRAFT_DIR = f"{PLANNER_WORKDIR}/{DRAFT_DIR_NAME}"
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
- At least one criterion must be automated. Criteria that need a person (looks,
  feel) are `kind: human_judgment` with no test; the operator judges them later.

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
(every automated check should run, and fail, against an empty target), and /launch
to freeze the draft and start the run.

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

    @property
    def launchable(self) -> bool:
        return self.problem is None

    def view(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "problem": self.problem,
            "brief": self.brief.decode("utf-8", "replace") if self.brief is not None else None,
            "files": sorted(self.checks),
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
    return check_draft(brief, checks)


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


def dry_run_summary(
    digest: str, criteria: Sequence[frozen.Criterion], results: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    checks = []
    for c in criteria:
        if c.kind != "automated":
            continue
        r = results.get(c.key) or {"status": "not_run", "summary": "not run"}
        ok, verdict = dry_run_verdict(str(r.get("status")))
        checks.append(
            {
                "key": c.key,
                "test": c.test,
                "runner": c.runner,
                "status": r.get("status"),
                "ok": ok,
                "verdict": verdict,
                "summary": r.get("summary"),
                "excerpt": r.get("excerpt"),
            }
        )
    return {"digest": digest, "ok": bool(checks) and all(c["ok"] for c in checks), "checks": checks}


def dry_run_message(result: Mapping[str, Any]) -> str:
    """What the planner is told about the operator's dry run."""
    lines = [
        "The operator dry-ran the draft checks against an empty target (no app"
        " running). A working check fails there; one that errors does not run, and one"
        " that passes checks nothing."
    ]
    for c in result.get("checks") or []:
        mark = "ok     " if c["ok"] else "PROBLEM"
        lines.append(f"{mark} {c['key']} ({c['test']}): {c['verdict']}")
        if not c["ok"] and c.get("excerpt"):
            excerpt = str(c["excerpt"])[:1200]
            lines.append("    " + excerpt.replace("\n", "\n    "))
    if not result.get("ok"):
        lines.append("Fix the checks that have a problem, then tell the operator.")
    return "\n".join(lines)
