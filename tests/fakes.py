"""In-memory stand-ins for Docker, HTTP, the Agent Server and time.

Unit tests never need Docker, SSH or a model: every side effect goes through one of
these and is recorded for assertions.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dgx_autonomy.config import Settings
from dgx_autonomy.controller import Controller
from dgx_autonomy.evaluation import EVALUATOR_OUT_DIR, LABEL_CRITERION
from dgx_autonomy.openhands_adapter import conversation_id_for
from dgx_autonomy.ports import (
    ContainerSpec,
    ContainerState,
    ConversationRequest,
    ConversationSnapshot,
    DemoHandle,
    DemoSpec,
    DemoStatus,
    EventSummary,
    EvidenceMessage,
    HttpResponse,
    KillReport,
    RuntimeSnapshot,
    SandboxProcess,
    ServerRef,
    WorkspaceHandle,
    WorkspaceSpec,
)
from dgx_autonomy.runtime import (
    LABEL_ROLE,
    LABEL_RUN,
    CommandResult,
    DockerError,
    agent_container_name,
    agent_container_spec,
    docker_run_argv,
    planner_container_name,
)
from dgx_autonomy.state import StateStore


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


@dataclass
class FakeRunner:
    """Records docker argv. `responses` maps an argv prefix (joined by spaces) to a result."""

    responses: dict[str, CommandResult | Callable[[list[str]], CommandResult]] = field(
        default_factory=dict
    )
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        best = max((p for p in self.responses if joined.startswith(p)), key=len, default=None)
        if best is None:
            return CommandResult(0, "", "")
        res = self.responses[best]
        return res(argv) if callable(res) else res

    def commands(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == verb]


class FakeRuntime:
    """RuntimePort + ContainerPort with containers kept in a dict.

    Each sandbox has a simulated process table. A fresh sandbox holds the Agent
    Server, unless its control dir says `demo-only` (like the real supervisor).
    """

    def __init__(self, settings: Settings | None = None, trace: list[str] | None = None) -> None:
        self.settings = settings or Settings.from_env({})
        self.containers: dict[str, ContainerState] = {}
        self.specs: dict[str, ContainerSpec] = {}
        self.runs: list[str] = []  # names passed to a (fake) docker run
        self.started: list[str] = []  # existing, stopped containers started again
        self.killed: list[str] = []
        # Called with the run id whenever a sandbox boots with its Agent Server.
        self.on_agent_boot: list[Callable[[str], None]] = []
        self.logs: dict[str, str] = {}
        self.fail_run: dict[str, str] = {}
        self.trace = trace if trace is not None else []
        self.procs: dict[str, list[SandboxProcess]] = {}  # run_id -> processes
        self.control_dirs: dict[str, str] = {}
        self.unkillable: set[int] = set()  # pids that survive kill-agent (not a restart)
        self.listening: dict[str, bool] = {}  # run_id -> demo port listening
        self.fail_sandbox: str | None = None  # DockerError message for sandbox calls
        # Docker network -> host bridge. Default: what compose creates.
        self.bridges: dict[str, str | None] = {
            self.settings.egress_network: self.settings.egress_bridge,
            self.settings.internal_network: self.settings.internal_bridge,
        }
        self.removed: list[str] = []
        self.completed: list[ContainerSpec] = []  # specs passed to run_to_completion
        self.completion_output: tuple[int, str] = (0, "{}")
        # Evaluator containers "run" when they are created: criterion key -> what the
        # runner does (exit code, files it writes to /out, its log). None: it keeps
        # running (a hung test). Unknown keys pass, in their runner's format.
        self.evaluator_outcomes: dict[str, EvaluatorOutcome | None] = {}
        self.evaluators: list[ContainerSpec] = []
        # Dry runs of planning checks (run_to_completion): criterion key -> outcome.
        # None: the check never finishes (a timeout).
        self.dry_run_outcomes: dict[str, EvaluatorOutcome | None] = {}
        self.reference_outcomes: dict[str, EvaluatorOutcome | None] = {}
        # Called when an evaluator starts, e.g. to have the "agent" change the project.
        self.on_evaluator: list[Callable[[ContainerSpec], None]] = []
        self._next_pid = 100

    def _pid(self) -> int:
        self._next_pid += 1
        return self._next_pid

    def _boot(self, run_id: str) -> None:
        """A (re)started sandbox: PID 1, plus the Agent Server unless demo-only."""
        procs = [SandboxProcess(1, 0, 1, "S", "python3 -I dgx_sandbox.py supervise", "supervisor")]
        mode_file = self.control_dirs.get(run_id)
        mode = "agent"
        if mode_file is not None:
            try:
                with open(f"{mode_file}/mode") as f:
                    mode = f.read().strip()
            except OSError:
                pass
        if mode == "agent":
            pid = self._pid()
            procs.append(SandboxProcess(pid, 1, pid, "S", "/usr/local/bin/openhands-agent-server"))
        self.procs[run_id] = procs
        self.listening[run_id] = False
        if mode == "agent":
            for hook in self.on_agent_boot:
                hook(run_id)

    def agent_server_up(self, run_id: str) -> bool:
        return self._running(run_id) and any(
            "openhands-agent-server" in p.cmd for p in self.procs.get(run_id, [])
        )

    def agent_tool(self, run_id: str, cmd: str = "sleep 100000", *, sid: int | None = None) -> int:
        """Simulate a process the agent's tools started (e.g. under tmux)."""
        pid = self._pid()
        self.procs[run_id].append(SandboxProcess(pid, 1, sid or pid, "S", cmd))
        return pid

    def inspect_container(self, name: str) -> ContainerState | None:
        return self.containers.get(name)

    def ensure_container(self, spec: ContainerSpec) -> ContainerState:
        docker_run_argv(spec)  # same safety checks as the real adapter
        current = self.containers.get(spec.name)
        if current is not None and current.running:
            return current
        if spec.name in self.fail_run:
            raise DockerError(self.fail_run[spec.name])
        if current is not None:
            # docker start: the same container, with the spec it was created with.
            self.started.append(spec.name)
            state = replace(current, running=True, status="running", exit_code=0)
            spec = self.specs[spec.name]
        else:
            self.runs.append(spec.name)
            self.specs[spec.name] = spec
            state = ContainerState(
                id=f"cid-{spec.name}",
                name=spec.name,
                running=True,
                status="running",
                exit_code=0,
                labels=dict(spec.labels),
            )
        self.containers[spec.name] = state
        role = spec.labels.get(LABEL_ROLE)
        run_id = spec.labels.get(LABEL_RUN)
        if role == "evaluator":
            self._run_evaluator(spec)
            return self.containers[spec.name]
        if run_id is not None and role == "agent":
            for m in spec.mounts:
                if m.target == "/dgx-control":
                    self.control_dirs[run_id] = m.source
            self._boot(run_id)
        return state

    def _run_evaluator(self, spec: ContainerSpec) -> None:
        self.evaluators.append(spec)
        for hook in self.on_evaluator:
            hook(spec)
        key = spec.labels[LABEL_CRITERION]
        default = playwright_passes() if "playwright" in " ".join(spec.command) else pytest_passes()
        outcome = self.evaluator_outcomes.get(key, default)
        if outcome is None:
            return  # still running
        out = next(Path(m.source) for m in spec.mounts if m.target == EVALUATOR_OUT_DIR)
        for name, text in outcome.files.items():
            (out / name).write_text(text)
        self.exit(spec.name, code=outcome.exit_code, logs=outcome.logs)

    def container_logs(self, name: str, tail: int = 40) -> str:
        return self.logs.get(name, "")

    def kill_container(self, name: str) -> bool:
        c = self.containers.get(name)
        if c is None or not c.running:
            return False
        self.killed.append(name)
        self.exit(name, code=137)
        return True

    def reboot(self) -> None:
        """The DGX restarts: every container stops, every sandbox process is gone.

        None of them has a restart policy; only the controller comes back by itself.
        """
        for name in list(self.containers):
            self.exit(name, code=255)
        self.procs = {run_id: [] for run_id in self.procs}

    def remove_container(self, name: str) -> bool:
        self.trace.append(f"rm {name}")
        self.removed.append(name)
        return self.containers.pop(name, None) is not None

    def network_bridge(self, network: str) -> str | None:
        return self.bridges.get(network)

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> tuple[int, str]:
        docker_run_argv(spec, detach=False)  # same safety checks as the real adapter
        self.completed.append(spec)
        if spec.labels.get(LABEL_ROLE) == "dry-run":
            # A check against an empty target: by default it runs and fails.
            key = spec.labels[LABEL_CRITERION]
            playwright = "playwright" in " ".join(spec.command)
            if any(m.target == "/reference" for m in spec.mounts):
                # Against the planner's reference app: by default it passes.
                default = playwright_passes() if playwright else pytest_passes()
                outcome = self.reference_outcomes.get(key, default)
            else:
                default = (
                    playwright_fails("net::ERR_CONNECTION_REFUSED at http://127.0.0.1:3000/")
                    if playwright
                    else pytest_fails("httpx.ConnectError: [Errno 111] Connection refused")
                )
                outcome = self.dry_run_outcomes.get(key, default)
            if outcome is None:
                raise DockerError(f"{spec.name}: no result in {timeout_s:.0f}s")
            out = next(Path(m.source) for m in spec.mounts if m.target == EVALUATOR_OUT_DIR)
            for name, text in outcome.files.items():
                (out / name).write_text(text)
            return outcome.exit_code, outcome.logs
        return self.completion_output

    def exit(self, name: str, code: int = 1, logs: str = "") -> None:
        self.containers[name] = replace(
            self.containers[name], running=False, status="exited", exit_code=code
        )
        self.logs[name] = logs
        labels = self.containers[name].labels
        run_id = labels.get(LABEL_RUN)
        if run_id is not None and labels.get(LABEL_ROLE) == "agent":
            self.procs[run_id] = []

    def inspect(self, run_id: str) -> RuntimeSnapshot:
        found = tuple(c for c in self.containers.values() if c.labels.get(LABEL_RUN) == run_id)
        return RuntimeSnapshot(run_id=run_id, containers=found)

    def ensure_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        state = self.ensure_container(agent_container_spec(self.settings, spec))
        return WorkspaceHandle(
            container_id=state.id,
            running=state.running,
            url=f"http://{agent_container_name(spec.run_id)}:{self.settings.agent_port}",
        )

    # --- inside the sandbox --------------------------------------------------------

    def _running(self, run_id: str) -> bool:
        c = self.containers.get(agent_container_name(run_id))
        return c is not None and c.running

    def _classified(self, run_id: str, keep: int | None) -> tuple[SandboxProcess, ...]:
        out = []
        for p in self.procs.get(run_id, []):
            role = p.role
            if role == "agent" and keep and p.sid == keep:
                role = "demo"
            elif role == "demo" and p.sid != keep:
                role = "agent"
            out.append(replace(p, role=role))
        return tuple(out)

    def _check(self) -> None:
        if self.fail_sandbox is not None:
            raise DockerError(self.fail_sandbox)

    def sandbox_processes(
        self, run_id: str, keep_session: int | None
    ) -> tuple[SandboxProcess, ...] | None:
        self.trace.append(f"ps {run_id}")
        if not self._running(run_id):
            return None
        self._check()
        return self._classified(run_id, keep_session)

    def stop_agent(self, run_id: str, keep_session: int | None, grace_s: float) -> KillReport:
        self.trace.append(f"kill-agent keep={keep_session}")
        if not self._running(run_id):
            return KillReport(container_running=False)
        self._check()
        before = tuple(p for p in self._classified(run_id, keep_session) if p.role == "agent")
        doomed = {p.pid for p in before if p.pid not in self.unkillable}
        self.procs[run_id] = [p for p in self.procs[run_id] if p.pid not in doomed]
        survivors = tuple(p for p in before if p.pid not in doomed)
        return KillReport(container_running=True, before=before, survivors=survivors)

    def restart_sandbox(self, run_id: str) -> None:
        self.trace.append("restart")
        self._check()
        self.unkillable.clear()
        self._boot(run_id)

    def ensure_demo(self, spec: DemoSpec) -> DemoHandle:
        self.trace.append(f"demo-start {spec.command!r} replace={spec.replace_session}")
        self._check()
        procs = self.procs[spec.run_id]
        if spec.replace_session:
            procs[:] = [p for p in procs if p.sid != spec.replace_session]
        pid = self._pid()
        procs.append(SandboxProcess(pid, 1, pid, "S", f"/bin/sh -c {spec.command}", "demo"))
        return DemoHandle(session_id=pid)

    def demo_status(self, run_id: str, session_id: int, port: int) -> DemoStatus:
        if not self._running(run_id):
            return DemoStatus(alive=False, listening=False)
        self._check()
        alive = any(p.sid == session_id for p in self.procs.get(run_id, []))
        return DemoStatus(
            alive=alive, listening=alive and self.listening.get(run_id, False), log_tail="boom"
        )


