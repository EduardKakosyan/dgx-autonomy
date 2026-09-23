"""Protected evaluation: the frozen checks decide completion, not the builder.

When the builder claims completion (its conversation finishes), the controller
evaluates the claim:

    pin      check the frozen agreement's digest; snapshot the project (snapshot.py)
    demo     the demo must serve that snapshot: relaunch it if it was started from
             another one (controller.py)
    run      one disposable evaluator container per automated criterion, one at a
             time: the checks read-only at /checks, an output directory of its own
             at /out, the egress network (it reaches the demo by the sandbox's
             name; no Docker socket, no model, no controller state)
    conclude snapshot again: if the project moved, the result cannot be attributed
             to the pinned snapshot and the evaluation is inconclusive. Otherwise
             passed / failed / infra_error from the criteria's results.

Each step is one tick of the controller's loop, recorded on the evaluation, so a
long browser test never blocks the loop and a restarted controller starts the
evaluation over (it changes nothing in the project, so running it again is safe).

A test runner that crashes, times out or reports nothing is an infrastructure
error, never a pass and never an application failure. Only the checks' own
assertions fail a criterion. The evaluator's output is read like any file an
untrusted process wrote (agent_files.py): no symlinks, bounded sizes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import frozen
from .agent_files import AgentFileError, open_dir, read_bytes_at
from .config import Settings
from .egress import check_policy
from .ports import Clock, ContainerSpec, Mount, RuntimePort, SnapshotPort
from .runtime import LABEL_OP, LABEL_PREFIX, LABEL_ROLE, LABEL_RUN, DockerError
from .snapshot import SnapshotError
from .state import CriterionRow, Evaluation, EvaluationResult, StateStore

CriterionStatus = Literal["passed", "failed", "error", "not_run"]

EVALUATOR_CHECKS_DIR = "/checks"
EVALUATOR_OUT_DIR = "/out"
EVALUATOR_REFERENCE_DIR = "/reference"
EVALUATOR_PYTHON = "/opt/evaluator/venv/bin/python"
EVALUATOR_PLAYWRIGHT = "/opt/evaluator/node_modules/.bin/playwright"
EVALUATOR_PLAYWRIGHT_CONFIG = "/opt/evaluator/playwright.config.mjs"
LABEL_CRITERION = f"{LABEL_PREFIX}.criterion"
REFERENCE_URL = "http://127.0.0.1:3000"
# Serve /reference on the container's loopback, wait until it listens, then run the
# check ("$@"). Its log goes next to the check's report.
SERVE_REFERENCE = (
    f"{EVALUATOR_PYTHON} -m http.server 3000 --bind 127.0.0.1 --directory"
    f" {EVALUATOR_REFERENCE_DIR} > {EVALUATOR_OUT_DIR}/reference-server.log 2>&1 &"
    " i=0; while [ $i -lt 100 ]; do"
    f" {EVALUATOR_PYTHON} -c 'import socket; socket.create_connection((\"127.0.0.1\", 3000), 1)'"
    ' 2>/dev/null && break; i=$((i+1)); sleep 0.1; done; exec "$@"'
)
MAX_REPORT_BYTES = 8 * 1024 * 1024
EXCERPT_CHARS = 2000
LOG_TAIL_LINES = 200
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# pytest exit codes: https://docs.pytest.org/en/stable/reference/exit-codes.html
_PYTEST_EXIT = {
    2: "the test run was interrupted",
    3: "pytest hit an internal error",
    4: "pytest was invoked wrongly",
    5: "no tests were collected",
}


class EvaluationError(RuntimeError):
    """This attempt cannot produce a result (an infrastructure problem)."""


@dataclass(frozen=True)
class CriterionResult:
    status: CriterionStatus
    summary: str
    excerpt: str = ""
    exit_code: int | None = None
    counts: dict[str, int] | None = None
    evidence: str | None = None  # the criterion's output directory, on the DGX

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluator_container_name(run_id: str, n: int, index: int) -> str:
    return f"{LABEL_PREFIX}-eval-{run_id}-{n}-{index}"


def app_url(settings: Settings, sandbox_name: str) -> str:
    """The demo as the evaluator reaches it: the sandbox's name on the egress network."""
    return f"http://{sandbox_name}:{settings.demo_port}"


