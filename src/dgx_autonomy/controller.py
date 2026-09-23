"""The trusted controller: lifecycle state, reconciliation, and the control API handler.

A command (for example `launch`) only persists the requested transition and wakes the
reconcile loop. The loop does the work, one idempotent step per tick:

    launched: ensure inference -> ensure workspace -> start conversation -> running
    running:  observe the conversation; FINISHED -> finished, ERROR/STUCK -> failed;
              serve start_demo requests
    stopping: end agent execution (retried until it is verified)

Every external side effect is preceded by a durable operation intent. A step that is
not ready yet (the model still loading, the Agent Server still booting) leaves the
intent in place and is re-checked on the next tick, so no SDK or HTTP call blocks
the loop for long.

The deadline is enforced by `deadline.DeadlineWatchdog` on its own thread, not by
this loop, so an SDK call that hangs here cannot delay it. Deadline and `stop` share
one sequence, `stop_agent`: persist the stop, pause the conversation and cancel its
LLM call, wait (bounded) for quiet, end every agent process in the sandbox, verify
none is left, and keep (or relaunch) the demo.

Crashes and restarts. Only the controller holding the state store's writer lock
operates it. On startup (`reconcile_on_start`) it first expires every run whose
deadline passed while it, or the DGX, was down, then inspects what each interrupted
operation left behind before repeating it. After that, a launched or running run
whose llama-server or sandbox is not running (the DGX restarted, a container
crashed) is brought back by a recovery: durable intent first, then llama-server,
the sandbox and its Agent Server, the recorded demo, and finally the run's one
conversation, which is resumed with a note about what happened. Nothing replays an
agent tool action, and no recovery moves the deadline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import socket
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .agent_files import AgentFileError, read_json
from .config import ConfigError, ModelCatalog, Settings, load_models
from .deadline import DeadlineWatchdog
from .egress import EgressPolicyError, check_policy, probe, probe_targets
from .inference import InferenceError, InferenceManager
from .openhands_adapter import (
    ConversationError,
    conversation_id_for,
    persisted_events,
    persisted_status,
)
from .ports import (
    Clock,
    ContainerState,
    ConversationPort,
    ConversationRequest,
    ConversationSnapshot,
    DemoSpec,
    DemoStatus,
    EventSummary,
    EvidenceMessage,
    HttpClient,
    LlmEndpoint,
    RuntimePort,
    SandboxProcess,
    ServerRef,
    WorkspaceHandle,
    WorkspaceSpec,
)
from .runtime import (
    AGENT_BRIEF_PATH,
    AGENT_PROJECT_DIR,
    LABEL_OP,
    DockerError,
    agent_container_name,
)
from .state import (
    Demo,
    Operation,
    OperationKind,
    Recovery,
    Run,
    StateStore,
    WriterLockError,
    operation_id,
)

log = logging.getLogger("dgx_autonomy.controller")

FAILED_CONVERSATION_STATUSES = frozenset({"error", "stuck"})
# A conversation in one of these states before the restart had ended; recovery
# leaves it for _observe instead of resuming it.
_ENDED_CONVERSATION_STATUSES = frozenset({"finished", "error", "stuck"})
# After a restart: the Agent Server turns an interrupted RUNNING conversation into
# ERROR (SDK 1.49.4, EventService.start); paused and idle need a run as well.
_RESUMABLE_CONVERSATION_STATUSES = frozenset({"error", "paused", "idle"})
FAULT_TARGETS = ("controller", "sandbox", "inference")
MAX_BRIEF_BYTES = 256 * 1024
MAX_DEMO_REQUEST_BYTES = 16 * 1024
MAX_DEMO_COMMAND = 4096
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Processes that are the Agent Server itself rather than something a tool started.
_SERVER_MARKERS = ("openhands-agent-server", ".openvscode-server")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RequestError(ValueError):
    """A control request is invalid; the message goes back to the CLI as-is."""


class RecoveryFailed(RuntimeError):
    """This recovery attempt cannot finish; a later one retries after a backoff."""


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def brief(self) -> Path:
        return self.root / "brief.md"

    @property
    def agent_dir(self) -> Path:
        """Mounted at /workspace in the agent container: project + SDK conversations."""
        return self.root / "agent"

    @property
    def project_dir(self) -> Path:
        return self.agent_dir / "project"

    @property
    def conversations_dir(self) -> Path:
        return self.agent_dir / "conversations"

    @property
    def control_dir(self) -> Path:
        """Controller-owned; read-only at /dgx-control in the sandbox."""
        return self.root / "control"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"


@dataclass(frozen=True)
class StopEvidence:
    """What ending agent execution found and did. Persisted as JSON on the run.

    `after_pause` lists the agent processes still present after the pause and the
    bounded wait, before anything was killed: whether pausing the conversation
    ended the tools' processes is read from here, not assumed.
    `failed` means cessation could not be shown; the run then stays `stopping`.
    """

    run_id: str
    reason: str
    started_at: str
    finished_at: str
    container_running: bool
    conversation_status: str | None
    quiescent: bool | None
    after_pause: list[dict[str, Any]]
    tool_processes_after_pause: int
    survivors: list[dict[str, Any]] | None
    sandbox_restarted: bool
    inference_busy_slots: int | None
    demo: dict[str, Any] | None
    failed: bool
    notes: list[str] = field(default_factory=list)


def _atomic_write(path: Path, data: str, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def agent_message(brief: str) -> str:
    return (
        "You are working unattended; nobody will answer questions until the work is done.\n"
        f"Your project directory is {AGENT_PROJECT_DIR}. The agreed brief is below and is also"
        f" available read-only at {AGENT_BRIEF_PATH}.\n"
        "To serve the app for the operator, use the start_demo tool: it is the only way to"
        " keep the app running after your work ends.\n"
        "Complete the brief, verify the result yourself, then finish.\n\n"
        "--- BRIEF ---\n"
        f"{brief.strip()}\n"
    )


def recovery_notice(cause: str, *, sandbox_restarted: bool, demo_relaunched: bool) -> str:
    """What the resumed conversation is told about the interruption."""
    if sandbox_restarted:
        lines = [
            f"Your sandbox was restarted while you were working ({cause}).",
            "Every process you had started has ended: terminal sessions, servers and"
            " background jobs.",
            f"Your files in {AGENT_PROJECT_DIR} are as they were. A tool call that was in"
            " progress did not complete; check its effects before repeating it.",
        ]
    else:
        lines = [
            f"The model server was restarted while you were working ({cause}).",
            "Your last step may have failed because of that.",
        ]
    if demo_relaunched:
        lines.append("The demo was relaunched with the command you gave start_demo.")
    lines.append("Check the state of the project and continue with the brief.")
    return "\n".join(lines)


def _describe(container: ContainerState | None) -> str:
    if container is None:
        return "missing"
    return f"{container.status} (exit code {container.exit_code})"


def _new_run_id(now: datetime) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _proc_view(p: SandboxProcess) -> dict[str, Any]:
    return {"pid": p.pid, "sid": p.sid, "state": p.state, "cmd": p.cmd}


def _is_server_process(p: SandboxProcess) -> bool:
    return any(marker in p.cmd for marker in _SERVER_MARKERS)


def _clip(text: str, limit: int = 1500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def demo_request_problem(command: object, port: object, demo_port: int) -> str | None:
    """Why a start_demo request is refused, or None when it is acceptable."""
    if not isinstance(command, str) or not command.strip():
        return "command must be a non-empty string"
    if len(command) > MAX_DEMO_COMMAND:
        return f"command is longer than {MAX_DEMO_COMMAND} characters"
    if "\x00" in command:
        return "command contains a NUL byte"
    if isinstance(port, bool) or not isinstance(port, int) or port != demo_port:
        return (
            f"the demo must listen on port {demo_port}; it is the only port published to "
            f"the operator (got {port!r})"
        )
    return None


class Controller:
    def __init__(
        self,
        *,
        settings: Settings,
        state: StateStore,
        catalog: ModelCatalog,
        runtime: RuntimePort,
        inference: InferenceManager,
        conversations: ConversationPort,
        http: HttpClient,
        clock: Clock,
        chown: Callable[[Path, int, int], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        crash: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._catalog = catalog
        self._runtime = runtime
        self._inference = inference
        self._conversations = conversations
        self._http = http
        self._clock = clock
        self._chown = chown if chown is not None else _default_chown
        self._sleep = sleep
        self._crash = crash if crash is not None else _crash_now
        self._wake = threading.Event()
        self._reconcile_lock = threading.Lock()
        self._snapshots: dict[str, ConversationSnapshot] = {}
        self._stop_locks: dict[str, threading.Lock] = {}
        self._stop_locks_guard = threading.Lock()
        self._bad_requests: dict[str, str] = {}

    # --- paths & secrets -----------------------------------------------------------

    def paths(self, run_id: str) -> RunPaths:
        return RunPaths(self._settings.runs_dir / run_id)

    def _prepare_run_dirs(self, run_id: str) -> RunPaths:
        """Idempotent. The agent owns its workspace; the controller owns everything else."""
        p = self.paths(run_id)
        p.project_dir.mkdir(parents=True, exist_ok=True)
        uid, gid = self._settings.agent_uid, self._settings.agent_gid
        for d in (p.agent_dir, p.project_dir):
            os.chmod(d, 0o755)
            self._chown(d, uid, gid)
        p.secrets_dir.mkdir(mode=0o700, exist_ok=True)
        for name in ("session_api_key", "secret_key"):
            f = p.secrets_dir / name
            if not f.exists():
                _atomic_write(f, secrets.token_urlsafe(32), 0o600)
        p.control_dir.mkdir(exist_ok=True)
        os.chmod(p.control_dir, 0o755)
        # Never reset an existing mode: after a stop it must stay demo-only.
        if not (p.control_dir / "mode").exists():
            _atomic_write(p.control_dir / "mode", "agent\n", 0o644)
        return p

    def _write_control(self, run_id: str, name: str, data: str) -> None:
        control = self.paths(run_id).control_dir
        control.mkdir(parents=True, exist_ok=True)
        _atomic_write(control / name, data, 0o644)

    def _server_ref(self, run_id: str) -> ServerRef:
        key = (self.paths(run_id).secrets_dir / "session_api_key").read_text().strip()
        url = f"http://{agent_container_name(run_id)}:{self._settings.agent_port}"
        return ServerRef(url=url, api_key=key)

    # --- commands (called from the control socket) ---------------------------------

    def handle(self, op: str, args: Mapping[str, Any]) -> Any:
        handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "ping": lambda a: {"pong": True, "controller": self._holder_view()},
            "launch": self.launch,
            "status": self.status,
            "runs": lambda a: [self._run_view(r) for r in self._state.list_runs()],
            "logs": self.logs,
            "stop": self.stop,
            "processes": self.processes,
            "inference.ensure": self.inference_ensure,
            "inference.status": self.inference_status,
            "inference.request": self.inference_request,
            "inference.stop": self.inference_stop,
            "network.policy": lambda a: check_policy(self._settings, self._runtime).as_dict(),
            "network.probe": self.network_probe,
            "fault.inject": self.fault_inject,
        }
        handler = handlers.get(op)
        if handler is None:
            raise RequestError(f"unknown operation {op!r}")
        return handler(args)

    def launch(self, args: Mapping[str, Any]) -> dict[str, Any]:
        brief = args.get("brief_text")
        if not isinstance(brief, str) or not brief.strip():
            raise RequestError("launch needs a non-empty brief_text")
        if len(brief.encode()) > MAX_BRIEF_BYTES:
            raise RequestError(f"brief is larger than {MAX_BRIEF_BYTES} bytes")
        try:
            budget = float(args.get("budget_hours", self._settings.max_budget_hours))
        except (TypeError, ValueError):
            raise RequestError("budget_hours must be a number") from None
        if not 0 < budget <= self._settings.max_budget_hours:
            raise RequestError(
                f"budget_hours must be in (0, {self._settings.max_budget_hours:g}], got {budget:g}"
            )
        try:
            model = self._catalog.get(args.get("model_key"))
        except ConfigError as exc:
            raise RequestError(str(exc)) from None

        now = self._clock.now()
        run_id = _new_run_id(now)
        paths = self._prepare_run_dirs(run_id)
        _atomic_write(paths.brief, brief, 0o644)
        run = self._state.create_run(
            run_id=run_id,
            model_key=model.key,
            launched_at=now,
            deadline_at=now + timedelta(hours=budget),
            brief_path=str(paths.brief),
        )
        log.info(
            "launched %s (model %s, deadline %s, brief from %s)",
            run.id,
            model.key,
            run.deadline_at,
            args.get("brief_source", "?"),
        )
        self._wake.set()
        return {"run_id": run.id, "deadline_at": run.deadline_at.isoformat()}

    def _resolve(self, args: Mapping[str, Any]) -> Run:
        run_id = args.get("run_id")
        run = self._state.get_run(str(run_id)) if run_id else self._state.latest_run()
        if run is None:
            raise RequestError(f"no run {run_id}" if run_id else "no runs yet")
        return run

    def status(self, args: Mapping[str, Any]) -> dict[str, Any]:
        run = self._resolve(args)
        view = self._run_view(run)
        view["stop_requested"] = run.stop_requested
        snap = None
        if run.phase != "stopped":
            snap = self._snapshots.get(run.id)
            if snap is None and run.conversation_id is not None:
                snap = self._try_inspect(run)
        if snap is not None:
            view["conversation_status"] = snap.status
            view["last_event"] = asdict(snap.last_event) if snap.last_event else None
        elif run.conversation_id is not None and run.terminal:
            # The Agent Server is gone (stopped, or not brought back after a restart);
            # read what the SDK persisted.
            conv_dir = self.paths(run.id).conversations_dir
            saved = persisted_status(conv_dir, run.conversation_id)
            # An Agent Server killed mid-step never saved its last status; `running`
            # on disk for an ended run only says the stop cut it off.
            view["conversation_status"] = "interrupted" if saved == "running" else saved
            last = persisted_events(conv_dir, run.conversation_id, 0, 1, last=True)
            view["last_event"] = asdict(last[0]) if last else None
        else:
            view["conversation_status"] = None
            view["last_event"] = None
        view["operations"] = [
            {"kind": o.kind, "status": o.status, "resource_id": o.resource_id, "error": o.error}
            for o in self._state.operations(run.id)
        ]
        view["demo"] = self._demo_view(run)
        view["stop_evidence"] = json.loads(run.stop_evidence) if run.stop_evidence else None
        view["recoveries"] = [
            {
                "n": r.n,
                "cause": r.cause,
                "status": r.status,
                "status_before": r.status_before,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "error": r.error,
                "steps": r.steps,
            }
            for r in self._state.recoveries(run.id)
        ]
        view["containers"] = self._containers_view(run)
        return view

    def _containers_view(self, run: Run) -> list[dict[str, Any]] | None:
        """The run's labeled containers as Docker reports them (None if it cannot)."""
        try:
            snapshot = self._runtime.inspect(run.id)
        except DockerError as exc:
            log.warning("%s: cannot list containers: %s", run.id, exc)
            return None
        return [
            {
                "name": c.name,
                "id": c.id,
                "running": c.running,
                "status": c.status,
                "op": c.labels.get(LABEL_OP),
            }
            for c in snapshot.containers
        ]

    def logs(self, args: Mapping[str, Any]) -> dict[str, Any]:
        run = self._resolve(args)
        since = max(0, int(args.get("since", 0)))
        limit = min(500, max(1, int(args.get("limit", 200))))
        if run.conversation_id is None:
            return {"run_id": run.id, "events": [], "next": since, "phase": run.phase}
        events: list[EventSummary] | None = None
        if run.phase != "stopped":
            try:
                events = list(
                    self._conversations.events(
                        self._server_ref(run.id), run.conversation_id, since, limit
                    )
                )
            except ConversationError as exc:
                log.info("%s: Agent Server unavailable (%s); reading persisted events", run.id, exc)
        if events is None:
            events = persisted_events(
                self.paths(run.id).conversations_dir, run.conversation_id, since, limit
            )
        return {
            "run_id": run.id,
            "events": [asdict(e) for e in events],
            "next": since + len(events),
            "phase": run.phase,
        }

    def stop(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """End agent execution now; keep the demo. Returns the run's status."""
        run = self._resolve(args)
        if run.terminal:
            view = self.status({"run_id": run.id})
            view["message"] = f"run already {run.phase}; nothing to stop"
            return view
        self._state.request_stop(run.id, "stopped")
        log.warning("%s: stop requested by the operator", run.id)
        self.stop_agent(run.id)
        return self.status({"run_id": run.id})

    def processes(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The sandbox's processes, classified by the in-sandbox helper (diagnostic)."""
        run = self._resolve(args)
        demo = self._state.get_demo(run.id)
        try:
            procs = self._runtime.sandbox_processes(run.id, demo.session_id if demo else None)
        except DockerError as exc:
            raise RequestError(str(exc)) from None
        return {
            "run_id": run.id,
            "sandbox_running": procs is not None,
            "processes": [asdict(p) for p in procs or ()],
        }

    def inference_ensure(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            model = self._catalog.get(args.get("model_key"))
            self._inference.ensure(model)
        except (ConfigError, InferenceError, DockerError) as exc:
            raise RequestError(str(exc)) from None
        return self._inference.status().as_dict()

    def inference_status(self, args: Mapping[str, Any]) -> dict[str, Any]:
        view = self._inference.status().as_dict()
        view["reservation"] = self._reservation_state()
        return view

    def _reservation_state(self) -> str:
        """held | reserving | releasing | none, from the host helper's record."""
        try:
            raw = json.loads(self._settings.reservation_record.read_text())
        except FileNotFoundError:
            return "none"
        except (OSError, ValueError) as exc:
            return f"unreadable ({exc})"
        return str(raw.get("state", "unknown")) if isinstance(raw, dict) else "unknown"

    def inference_stop(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Remove the owned llama-server (before `release`). Refused while a run needs it."""
        active = [r.id for r in self._state.active_runs()]
        if active:
            raise RequestError(
                f"run {', '.join(active)} still uses the model; `dgx-autonomy stop` it first"
            )
        try:
            removed = self._runtime.remove_container(self._settings.inference_name)
        except DockerError as exc:
            raise RequestError(str(exc)) from None
        log.info("owned llama-server %s", "removed" if removed else "was not running")
        return {"removed": removed, **self.inference_status({})}

    def network_probe(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Connect to `targets` from the agent's network position (diagnostic)."""
        try:
            targets = probe_targets(args.get("targets"))
            timeout = float(args.get("timeout_s", 5.0))
        except (TypeError, ValueError) as exc:
            raise RequestError(str(exc)) from None
        if not 0 < timeout <= 30:
            raise RequestError("timeout_s must be in (0, 30]")
        try:
            result = probe(self._settings, self._runtime, targets, timeout)
        except DockerError as exc:
            raise RequestError(str(exc)) from None
        result["policy"] = check_policy(self._settings, self._runtime).as_dict()
        return result

    def fault_inject(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Crash one component on purpose, as the recovery tests do (diagnostic).

        target: `controller` (this process dies at once, without cleanup; compose
        restarts it), `sandbox` (SIGKILL the run's agent container) or `inference`
        (SIGKILL the owned llama-server). `confirm` must repeat the target.
        """
        target = args.get("target")
        if target not in FAULT_TARGETS:
            raise RequestError(f"target must be one of {', '.join(FAULT_TARGETS)}")
        if args.get("confirm") != target:
            raise RequestError(f"fault.inject needs confirm={target!r}")
        if target == "controller":
            log.warning("fault injection: the controller crashes now")
            # After the reply is on its way.
            threading.Timer(0.5, self._crash).start()
            return {"target": target, "crashing": True}
        if target == "sandbox":
            run = self._resolve(args)
            if run.terminal:
                raise RequestError(f"run {run.id} is {run.phase}; its sandbox only serves the demo")
            name = agent_container_name(run.id)
        else:
            name = self._settings.inference_name
        try:
            killed = self._runtime.kill_container(name)
        except DockerError as exc:
            raise RequestError(str(exc)) from None
        log.warning("fault injection: %s %s", name, "killed" if killed else "was not running")
        return {"target": target, "container": name, "killed": killed}

    def inference_request(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            res = self._inference.request(
                str(args.get("method", "GET")), str(args.get("path", "")), args.get("body")
            )
        except InferenceError as exc:
            raise RequestError(str(exc)) from None
        return {"status": res.status, "body": res.body, "error": res.error}

    def _run_view(self, run: Run) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "phase": run.phase,
            "outcome": run.outcome,
            "model_key": run.model_key,
            "launched_at": run.launched_at.isoformat(),
            "deadline_at": run.deadline_at.isoformat(),
            "brief_path": run.brief_path,
            "conversation_id": run.conversation_id,
            "workspace_dir": str(self.paths(run.id).project_dir),
        }

    def _demo_view(self, run: Run) -> dict[str, Any] | None:
        demo = self._state.get_demo(run.id)
        if demo is None:
            return None
        live = self._demo_live(demo)
        return {
            "command": demo.command,
            "port": demo.port,
            "host_port": demo.host_port,
            "url": f"http://127.0.0.1:{demo.host_port}/",
            "state": demo.state,
            "message": demo.message,
            "session_id": demo.session_id,
            "alive": live.alive if live else None,
            "listening": live.listening if live else None,
        }

    def _demo_live(self, demo: Demo) -> DemoStatus | None:
        if demo.session_id is None:
            return None
        try:
            return self._runtime.demo_status(demo.run_id, demo.session_id, demo.port)
        except DockerError as exc:
            log.warning("%s: cannot check the demo: %s", demo.run_id, exc)
            return None

    # --- the writer lock and startup ----------------------------------------------

    def _boot_id(self) -> str:
        try:
            return self._settings.boot_id_file.read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def acquire_writer(self) -> None:
        """Become the only controller operating the state. Raises WriterLockError."""
        boot_id = self._boot_id()
        previous = self._state.acquire_writer(
            pid=os.getpid(), host=socket.gethostname(), boot_id=boot_id, now=self._clock.now()
        )
        if previous is None:
            log.info("writer lock acquired")
        else:
            log.warning(
                "writer lock taken over from pid %s on %s (held since %s); %s",
                previous.pid,
                previous.host,
                previous.acquired_at.isoformat(),
                "the controller restarted"
                if previous.boot_id == boot_id
                else "the DGX restarted since",
            )

    def _holder_view(self) -> dict[str, Any] | None:
        holder = self._state.writer_holder()
        if holder is None:
            return None
        return {
            "token": holder.token,
            "pid": holder.pid,
            "host": holder.host,
            "boot_id": holder.boot_id,
            "acquired_at": holder.acquired_at.isoformat(),
        }

    def reconcile_on_start(self) -> dict[str, list[str]]:
        """Once, with the writer lock held, before the control socket and the loop start.

        1. A run whose deadline passed while the controller (or the DGX) was down is
           expired: agent execution ends, the demo is kept, nothing is resumed.
        2. Every operation still `intended` is inspected before it is repeated. A
           running resource it created is adopted; otherwise the step creates or
           starts it again. Either way the attempt's timeouts restart now.
        3. An open recovery restarts from inspection, not from its recorded steps.
        4. The retained demos of runs that had already ended come back (demo-only).

        The reconcile loop does the rest: it brings back a llama-server or sandbox
        that is down, and attaches to the run's one conversation.
        """
        now = self._clock.now()
        expired = self.deadline_watchdog().check_once()
        retried: list[str] = []
        for run in self._state.active_runs():
            if run.phase not in ("launched", "running"):
                continue
            for op in self._state.operations(run.id):
                if op.status == "intended":
                    self._retry_intent(run, op, now)
                    retried.append(op.id)
            rec = self._state.open_recovery(run.id)
            if rec is not None:
                self._state.retry_recovery(rec.id, now)
                log.warning("%s: recovery #%d was interrupted; starting it over", run.id, rec.n)
                retried.append(rec.id)
        summary = {
            "expired": expired,
            "retried": retried,
            "resuming": [r.id for r in self._state.active_runs() if r.phase != "stopping"],
            "demos": self._restore_retained_demos(),
        }
        log.info("startup reconciliation: %s", summary)
        self._wake.set()
        return summary

    def _restore_retained_demos(self) -> list[str]:
        """Bring back the demos of runs that had ended before the restart.

        A sandbox that went down (with the DGX, or on its own) is started again
        demo-only and the recorded demo command is relaunched; the agent does not come
        back. A sandbox that no longer exists (the operator removed it) stays gone.
        """
        restored = []
        for run in self._state.list_runs():
            if not run.terminal:
                continue
            demo = self._state.get_demo(run.id)
            if demo is None or demo.command is None or demo.state not in ("starting", "running"):
                continue
            try:
                container = self._runtime.inspect_container(agent_container_name(run.id))
                if container is None or container.running:
                    continue
                # Before the start: the supervisor must not bring the Agent Server back.
                self._write_control(run.id, "mode", "demo-only\n")
                op = self._state.get_operation(run.id, "workspace.create")
                self._ensure_workspace(
                    run.id, op.id if op else operation_id(run.id, "workspace.create")
                )
                handle = self._runtime.ensure_demo(DemoSpec(run.id, demo.command, demo.port))
            except (DockerError, EgressPolicyError) as exc:
                log.warning("%s: cannot bring the retained demo back: %s", run.id, exc)
                continue
            demo = self._state.set_demo_state(
                run.id,
                state="starting",
                message="relaunched from the recorded spec after the sandbox restarted",
                now=self._clock.now(),
                session_id=handle.session_id,
            )
            self._publish_demo_status(demo)
            log.info("%s (%s): retained demo relaunched demo-only", run.id, run.phase)
            restored.append(run.id)
        return restored

    def _observe_resource(self, run: Run, op: Operation) -> ContainerState | None:
        """What an interrupted attempt of `op` left behind, found by inspection."""
        if op.kind == "inference.start":
            return self._runtime.inspect_container(self._settings.inference_name)
        if op.kind == "workspace.create":
            found = [
                c for c in self._runtime.inspect(run.id).containers
                if c.labels.get(LABEL_OP) == op.id
            ]  # fmt: skip
            return found[0] if found else None
        # conversation.start: the step asks the Agent Server before creating anything.
        return None

    def _retry_intent(self, run: Run, op: Operation, now: datetime) -> None:
        try:
            observed = self._observe_resource(run, op)
        except DockerError as exc:
            log.warning("%s: cannot inspect %s: %s", run.id, op.kind, exc)
            observed = None
        live = observed if observed is not None and observed.running else None
        self._state.retry_operation(op.id, now, resource_id=live.id if live else None)
        if live is not None:
            what = f"adopting running {live.name}"
        elif observed is not None:
            what = f"{observed.name} is {observed.status}; starting it again"
        else:
            what = "retrying"
        log.warning("%s: %s was interrupted; %s", run.id, op.kind, what)

    # --- reconciliation ------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def deadline_watchdog(self) -> DeadlineWatchdog:
        return DeadlineWatchdog(
            state=self._state,
            clock=self._clock,
            on_expired=self.expire,
            interval_s=self._settings.watchdog_interval_s,
        )

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.reconcile_once()
            self._wake.wait(self._settings.poll_seconds)
            self._wake.clear()

    def reconcile_once(self) -> None:
        with self._reconcile_lock:
            for run in self._state.active_runs():
                try:
                    self._reconcile_run(run)
                except Exception:
                    # Keep the loop alive; the next tick retries from durable state.
                    log.exception("reconcile %s failed", run.id)
            # A demo relaunched for an ended run (by a stop, or at startup) still has
            # to be seen listening; ended runs are not reconciled otherwise.
            for demo in self._state.demos_in_state("starting"):
                ended = self._state.get_run(demo.run_id)
                if ended is None or not ended.terminal or demo.session_id is None:
                    continue
                try:
                    self._check_demo_start(ended, demo)
                except Exception:
                    log.exception("demo check %s failed", ended.id)

    def _phase_is(self, run_id: str, *phases: str) -> bool:
        """Re-read the phase: a stop may have moved the run on during a slow step."""
        run = self._state.get_run(run_id)
        return run is not None and run.phase in phases

    def _reconcile_run(self, run: Run) -> None:
        if run.phase == "stopping":
            # Another thread (the watchdog, a `stop` command) may be on it already.
            self.stop_agent(run.id, block=False)
            return
        if not self._ensure_live(run):
            return
        if run.phase == "launched":
            if not self._step_inference(run) or not self._phase_is(run.id, "launched"):
                return
            server = self._step_workspace(run)
            if server is None or not self._phase_is(run.id, "launched"):
                return
            if not self._step_conversation(run, server):
                return
            if not self._state.try_set_phase(run.id, "running"):
                return
            log.info("%s is running", run.id)
            run = self._state.get_run(run.id) or run
        if run.phase == "running" and run.conversation_id is not None:
            self._observe(run)
            if self._phase_is(run.id, "running"):
                self._step_demo(run)

    def _fail(self, run: Run, op: Operation, error: str) -> None:
        log.error("%s: %s failed: %s", run.id, op.kind, error)
        self._state.fail_operation(op.id, error)
        self._state.try_set_phase(run.id, "failed")

    def _intent(self, run: Run, kind: OperationKind) -> Operation | None:
        """The intent for `kind`, or None when the run already failed at this step."""
        op = self._state.record_intent(run.id, kind, self._clock.now())
        if op.status == "failed":
            self._state.try_set_phase(run.id, "failed")
            return None
        return op

    def _timed_out(self, op: Operation, limit_s: float) -> bool:
        return (self._clock.now() - op.attempted_at).total_seconds() > limit_s

    def _done(self, run_id: str, kind: OperationKind) -> Operation | None:
        op = self._state.get_operation(run_id, kind)
        return op if op is not None and op.status == "done" else None

    def _step_inference(self, run: Run) -> bool:
        op = self._intent(run, "inference.start")
        if op is None:
            return False
        if op.status == "done":
            return True
        resource = op.resource_id
        if resource is None:
            # First attempt for this operation: start (or reuse) the container once.
            # Afterwards a stopped container means it crashed, not that it needs a start.
            try:
                container = self._inference.ensure(self._catalog.get(run.model_key))
            except (InferenceError, DockerError) as exc:
                self._fail(run, op, str(exc))
                return False
            resource = container.id
            self._state.note_resource(op.id, resource)
        status = self._inference.status()
        if status.ready:
            self._state.complete_operation(op.id, resource)
            return True
        if status.container != "running":
            self._fail(run, op, f"llama-server is {status.container}: {status.detail}")
        elif self._timed_out(op, self._settings.inference_load_timeout_s):
            self._fail(run, op, f"llama-server not ready in time ({status.health})")
        return False

    def _workspace_spec(self, run_id: str, op_id: str) -> WorkspaceSpec:
        paths = self._prepare_run_dirs(run_id)
        demo = self._state.reserve_demo(
            run_id,
            port=self._settings.demo_port,
            host_port_base=self._settings.demo_host_port_base,
            host_port_count=self._settings.demo_host_port_count,
            now=self._clock.now(),
        )
        return WorkspaceSpec(
            run_id=run_id,
            op_id=op_id,
            agent_dir=paths.agent_dir,
            brief_file=paths.brief,
            session_api_key=(paths.secrets_dir / "session_api_key").read_text().strip(),
            secret_key=(paths.secrets_dir / "secret_key").read_text().strip(),
            control_dir=paths.control_dir,
            demo_host_port=demo.host_port,
        )

    def _ensure_workspace(self, run_id: str, op_id: str) -> WorkspaceHandle:
        """Create or restart the sandbox, only inside the host's egress policy."""
        if self._settings.require_egress_policy:
            policy = check_policy(self._settings, self._runtime)
            if not policy.ok:
                raise EgressPolicyError(
                    "refusing to start the agent sandbox: " + "; ".join(policy.problems)
                )
        return self._runtime.ensure_workspace(self._workspace_spec(run_id, op_id))

    def _step_workspace(self, run: Run) -> ServerRef | None:
        op = self._intent(run, "workspace.create")
        if op is None:
            return None
        server = self._server_ref(run.id)
        if op.status == "done":
            return server
        name = agent_container_name(run.id)
        resource = op.resource_id
        if resource is None:
            try:
                handle = self._ensure_workspace(run.id, op.id)
            except (DockerError, EgressPolicyError) as exc:
                self._fail(run, op, str(exc))
                return None
            resource = handle.container_id
            self._state.note_resource(op.id, resource)
        if self._http.request("GET", f"{server.url}/health").ok:
            self._state.complete_operation(op.id, resource)
            return server
        container = self._runtime.inspect_container(name)
        if container is None or not container.running:
            logs = self._runtime.container_logs(name)
            self._fail(run, op, f"agent container stopped: {logs[-1500:]}")
        elif self._timed_out(op, self._settings.agent_start_timeout_s):
            self._fail(run, op, "Agent Server did not become healthy in time")
        return None

    def _step_conversation(self, run: Run, server: ServerRef) -> bool:
        op = self._intent(run, "conversation.start")
        if op is None:
            return False
        if op.status == "done" and run.conversation_id is not None:
            return True
        model = self._catalog.get(run.model_key)
        request = ConversationRequest(
            server=server,
            conversation_id=run.conversation_id or conversation_id_for(run.id),
            working_dir=AGENT_PROJECT_DIR,
            llm=LlmEndpoint(
                model=model.key,
                base_url=f"{self._settings.inference_url}/v1",
                max_input_tokens=model.ctx,
            ),
            message=agent_message(Path(run.brief_path).read_text()),
        )
        try:
            cid = self._conversations.start(request)
        except ConversationError as exc:
            # The Agent Server answered health but refused; retry until the start timeout.
            if not self._phase_is(run.id, "launched"):
                return False
            if self._timed_out(op, self._settings.agent_start_timeout_s):
                self._fail(run, op, str(exc))
            else:
                log.warning("%s: conversation start will be retried: %s", run.id, exc)
            return False
        # Recorded even if a stop arrived meanwhile: logs need the id.
        self._state.set_conversation_id(run.id, cid)
        self._state.complete_operation(op.id, cid)
        return True

    def _try_inspect(self, run: Run) -> ConversationSnapshot | None:
        if run.conversation_id is None:
            return None
        try:
            return self._conversations.inspect(self._server_ref(run.id), run.conversation_id)
        except (ConversationError, OSError) as exc:
            log.warning("%s: cannot inspect conversation: %s", run.id, exc)
            return None

    def _observe(self, run: Run) -> None:
        snap = self._try_inspect(run)
        if snap is None:
            return
        self._snapshots[run.id] = snap
        if snap.status == "finished":
            if not self._state.try_set_phase(run.id, "finished"):
                return
            log.info("%s finished", run.id)
        elif snap.status in FAILED_CONVERSATION_STATUSES:
            if not self._state.try_set_phase(run.id, "failed"):
                return
            log.warning("%s conversation ended %s", run.id, snap.status)
        else:
            return
        self._publish_project(run.id)

    # --- recovery ------------------------------------------------------------------

    def _ensure_live(self, run: Run) -> bool:
        """True when what the run has started so far is up.

        Otherwise it brings the run back (a recovery) and returns False until that is
        done, so nothing else happens to the run meanwhile.
        """
        rec = self._state.open_recovery(run.id)
        if rec is None:
            cause = self._down(run)
            if cause is None:
                return True
            if not self._recovery_due(run.id):
                return False
            rec = self._state.begin_recovery(
                run.id, cause=cause, status_before=self._status_before(run), now=self._clock.now()
            )
            log.warning(
                "%s: %s; recovery #%d (conversation was %s)",
                run.id,
                cause,
                rec.n,
                rec.status_before,
            )
        return self._recover(run, rec)

    def _down(self, run: Run) -> str | None:
        """Why the run needs a recovery, or None when its started resources are up."""
        causes = []
        if self._done(run.id, "inference.start"):
            c = self._runtime.inspect_container(self._settings.inference_name)
            if c is None or not c.running:
                causes.append(f"llama-server is {_describe(c)}")
        if self._done(run.id, "workspace.create"):
            c = self._runtime.inspect_container(agent_container_name(run.id))
            if c is None or not c.running:
                causes.append(f"the agent sandbox is {_describe(c)}")
        return "; ".join(causes) or None

    def _recovery_due(self, run_id: str) -> bool:
        """Back off: each recovery within the window waits twice as long as the last."""
        now = self._clock.now()
        window_start = now - timedelta(seconds=self._settings.recovery_window_s)
        recent = [r for r in self._state.recoveries(run_id) if r.started_at >= window_start]
        if not recent:
            return True
        delay = min(
            self._settings.recovery_backoff_s * 2 ** (len(recent) - 1),
            self._settings.recovery_backoff_max_s,
        )
        last = recent[-1]
        return now >= (last.finished_at or last.started_at) + timedelta(seconds=delay)

    def _conversation_ref(self, run: Run) -> str | None:
        """The run's one conversation, if it was (or may have been) created."""
        if run.conversation_id is not None:
            return run.conversation_id
        if self._state.get_operation(run.id, "conversation.start") is not None:
            return conversation_id_for(run.id)
        return None

    def _status_before(self, run: Run) -> str | None:
        """The conversation's status as the SDK persisted it before anything restarts.

        Read now because the restarted Agent Server rewrites an interrupted RUNNING
        conversation to ERROR. A recovery that failed passes its reading on: it may
        already have restarted the Agent Server.
        """
        previous = self._state.recoveries(run.id)
        if previous and previous[-1].status == "failed":
            return previous[-1].status_before
        cid = self._conversation_ref(run)
        if cid is None:
            return None
        return persisted_status(self.paths(run.id).conversations_dir, cid)

    def _recover(self, run: Run, rec: Recovery) -> bool:
        """One tick of a recovery: llama-server, then the sandbox and its Agent Server,
        then the demo, then the conversation. Each step inspects before it acts, so
        the next tick, or the next controller, can pick the recovery up anywhere."""
        steps = dict(rec.steps)
        try:
            if not self._recover_inference(run, rec, steps):
                return False
            if not self._phase_is(run.id, "launched", "running"):
                return False
            if not self._recover_sandbox(run, rec, steps):
                return False
            if not self._phase_is(run.id, "launched", "running"):
                return False
            self._recover_demo(run, rec, steps)
            if not self._recover_conversation(run, rec, steps):
                return False
        except (RecoveryFailed, DockerError, InferenceError, EgressPolicyError) as exc:
            self._state.finish_recovery(
                rec.id, status="failed", error=str(exc), now=self._clock.now()
            )
            log.error("%s: recovery #%d failed: %s", run.id, rec.n, exc)
            return False
        self._state.finish_recovery(rec.id, status="done", error=None, now=self._clock.now())
        log.info("%s: recovered (#%d): %s", run.id, rec.n, steps)
        return True

    def _save_steps(self, rec: Recovery, steps: dict[str, Any]) -> None:
        self._state.record_recovery_steps(rec.id, steps)

    def _waited(self, rec: Recovery, steps: dict[str, Any], key: str) -> float:
        """Seconds since `key` was first recorded in this attempt (recorded now if new)."""
        if key not in steps:
            steps[key] = self._clock.now().isoformat()
            self._save_steps(rec, steps)
        return (self._clock.now() - datetime.fromisoformat(steps[key])).total_seconds()

    def _recover_inference(self, run: Run, rec: Recovery, steps: dict[str, Any]) -> bool:
        if not self._done(run.id, "inference.start"):
            return True  # the launch step starts the model itself
        status = self._inference.status()
        if status.ready:
            return True
        if status.container != "running":
            if "inference_started" in steps:
                raise RecoveryFailed(f"llama-server stopped again: {status.detail}")
            container = self._inference.ensure(self._catalog.get(run.model_key))
            steps["inference_started"] = self._clock.now().isoformat()
            steps["inference_container"] = container.id
            self._save_steps(rec, steps)
            return False
        if self._waited(rec, steps, "inference_started") > self._settings.inference_load_timeout_s:
            raise RecoveryFailed(f"llama-server not ready in time ({status.health})")
        return False

    def _recover_sandbox(self, run: Run, rec: Recovery, steps: dict[str, Any]) -> bool:
        op = self._done(run.id, "workspace.create")
        if op is None:
            return True  # the launch step creates the sandbox itself
        name = agent_container_name(run.id)
        container = self._runtime.inspect_container(name)
        if container is None or not container.running:
            if "sandbox_started" in steps:
                logs = self._runtime.container_logs(name)
                raise RecoveryFailed(f"the agent sandbox stopped again: {_clip(logs)}")
            # The same container again (docker start), or a new one by the same name
            # over the same workspace if it is gone. Only inside the egress policy.
            handle = self._ensure_workspace(run.id, op.id)
            steps["sandbox_started"] = self._clock.now().isoformat()
            steps["sandbox_container"] = handle.container_id
            self._save_steps(rec, steps)
        if self._http.request("GET", f"{self._server_ref(run.id).url}/health").ok:
            return True
        if self._waited(rec, steps, "sandbox_started") > self._settings.agent_start_timeout_s:
            raise RecoveryFailed("the Agent Server did not become healthy in time")
        return False

    def _recover_demo(self, run: Run, rec: Recovery, steps: dict[str, Any]) -> None:
        """Relaunch the recorded demo if its session did not survive."""
        demo = self._state.get_demo(run.id)
        if demo is None or demo.command is None or demo.state not in ("starting", "running"):
            return
        if demo.session_id is not None:
            live = self._runtime.demo_status(run.id, demo.session_id, demo.port)
            if live.alive:
                return
        handle = self._runtime.ensure_demo(DemoSpec(run.id, demo.command, demo.port))
        demo = self._state.set_demo_state(
            run.id,
            state="starting",
            message="relaunched from the recorded spec after the sandbox restarted",
            now=self._clock.now(),
            session_id=handle.session_id,
        )
        self._publish_demo_status(demo)
        steps["demo_relaunched"] = handle.session_id
        self._save_steps(rec, steps)

    def _recover_conversation(self, run: Run, rec: Recovery, steps: dict[str, Any]) -> bool:
        """Attach to the run's one conversation and resume it if the restart stopped it.

        Never starts a new conversation: while none was created, the launch step
        creates it.
        """
        cid = self._conversation_ref(run)
        if cid is None or not self._done(run.id, "workspace.create"):
            return True
        server = self._server_ref(run.id)
        try:
            snap = self._conversations.inspect(server, cid)
        except ConversationError as exc:
            if not self._done(run.id, "conversation.start"):
                return True  # never created; the launch step creates it
            # The Agent Server may still hold the dead instance's lease on it (<= 45 s).
            if self._waited(rec, steps, "conversation_wait") > self._settings.agent_start_timeout_s:
                raise RecoveryFailed(f"cannot reach conversation {cid}: {exc}") from None
            return False
        steps["conversation_status"] = snap.status
        if (
            snap.status in _RESUMABLE_CONVERSATION_STATUSES
            and rec.status_before not in _ENDED_CONVERSATION_STATUSES
        ):
            # Recorded before it is sent. If this controller dies in between, the next
            # one sees the conversation running (sent) or still stopped (send again).
            steps["resumed_from"] = snap.status
            steps["resumed_at"] = self._clock.now().isoformat()
            self._save_steps(rec, steps)
            notice = recovery_notice(
                rec.cause,
                sandbox_restarted="sandbox_started" in steps,
                demo_relaunched="demo_relaunched" in steps,
            )
            try:
                self._conversations.deliver(server, cid, EvidenceMessage(notice))
            except ConversationError as exc:
                raise RecoveryFailed(f"cannot resume conversation {cid}: {exc}") from None
            log.info("%s: conversation %s resumed from %s", run.id, cid, snap.status)
        else:
            self._save_steps(rec, steps)
        return True

    def _abandon_recovery(self, run_id: str, reason: str) -> None:
        rec = self._state.open_recovery(run_id)
        if rec is not None:
            self._state.finish_recovery(
                rec.id, status="failed", error=f"abandoned: {reason}", now=self._clock.now()
            )

    # --- the demo ------------------------------------------------------------------

    def _read_demo_request(self, run_id: str) -> dict[str, Any] | None:
        """The agent's pending start_demo request, if it is one we can answer."""
        agent_dir = self.paths(run_id).agent_dir
        try:
            raw = read_json(
                agent_dir, ".dgx", "demo-request.json", max_bytes=MAX_DEMO_REQUEST_BYTES
            )
        except FileNotFoundError:
            return None
        except AgentFileError as exc:
            self._warn_bad_request(run_id, str(exc))
            return None
        rid = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(rid, str) or not _REQUEST_ID.match(rid):
            self._warn_bad_request(run_id, "demo request without a valid id")
            return None
        return dict(raw)

    def _warn_bad_request(self, run_id: str, why: str) -> None:
        if self._bad_requests.get(run_id) != why:
            self._bad_requests[run_id] = why
            log.warning("%s: ignoring demo request: %s", run_id, why)

    def _step_demo(self, run: Run) -> None:
        demo = self._state.get_demo(run.id)
        if demo is None:
            return
        request = self._read_demo_request(run.id)
        if request is not None and request["id"] != demo.request_id:
            self._handle_demo_request(run, demo, request)
        elif demo.state == "starting" and demo.session_id is not None:
            self._check_demo_start(run, demo)

    def _handle_demo_request(self, run: Run, demo: Demo, request: Mapping[str, Any]) -> None:
        rid = str(request["id"])
        command = request.get("command")
        port = request.get("port", self._settings.demo_port)
        now = self._clock.now()
        problem = demo_request_problem(command, port, self._settings.demo_port)
        if problem is not None or not isinstance(command, str) or not isinstance(port, int):
            demo = self._state.record_demo_request(
                run.id, request_id=rid, command=None, state="refused", message=problem, now=now
            )
            log.warning("%s: refused demo request %s: %s", run.id, rid, problem)
            self._publish_demo_status(demo)
            return
        # The spec is durable before anything starts: it is what a sandbox restart
        # relaunches.
        previous_session = demo.session_id
        self._state.record_demo_request(
            run.id, request_id=rid, command=command, state="starting", message=None, now=now
        )
        try:
            handle = self._runtime.ensure_demo(
                DemoSpec(run.id, command, port, replace_session=previous_session)
            )
        except DockerError as exc:
            demo = self._state.set_demo_state(
                run.id, state="failed", message=f"could not start the demo: {exc}", now=now
            )
        else:
            demo = self._state.set_demo_state(
                run.id, state="starting", message=None, now=now, session_id=handle.session_id
            )
            log.info("%s: demo %s started (session %s)", run.id, rid, handle.session_id)
        self._publish_demo_status(demo)

    def _check_demo_start(self, run: Run, demo: Demo) -> None:
        live = self._demo_live(demo)
        if live is None:
            return
        now = self._clock.now()
        waited = (now - demo.updated_at).total_seconds()
        if live.listening:
            demo = self._state.set_demo_state(
                run.id, state="running", message=f"listening on port {demo.port}", now=now
            )
        elif not live.alive:
            demo = self._state.set_demo_state(
                run.id,
                state="failed",
                message=(
                    f"the command exited before listening on port {demo.port}. "
                    f"Last output:\n{_clip(live.log_tail)}"
                ),
                now=now,
            )
        elif waited > self._settings.demo_start_timeout_s:
            demo = self._state.set_demo_state(
                run.id,
                state="failed",
                message=(
                    f"not listening on port {demo.port} after {waited:.0f}s; the command is "
                    f"still running. Last output:\n{_clip(live.log_tail)}"
                ),
                now=now,
            )
        else:
            return
        log.info("%s: demo %s", run.id, demo.state)
        self._publish_demo_status(demo)

    def _publish_demo_status(self, demo: Demo) -> None:
        """The answer the agent's start_demo tool waits for (read-only to the agent)."""
        body = {
            "request_id": demo.request_id,
            "state": demo.state,
            "message": demo.message,
            "port": demo.port,
            "updated_at": demo.updated_at.isoformat(),
        }
        self._write_control(demo.run_id, "demo.json", json.dumps(body))

    # --- deadline and stop ---------------------------------------------------------

    def expire(self, run_id: str) -> dict[str, Any] | None:
        """The deadline passed: persist that, then end agent execution."""
        self._state.request_stop(run_id, "expired")
        return self.stop_agent(run_id)

    def _stop_lock(self, run_id: str) -> threading.Lock:
        with self._stop_locks_guard:
            return self._stop_locks.setdefault(run_id, threading.Lock())

    def stop_agent(self, run_id: str, *, block: bool = True) -> dict[str, Any] | None:
        """End agent execution for a run whose stop is persisted; keep its demo.

        Safe to call from several threads: one does the work, a blocking caller
        waits and gets the recorded evidence, a non-blocking caller gets None.
        """
        lock = self._stop_lock(run_id)
        if not lock.acquire(blocking=block):
            return None
        try:
            run = self._state.get_run(run_id)
            if run is None:
                raise RequestError(f"no run {run_id}")
            if run.phase == "stopped":
                return json.loads(run.stop_evidence) if run.stop_evidence else None
            if run.phase != "stopping":
                raise RequestError(f"{run_id} is {run.phase}; persist the stop request first")
            return asdict(self._stop_agent_locked(run))
        finally:
            lock.release()

    def _stop_agent_locked(self, run: Run) -> StopEvidence:
        started = self._clock.now()
        notes: list[str] = []
        self._abandon_recovery(run.id, f"agent execution is ending ({run.outcome or 'stopped'})")
        # 1. A sandbox that (re)starts from now on never starts the Agent Server.
        self._write_control(run.id, "mode", "demo-only\n")

        # 2. Pause the conversation and cancel its in-flight LLM call.
        try:
            container = self._runtime.inspect_container(agent_container_name(run.id))
        except DockerError as exc:
            notes.append(f"inspect: {exc}")
            container = None
        running = container is not None and container.running
        conversation_status: str | None = None
        quiescent: bool | None = None
        if running and run.conversation_id is not None:
            server = self._server_ref(run.id)
            for name, call in (
                ("interrupt", self._conversations.interrupt),
                ("pause", self._conversations.pause),
            ):
                try:
                    call(server, run.conversation_id)
                except ConversationError as exc:
                    notes.append(f"{name}: {exc}")
            # 3. Bounded wait for the conversation to go quiet.
            conversation_status = self._wait_quiescent(server, run.conversation_id)
            quiescent = conversation_status is not None and conversation_status != "running"

        # 4. End every agent process that is left; spare the demo session.
        demo = self._state.get_demo(run.id)
        keep = demo.session_id if demo is not None else None
        after_pause: list[SandboxProcess] = []
        survivors: list[SandboxProcess] | None
        try:
            report = self._runtime.stop_agent(run.id, keep, self._settings.stop_kill_grace_s)
            after_pause = list(report.before)
            survivors = list(report.survivors)
        except DockerError as exc:
            notes.append(f"kill: {exc}")
            survivors = None

        # 5. Could not end, or could not show the end of, agent execution in place:
        #    hard reset. The supervisor comes back demo-only, so the agent does not.
        restarted = False
        if survivors is None or survivors:
            try:
                self._runtime.restart_sandbox(run.id)
                restarted = True
                procs = self._runtime.sandbox_processes(run.id, None)
                survivors = [p for p in procs or () if p.role == "agent"]
            except DockerError as exc:
                notes.append(f"restart: {exc}")
                survivors = None
        failed = survivors is None or bool(survivors)

        # 6. The killed Agent Server's connection is closed, which cancels generation.
        busy = self._wait_inference_idle()

        # 7. Keep the demo; relaunch the recorded spec if the sandbox restarted.
        demo_view = self._retain_demo(run, demo, reset=restarted or not running, notes=notes)

        evidence = StopEvidence(
            run_id=run.id,
            reason=run.outcome or "stopped",
            started_at=started.isoformat(),
            finished_at=self._clock.now().isoformat(),
            container_running=running,
            conversation_status=conversation_status,
            quiescent=quiescent,
            after_pause=[_proc_view(p) for p in after_pause],
            tool_processes_after_pause=sum(1 for p in after_pause if not _is_server_process(p)),
            survivors=None if survivors is None else [_proc_view(p) for p in survivors],
            sandbox_restarted=restarted,
            inference_busy_slots=busy,
            demo=demo_view,
            failed=failed,
            notes=notes,
        )
        self._state.record_stop(run.id, json.dumps(asdict(evidence)), verified=not failed)
        if failed:
            log.error("%s: could not show that agent execution ended: %s", run.id, evidence)
        else:
            log.info(
                "%s: agent execution ended (%s); %d agent processes after pause, %d from tools",
                run.id,
                evidence.reason,
                len(after_pause),
                evidence.tool_processes_after_pause,
            )
            self._publish_project(run.id)
        return evidence

    def _wait_quiescent(self, server: ServerRef, conversation_id: str) -> str | None:
        end = self._clock.now() + timedelta(seconds=self._settings.stop_grace_s)
        while True:
            try:
                status: str | None = self._conversations.inspect(server, conversation_id).status
            except (ConversationError, OSError):
                status = None
            if (status is not None and status != "running") or self._clock.now() >= end:
                return status
            self._sleep(1.0)

    def _wait_inference_idle(self) -> int | None:
        end = self._clock.now() + timedelta(seconds=self._settings.stop_grace_s)
        while True:
            try:
                busy = self._inference.status().slots_busy
            except (InferenceError, DockerError):
                return None
            if not busy or self._clock.now() >= end:
                return busy
            self._sleep(1.0)

    def _retain_demo(
        self, run: Run, demo: Demo | None, *, reset: bool, notes: list[str]
    ) -> dict[str, Any] | None:
        if demo is None or demo.command is None:
            return None
        if reset:
            # The demo went down with the sandbox. Bring the sandbox back (demo-only)
            # and relaunch exactly the recorded spec; the agent does not come back.
            try:
                op = self._state.record_intent(run.id, "workspace.create", self._clock.now())
                self._ensure_workspace(run.id, op.id)
                handle = self._runtime.ensure_demo(DemoSpec(run.id, demo.command, demo.port))
                demo = self._state.set_demo_state(
                    run.id,
                    state="starting",
                    message="relaunched from the recorded spec after the sandbox restarted",
                    now=self._clock.now(),
                    session_id=handle.session_id,
                )
            except (DockerError, EgressPolicyError) as exc:
                notes.append(f"demo relaunch: {exc}")
        live = self._demo_live(demo)
        return {
            "command": demo.command,
            "session_id": demo.session_id,
            "host_port": demo.host_port,
            "relaunched": reset,
            "alive": live.alive if live else None,
            "listening": live.listening if live else None,
        }

    def _publish_project(self, run_id: str) -> None:
        """Let the operator read the finished project from the host.

        The agent's file tools write 0600 as the agent uid, so without this the
        workspace the CLI points at is unreadable to jim.
        """
        try:
            _make_world_readable(self.paths(run_id).project_dir)
        except OSError as exc:
            log.warning("%s: cannot make the project readable: %s", run_id, exc)


def _make_world_readable(root: Path) -> None:
    """Add o+r (and o+x on directories) through `root`; symlinks are left alone."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                continue
            extra = stat.S_IROTH | (stat.S_IXOTH if stat.S_ISDIR(st.st_mode) else 0)
            if st.st_mode & extra != extra:
                os.chmod(path, stat.S_IMODE(st.st_mode) | extra)


def _crash_now() -> None:
    """Die like `docker kill`: no cleanup, no graceful shutdown."""
    os.kill(os.getpid(), signal.SIGKILL)


def _default_chown(path: Path, uid: int, gid: int) -> None:
    # Outside the controller container (tests, a laptop) we are not root; the
    # ownership handoff only matters where the agent container mounts the path.
    if os.geteuid() == 0:
        os.chown(path, uid, gid)


# --- daemon entry point ------------------------------------------------------------


def build_controller(settings: Settings) -> Controller:
    from .inference import HttpxClient
    from .openhands_adapter import OpenHandsConversations
    from .runtime import DockerRuntime

    http = HttpxClient()
    runtime = DockerRuntime(settings)
    return Controller(
        settings=settings,
        state=StateStore(settings.state_db),
        catalog=load_models(settings.models_file),
        runtime=runtime,
        inference=InferenceManager(settings, runtime, http),
        conversations=OpenHandsConversations(http),
        http=http,
        clock=SystemClock(),
    )


def serve(settings: Settings | None = None) -> None:
    from .control_api import ControlServer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or Settings.from_env()
    # Controller state is never mounted anywhere else; keep it private on the host too.
    settings.state_db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    controller = build_controller(settings)
    try:
        # Before anything else, including the socket: a second controller must not
        # take over the first one's socket either.
        controller.acquire_writer()
    except WriterLockError as exc:
        log.error("%s; not starting", exc)
        raise SystemExit(1) from None
    stop = threading.Event()
    server = ControlServer(
        settings.socket_path,
        controller.handle,
        owner_uid=settings.operator_uid,
        owner_gid=settings.operator_gid,
    )
    watchdog = controller.deadline_watchdog()

    def _shutdown(signum: int, _frame: object) -> None:
        log.info("signal %s: shutting down", signum)
        stop.set()
        controller.wake()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # Before anything can resume a run: a deadline that passed while the controller
    # was down ends that run's agent execution first; interrupted operations are
    # inspected before the loop repeats them.
    controller.reconcile_on_start()
    watchdog.start()
    server.start()
    log.info("control socket at %s", settings.socket_path)
    try:
        controller.run_forever(stop)
    finally:
        watchdog.stop()
        server.shutdown()