@dataclass(frozen=True)
class EvaluatorOutcome:
    exit_code: int
    files: dict[str, str] = field(default_factory=dict)
    logs: str = ""


def pytest_passes() -> EvaluatorOutcome:
    junit = '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
    junit += '<testcase classname="test_app" name="test_ok"/></testsuite></testsuites>'
    return EvaluatorOutcome(0, {"junit.xml": junit}, "1 passed in 0.10s")


def pytest_fails(message: str = "assert 404 == 200") -> EvaluatorOutcome:
    junit = (
        '<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0">'
        '<testcase classname="test_app" name="test_version">'
        f'<failure message="{message}">def test_version():\n&gt;   {message}</failure>'
        "</testcase></testsuite></testsuites>"
    )
    return EvaluatorOutcome(
        1, {"junit.xml": junit}, f"FAILED test_app.py::test_version - {message}"
    )


def playwright_report(expected: int, unexpected: int, message: str = "") -> str:
    spec = {
        "title": "shows the greeting",
        "ok": unexpected == 0,
        "tests": [{"results": [{"status": "failed" if unexpected else "passed",
                                "errors": [{"message": message}] if unexpected else []}]}],
    }  # fmt: skip
    return json.dumps(
        {
            "suites": [{"title": "home.spec.ts", "specs": [spec], "suites": []}],
            "errors": [],
            "stats": {"expected": expected, "unexpected": unexpected, "flaky": 0, "skipped": 0},
        }
    )