def evaluator_spec(
    settings: Settings,
    *,
    run_id: str,
    ev: Evaluation,
    index: int,
    criterion: CriterionRow,
    checks_dir: Path,
    out_dir: Path,
    url: str,
) -> ContainerSpec:
    """One criterion's evaluator: frozen checks read-only, its own output directory,
    the demo over the egress network, nothing else."""
    return check_container_spec(
        settings,
        name=evaluator_container_name(run_id, ev.n, index),
        labels={
            LABEL_RUN: run_id,
            LABEL_OP: ev.id,
            LABEL_ROLE: "evaluator",
            LABEL_CRITERION: criterion.key,
        },
        key=criterion.key,
        runner=criterion.runner,
        test=criterion.test,
        checks_dir=checks_dir,
        out_dir=out_dir,
        url=url,
    )


def check_container_spec(
    settings: Settings,
    *,
    name: str,
    labels: Mapping[str, str],
    key: str,
    runner: str | None,
    test: str | None,
    checks_dir: Path,
    out_dir: Path,
    url: str,
    reference_dir: Path | None = None,
) -> ContainerSpec:
    """A container that runs one check file against `url` (evaluation, or the
    planning dry run): the checks read-only at /checks, /out for its report.

    With `reference_dir` (the planning dry run's reference app), the container first
    serves it on its own loopback, port 3000, and the check runs against that.
    """
    path = f"{EVALUATOR_CHECKS_DIR}/{test}"
    if runner == "pytest":
        command: tuple[str, ...] = (
            EVALUATOR_PYTHON, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rfE",
            f"--junitxml={EVALUATOR_OUT_DIR}/junit.xml", "-o", "junit_family=xunit2", path,
        )  # fmt: skip
    elif runner == "playwright":
        command = (EVALUATOR_PLAYWRIGHT, "test", f"--config={EVALUATOR_PLAYWRIGHT_CONFIG}", path)
    else:
        raise EvaluationError(f"criterion {key}: unknown runner {runner!r}")
    mounts = [
        Mount(str(checks_dir), EVALUATOR_CHECKS_DIR, read_only=True),
        Mount(str(out_dir), EVALUATOR_OUT_DIR),
    ]
    if reference_dir is not None:
        mounts.append(Mount(str(reference_dir), EVALUATOR_REFERENCE_DIR, read_only=True))
        command = ("/bin/sh", "-c", SERVE_REFERENCE, "serve-reference", *command)
    return ContainerSpec(
        name=name,
        image=settings.evaluator_image,
        labels=dict(labels),
        networks=(settings.egress_network,),
        dns=settings.agent_dns,
        mounts=tuple(mounts),
        env={
            "APP_URL": url,
            "BASE_URL": url,
            "CI": "1",
            "HOME": "/tmp",
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        command=command,
        workdir=EVALUATOR_CHECKS_DIR,
        user=f"{settings.evaluator_uid}:{settings.evaluator_gid}",
        memory=settings.evaluator_memory,
        cpus=settings.evaluator_cpus,
        pids_limit=settings.evaluator_pids_limit,
    )


# --- reading what a runner reported ----------------------------------------------------


def _clean(text: str) -> str:
    return _ANSI.sub("", text).strip()


def _clip(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The start of a report (the first failures matter most)."""
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The end of a log (where a crash says why)."""
    text = _clean(text)
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def _read_output(out_dir: Path, name: str) -> bytes | None:
    """A file the evaluator wrote into its output directory, or None."""
    try:
        with open_dir(out_dir) as fd:
            return read_bytes_at(fd, name, MAX_REPORT_BYTES)
    except FileNotFoundError:
        return None
    except (AgentFileError, OSError):
        return None


def _pytest_counts(junit: bytes) -> tuple[dict[str, int], list[str]]:
    root = ET.fromstring(junit)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for k in counts:
            counts[k] += int(suite.get(k, "0") or 0)
    failing: list[str] = []
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            bad = case.find(tag)
            if bad is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
            message = bad.get("message") or ""
            body = bad.text or ""
            failing.append(f"{name} ({tag}): {message}\n{body[-800:]}".strip())
    return counts, failing


def classify_pytest(exit_code: int, out_dir: Path, logs: str) -> CriterionResult:
    counts: dict[str, int] | None = None
    failing: list[str] = []
    junit = _read_output(out_dir, "junit.xml")
    if junit is not None:
        try:
            counts, failing = _pytest_counts(junit)
        except (ET.ParseError, ValueError):
            counts = None
    excerpt = _clip("\n\n".join(failing)) if failing else _tail(logs)
    if exit_code == 0:
        return CriterionResult("passed", "all tests passed", "", exit_code, counts)
    if exit_code == 1 and counts is not None:
        n = counts["failures"] + counts["errors"]
        summary = f"{n} test(s) failed" if n else "tests failed"
        return CriterionResult("failed", summary, excerpt, exit_code, counts)
    if exit_code == 1:
        # A failure has to be shown by the report, not inferred from an exit code.
        return CriterionResult(
            "error", "pytest reported failures but wrote no readable report", _tail(logs), 1
        )
    why = _PYTEST_EXIT.get(exit_code, f"pytest exited with {exit_code}")
    return CriterionResult("error", why, _tail(logs), exit_code, counts)


def _playwright_failures(suite: Mapping[str, Any], trail: Sequence[str], out: list[str]) -> None:
    title = [*trail, str(suite.get("title") or "")]
    for spec in suite.get("specs") or []:
        if not isinstance(spec, dict) or spec.get("ok", True):
            continue
        name = " > ".join(t for t in [*title, str(spec.get("title") or "")] if t)
        messages = []
        for test in spec.get("tests") or []:
            for result in (test or {}).get("results") or []:
                for err in (result or {}).get("errors") or [(result or {}).get("error")]:
                    if isinstance(err, dict) and err.get("message"):
                        messages.append(_clean(str(err["message"]))[:800])
        out.append(f"{name}: " + ("\n".join(messages[:2]) or "failed"))
    for child in suite.get("suites") or []:
        if isinstance(child, dict):
            _playwright_failures(child, title, out)


def classify_playwright(exit_code: int, out_dir: Path, logs: str) -> CriterionResult:
    raw = _read_output(out_dir, "report.json")
    if raw is None:
        return CriterionResult(
            "error", f"Playwright wrote no report (exit {exit_code})", _tail(logs), exit_code
        )
    try:
        report = json.loads(raw)
        stats = report.get("stats") or {}
        counts = {k: int(stats.get(k) or 0) for k in ("expected", "unexpected", "flaky", "skipped")}
    except (ValueError, AttributeError, TypeError):
        return CriterionResult(
            "error", "Playwright's report is not readable", _tail(logs), exit_code
        )
    global_errors = [
        _clean(str(e.get("message", "")))[:800]
        for e in report.get("errors") or []
        if isinstance(e, dict)
    ]
    ran = counts["expected"] + counts["unexpected"] + counts["flaky"]
    if counts["unexpected"]:
        failures: list[str] = []
        for suite in report.get("suites") or []:
            if isinstance(suite, dict):
                _playwright_failures(suite, [], failures)
        excerpt = _clip("\n\n".join(failures)) if failures else _tail(logs)
        return CriterionResult(
            "failed", f"{counts['unexpected']} test(s) failed", excerpt, exit_code, counts
        )
    if ran == 0 or global_errors or exit_code != 0:
        why = "no tests ran" if ran == 0 else f"Playwright exited with {exit_code}"
        excerpt = _clip("\n".join(global_errors)) if global_errors else _tail(logs)
        return CriterionResult("error", why, excerpt, exit_code, counts)
    return CriterionResult("passed", f"{ran} test(s) passed", "", exit_code, counts)


def classify(runner: str | None, exit_code: int, out_dir: Path, logs: str) -> CriterionResult:
    if runner == "pytest":
        result = classify_pytest(exit_code, out_dir, logs)
    elif runner == "playwright":
        result = classify_playwright(exit_code, out_dir, logs)
    else:
        result = CriterionResult("error", f"unknown runner {runner!r}", "", exit_code)
    if exit_code == 137 and result.status != "passed":
        # SIGKILL: the memory limit (or a kill), not the application.
        result = CriterionResult(
            "error", "the evaluator was killed (out of memory?)", result.excerpt, exit_code,
            result.counts,
        )  # fmt: skip
    return result


def overall(
    criteria: Sequence[CriterionRow], results: Mapping[str, Mapping[str, Any]]
) -> tuple[EvaluationResult, str]:
    """passed only when every required automated criterion passed.

    A required criterion that failed makes the evaluation `failed` (its evidence
    goes back to the builder), even if another one could not run. Otherwise, a
    required criterion without a result is an infrastructure error.
    """
    required = [c for c in criteria if c.kind == "automated" and c.required]
    status = {c.key: str((results.get(c.key) or {}).get("status", "not_run")) for c in required}
    failed = sorted(k for k, s in status.items() if s == "failed")
    if failed:
        return "failed", f"failed: {', '.join(failed)}"
    undecided = sorted(k for k, s in status.items() if s != "passed")
    if undecided:
        return "infra_error", f"no result for: {', '.join(undecided)}"
    return "passed", f"{len(required)} required check(s) passed"


def failure_message(
    ev: Evaluation,
    criteria: Sequence[CriterionRow],
    results: Mapping[str, Mapping[str, Any]],
    *,
    demo_note: str | None,
    checks_dir: str,
) -> str:
    """What the builder is told when its completion claim is not accepted."""
    lines = [
        f"Your completion claim is not accepted yet. The environment ran the agreed"
        f" acceptance checks (evaluation #{ev.n}) against the app that start_demo serves,"
        f" at project snapshot {str(ev.snapshot_id or '?')[:12]}.",
    ]
    if demo_note:
        lines.append(demo_note)
    for c in criteria:
        if c.kind != "automated":
            continue
        r = results.get(c.key) or {}
        status = str(r.get("status", "not_run")).upper()
        optional = "" if c.required else " (optional)"
        lines.append(f"\n{status}{optional}  {c.key}: {c.description}  [{checks_dir}/{c.test}]")
        if r.get("status") != "passed":
            lines.append(f"  {r.get('summary', '')}")
            if r.get("excerpt"):
                lines.append("  " + str(r["excerpt"]).replace("\n", "\n  "))
    lines.append(
        f"\nThe checks are read-only in {checks_dir}; they are the agreement and do not"
        " change. Fix the app, make sure start_demo serves the fixed version, verify it"
        " yourself, then finish again. Every finish is checked the same way."
    )
    return "\n".join(lines)


# --- one evaluation, step by step ----------------------------------------------------


class Evaluator:
    """Runs an evaluation's steps. The controller decides when (claims, stops) and
    handles the demo and the conversation; this class never touches either."""

    def __init__(
        self,
        *,
        settings: Settings,
        state: StateStore,
        runtime: RuntimePort,
        snapshots: SnapshotPort,
        clock: Clock,
        chown: Any,
    ) -> None:
        self._settings = settings
        self._state = state
        self._runtime = runtime
        self._snapshots = snapshots
        self._clock = clock
        self._chown = chown

    def _save(self, ev: Evaluation, steps: dict[str, Any]) -> Evaluation:
        return self._state.record_evaluation_steps(ev.id, steps)

    def pin(
        self, ev: Evaluation, *, frozen_dir: Path, expected_digest: str, store: Path, project: Path
    ) -> Evaluation:
        """Check the frozen agreement and pin the project snapshot."""
        if ev.snapshot_id is not None:
            return ev
        try:
            frozen.load(frozen_dir, expected_digest)
        except frozen.FrozenError as exc:
            raise EvaluationError(str(exc)) from None
        try:
            snap = self._snapshots.take(store, project, f"evaluation #{ev.n} (before)")
        except SnapshotError as exc:
            raise EvaluationError(f"cannot snapshot the project: {exc}") from None
        Path(ev.evidence_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
        return self._state.pin_evaluation(ev.id, snapshot_id=snap.tree, snapshot_commit=snap.commit)

    def run_next(
        self, run_id: str, ev: Evaluation, *, checks_dir: Path, url: str
    ) -> tuple[Evaluation, bool]:
        """Advance the criteria by one step. True once every automated one has a result."""
        steps = dict(ev.steps)
        running: dict[str, str] = dict(steps.get("running") or {})
        results: dict[str, Any] = dict(steps.get("results") or {})
        automated = [c for c in self._state.criteria(run_id) if c.kind == "automated"]
        for index, c in enumerate(automated):
            if c.key in results:
                continue
            name = evaluator_container_name(run_id, ev.n, index)
            out_dir = Path(ev.evidence_dir) / f"{index}-{c.key}"
            if c.key not in running:
                self._start(run_id, ev, index, c, name, out_dir, checks_dir, url)
                running[c.key] = self._clock.now().isoformat()
                steps["running"] = running
                return self._save(ev, steps), False
            result = self._collect(c, name, out_dir, datetime.fromisoformat(running[c.key]))
            if result is None:
                return ev, False  # still running
            results[c.key] = result.as_dict()
            del running[c.key]
            steps.update(running=running, results=results)
            ev = self._save(ev, steps)
        return ev, True

    def _start(
        self,
        run_id: str,
        ev: Evaluation,
        index: int,
        c: CriterionRow,
        name: str,
        out_dir: Path,
        checks_dir: Path,
        url: str,
    ) -> None:
        policy = check_policy(self._settings, self._runtime)
        if not policy.ok:
            raise EvaluationError("refusing to start the evaluator: " + "; ".join(policy.problems))
        # A container by this name is from an interrupted attempt: never reuse it.
        self._runtime.remove_container(name)
        if out_dir.exists():
            raise EvaluationError(f"{out_dir} already exists; evidence is never overwritten")
        out_dir.mkdir(mode=0o755, parents=True)
        self._chown(out_dir, self._settings.evaluator_uid, self._settings.evaluator_gid)
        spec = evaluator_spec(
            self._settings,
            run_id=run_id,
            ev=ev,
            index=index,
            criterion=c,
            checks_dir=checks_dir,
            out_dir=out_dir,
            url=url,
        )
        try:
            self._runtime.ensure_container(spec)
        except DockerError as exc:
            raise EvaluationError(f"cannot start the evaluator for {c.key}: {exc}") from None

    def _collect(
        self, c: CriterionRow, name: str, out_dir: Path, started: datetime
    ) -> CriterionResult | None:
        container = self._runtime.inspect_container(name)
        logs = ""
        if container is None:
            result = CriterionResult("error", "the evaluator container disappeared")
        elif container.running:
            waited = (self._clock.now() - started).total_seconds()
            if waited <= self._settings.evaluator_timeout_s:
                return None
            self._runtime.kill_container(name)
            logs = self._runtime.container_logs(name, tail=LOG_TAIL_LINES)
            limit = self._settings.evaluator_timeout_s
            result = CriterionResult(
                "error", f"no result after {limit:.0f}s; the evaluator was stopped", _tail(logs)
            )
        else:
            logs = self._runtime.container_logs(name, tail=LOG_TAIL_LINES)
            result = classify(c.runner, container.exit_code, out_dir, logs)
        # Controller-owned, next to (not inside) the evaluator's directory.
        _write_new(out_dir.with_name(out_dir.name + ".log"), logs)
        self._runtime.remove_container(name)
        return CriterionResult(
            result.status, result.summary, result.excerpt, result.exit_code, result.counts,
            str(out_dir),
        )  # fmt: skip

    def conclude(
        self,
        run_id: str,
        ev: Evaluation,
        *,
        store: Path,
        project: Path,
        quiescence_problem: str | None,
    ) -> Evaluation:
        """Decide. The project must still be the pinned snapshot, and the builder
        must not have been working meanwhile; otherwise the result is inconclusive."""
        results: dict[str, Any] = dict(ev.steps.get("results") or {})
        criteria = self._state.criteria(run_id)
        detail: str | None
        if quiescence_problem is not None:
            status: EvaluationResult = "inconclusive"
            detail = quiescence_problem
        else:
            try:
                after = self._snapshots.take(store, project, f"evaluation #{ev.n} (after)")
            except SnapshotError as exc:
                raise EvaluationError(f"cannot snapshot the project: {exc}") from None
            if after.tree != ev.snapshot_id:
                status = "inconclusive"
                detail = (
                    f"the project changed while the checks ran ({str(ev.snapshot_id)[:12]} ->"
                    f" {after.tree[:12]}); the results cannot be attributed to one snapshot"
                )
            else:
                status, detail = overall(criteria, results)
        return self.finish(ev, status=status, detail=detail, results=results)

    def finish(
        self,
        ev: Evaluation,
        *,
        status: EvaluationResult,
        detail: str | None,
        results: Mapping[str, Any] | None = None,
    ) -> Evaluation:
        results = dict(results if results is not None else ev.steps.get("results") or {})
        self.remove_containers(ev)
        ev = self._state.finish_evaluation(
            ev.id, status=status, detail=detail, results=results, now=self._clock.now()
        )
        summary = {
            "evaluation": ev.n,
            "trigger": ev.trigger,
            "status": ev.status,
            "detail": ev.detail,
            "check_digest": ev.check_digest,
            "snapshot": ev.snapshot_id,
            "snapshot_commit": ev.snapshot_commit,
            "claim": {"event_id": ev.claim_event_id, "text": ev.claim_text},
            "started_at": ev.started_at.isoformat(),
            "finished_at": ev.finished_at.isoformat() if ev.finished_at else None,
            "results": results,
            "steps": ev.steps,
        }
        root = Path(ev.evidence_dir)
        try:
            root.mkdir(mode=0o755, parents=True, exist_ok=True)
            _write_new(root / "evaluation.json", json.dumps(summary, indent=2, default=str))
        except OSError:
            pass  # the database has the result; the file is a convenience
        return ev

    def remove_containers(self, ev: Evaluation) -> None:
        """Remove every evaluator container of this evaluation (by label)."""
        try:
            snapshot = self._runtime.inspect(ev.run_id)
        except DockerError:
            return
        for c in snapshot.containers:
            if c.labels.get(LABEL_ROLE) == "evaluator" and c.labels.get(LABEL_OP) == ev.id:
                with contextlib.suppress(DockerError):
                    self._runtime.remove_container(c.name)


def _write_new(path: Path, text: str) -> None:
    """Create a controller-owned file; never follow or overwrite what is there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as f:
        f.write(text)