def playwright_passes() -> EvaluatorOutcome:
    return EvaluatorOutcome(0, {"report.json": playwright_report(1, 0)}, "1 passed (2.1s)")


def playwright_fails(message: str) -> EvaluatorOutcome:
    return EvaluatorOutcome(1, {"report.json": playwright_report(0, 1, message)}, "1 failed")


def runner_crashes(
    logs: str = "Traceback: ModuleNotFoundError: No module named 'httpx'",
) -> EvaluatorOutcome:
    return EvaluatorOutcome(3, {}, logs)


class FakeHttp:
    """Routes by URL (exact, then prefix). Unrouted URLs look like a refused connection."""

    def __init__(self) -> None:
        self.routes: dict[str, HttpResponse | Callable[[str, Any], HttpResponse]] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 5.0,
    ) -> HttpResponse:
        self.calls.append((method, url, json_body))
        route = self.routes.get(url)
        if route is None:
            prefixes = [p for p in self.routes if url.startswith(p)]
            route = self.routes[max(prefixes, key=len)] if prefixes else None
        if route is None:
            return HttpResponse(status=0, error="connection refused")
        return route(method, json_body) if callable(route) else route


@dataclass
class FakeConversation:
    """ConversationPort. Set `status` to steer what the controller observes.

    `pause_to` is the status a pause leads to (None: pausing changes nothing, like
    an Agent Server stuck in a step). `start_gate`, when set, blocks start() until
    the event is set, like an SDK call that hangs.
    """

    status: str = "running"
    started: list[ConversationRequest] = field(default_factory=list)
    delivered: list[EvidenceMessage] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    interrupted: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    event_log: list[EventSummary] = field(default_factory=list)
    fail_start: Exception | None = None
    fail_pause: Exception | None = None
    fail_deliver: Exception | None = None
    pause_to: str | None = "paused"
    start_gate: threading.Event | None = None
    start_entered: threading.Event = field(default_factory=threading.Event)
    trace: list[str] = field(default_factory=list)
    event_limits: list[int] = field(default_factory=list)
    # `status` is the status of the newest conversation; earlier ones (replaced by a
    # rollover) keep theirs here. What each conversation was sent, by id.
    others: dict[str, str] = field(default_factory=dict)
    current: str | None = None
    sent_to: list[tuple[str, EvidenceMessage]] = field(default_factory=list)
    context_tokens: int | None = None

    def server_restarted(self, run_id: str) -> None:
        """What the Agent Server does when it loads a conversation that was RUNNING
        when it died: it marks it ERROR (SDK 1.49.4, EventService.start)."""
        mine = conversation_id_for(run_id)
        if any(r.conversation_id == mine for r in self.started) and self.status == "running":
            self.status = "error"

    def start(self, request: ConversationRequest) -> str:
        self.start_entered.set()
        if self.start_gate is not None:
            self.start_gate.wait(10)
        if self.fail_start is not None:
            raise self.fail_start
        if self.current is not None and request.conversation_id != self.current:
            if any(r.conversation_id == request.conversation_id for r in self.started):
                return request.conversation_id  # it exists already: attach
            self.others[self.current] = self.status
            self.status = "running"
        self.current = request.conversation_id
        self.started.append(request)
        return request.conversation_id

    def _status(self, conversation_id: str) -> str:
        if conversation_id in self.others:
            return self.others[conversation_id]
        return self.status

    def inspect(self, server: ServerRef, conversation_id: str) -> ConversationSnapshot:
        last = self.event_log[-1] if self.event_log else None
        return ConversationSnapshot(
            conversation_id, self._status(conversation_id), last, self.context_tokens
        )

    def resume(self, server: ServerRef, conversation_id: str) -> None:
        self.resumed.append(conversation_id)

    def pause(self, server: ServerRef, conversation_id: str) -> None:
        self.trace.append("pause")
        if self.fail_pause is not None:
            raise self.fail_pause
        self.paused.append(conversation_id)
        if conversation_id in self.others:
            if self.others[conversation_id] == "running":
                self.others[conversation_id] = "paused"
        elif self.pause_to is not None and self.status == "running":
            self.status = self.pause_to

    def interrupt(self, server: ServerRef, conversation_id: str) -> None:
        self.trace.append("interrupt")
        if self.fail_pause is not None:
            raise self.fail_pause
        self.interrupted.append(conversation_id)

    def deliver(self, server: ServerRef, conversation_id: str, evidence: EvidenceMessage) -> None:
        self.trace.append("deliver")
        if self.fail_deliver is not None:
            raise self.fail_deliver
        self.delivered.append(evidence)
        self.sent_to.append((conversation_id, evidence))
        if conversation_id in self.others:
            if evidence.run:
                self.others[conversation_id] = "running"
            return
        # A user message with run=True runs a conversation that is not running; a
        # FINISHED one is set IDLE first (SDK 1.49.4, LocalConversation.send_message).
        if evidence.run and self.status in ("error", "paused", "idle", "finished"):
            self.status = "running"

    def events(
        self,
        server: ServerRef,
        conversation_id: str,
        since: int,
        limit: int,
        *,
        text_limit: int = 300,
    ) -> Sequence[EventSummary]:
        self.event_limits.append(text_limit)
        return self.event_log[since : since + limit]

    def recent(self, server: ServerRef, conversation_id: str, limit: int) -> Sequence[EventSummary]:
        return list(reversed(self.event_log))[:limit]

    def claim(self, text: str = "The page is served on port 3000.") -> EventSummary:
        """The agent finishes: its finish action is the newest event."""
        event = EventSummary(
            id=f"ev-{len(self.event_log)}",
            timestamp="2026-09-22T12:00:00",
            kind="ActionEvent",
            source="agent",
            text=f'finish {{"message": "{text}"}}',
        )
        self.event_log.append(event)
        self.status = "finished"
        return event


@dataclass
class Harness:
    settings: Settings
    clock: FakeClock
    state: StateStore
    runtime: FakeRuntime
    http: FakeHttp
    conversation: FakeConversation
    controller: Controller
    chowned: list[tuple[str, int, int]]
    trace: list[str]

    def inference_loading(self) -> None:
        self.http.routes[f"{self.settings.inference_url}/health"] = HttpResponse(503, {})

    def inference_ready(self, slots: int = 1) -> None:
        self.http.routes[f"{self.settings.inference_url}/health"] = HttpResponse(
            200, {"status": "ok"}
        )
        self.http.routes[f"{self.settings.inference_url}/slots"] = HttpResponse(
            200, [{"id": i, "is_processing": False} for i in range(slots)]
        )

    def agent_healthy(self, run_id: str) -> None:
        """The Agent Server answers /health whenever it runs in the sandbox."""
        url = f"http://dgx-autonomy-agent-{run_id}:{self.settings.agent_port}/health"

        def health(method: str, body: Any) -> HttpResponse:
            if self.runtime.agent_server_up(run_id):
                return HttpResponse(200, "OK")
            return HttpResponse(0, error="connection refused")

        self.http.routes[url] = health

    def planner_healthy(self, plan_id: str) -> None:
        """The planner's Agent Server answers /health whenever its sandbox runs."""
        name = planner_container_name(plan_id)
        url = f"http://{name}:{self.settings.agent_port}/health"

        def health(method: str, body: Any) -> HttpResponse:
            c = self.runtime.containers.get(name)
            if c is not None and c.running:
                return HttpResponse(200, "OK")
            return HttpResponse(0, error="connection refused")

        self.http.routes[url] = health

    def restart_controller(self) -> Controller:
        """A new controller process over the same state, containers and Agent Server:
        what compose brings back after the old one was killed."""
        self.controller = _controller(self)
        return self.controller

    def running_run(self, **launch: Any) -> str:
        """A run that went launched -> running, with its Agent Server in the sandbox."""
        args = {"brief_text": "Serve a static page on port 3000.\n", "budget_hours": 1, **launch}
        run_id = str(self.controller.handle("launch", args)["run_id"])
        self.inference_ready()
        self.agent_healthy(run_id)
        self.controller.reconcile_once()
        run = self.state.get_run(run_id)
        assert run is not None and run.phase == "running", run
        return run_id


BOOT_ID = "4f1c2d9e-0000-4000-8000-000000000001"


def load_egress_policy(settings: Settings, boot_id: str = BOOT_ID) -> None:
    """What host/dgx-autonomy-egress writes when it loads the rules on this boot."""
    settings.boot_id_file.parent.mkdir(parents=True, exist_ok=True)
    settings.boot_id_file.write_text(f"{BOOT_ID}\n")
    settings.egress_marker.parent.mkdir(parents=True, exist_ok=True)
    settings.egress_marker.write_text(
        json.dumps({"table": "inet dgx_autonomy", "boot_id": boot_id, "rules_sha256": "ab"})
    )


def make_harness(settings: Settings) -> Harness:
    """A Controller wired to fakes, with every side effect observable."""
    load_egress_policy(settings)

    clock = FakeClock()
    state = StateStore(":memory:")
    trace: list[str] = []
    runtime = FakeRuntime(settings, trace)
    http = FakeHttp()
    conversation = FakeConversation(trace=trace)
    chowned: list[tuple[str, int, int]] = []
    runtime.on_agent_boot.append(conversation.server_restarted)
    h = Harness(settings, clock, state, runtime, http, conversation, None, chowned, trace)  # type: ignore[arg-type]
    h.controller = _controller(h)
    return h


def _controller(h: Harness) -> Controller:
    from dgx_autonomy.config import load_models
    from dgx_autonomy.inference import InferenceManager

    return Controller(
        settings=h.settings,
        state=h.state,
        catalog=load_models(h.settings.models_file),
        runtime=h.runtime,
        # The host's memory is not the test's business (test_runtime.py covers the guard).
        inference=InferenceManager(h.settings, h.runtime, h.http, available=lambda: None),
        conversations=h.conversation,
        http=h.http,
        clock=h.clock,
        chown=lambda path, uid, gid: h.chowned.append((str(path), uid, gid)),
        # Waiting advances fake time, so bounded waits end instantly in tests.
        sleep=lambda seconds: h.clock.advance(seconds=seconds),
        crash=lambda: h.trace.append("controller crashed"),
    )
