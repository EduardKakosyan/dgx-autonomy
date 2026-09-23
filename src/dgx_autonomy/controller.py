"""The trusted controller: lifecycle state, reconciliation, and the control API handler.

A command (for example `launch`) only persists the requested transition and wakes the
reconcile loop. The loop does the work, one idempotent step per tick:

    launched: ensure inference -> ensure workspace -> start conversation -> running
    running:  observe the conversation; FINISHED -> finished, ERROR/STUCK -> failed

Every external side effect is preceded by a durable operation intent. A step that is
not ready yet (the model still loading, the Agent Server still booting) leaves the
intent in place and is re-checked on the next tick, so no SDK or HTTP call blocks
the loop for long.
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import ConfigError, ModelCatalog, Settings, load_models
from .inference import InferenceError, InferenceManager
from .openhands_adapter import ConversationError, conversation_id_for
from .ports import (
    Clock,
    ConversationPort,
    ConversationRequest,
    ConversationSnapshot,
    HttpClient,
    LlmEndpoint,
    RuntimePort,
    ServerRef,
    WorkspaceSpec,
)
from .runtime import AGENT_BRIEF_PATH, AGENT_PROJECT_DIR, DockerError, agent_container_name
from .state import Operation, OperationKind, Run, StateStore

log = logging.getLogger("dgx_autonomy.controller")

FAILED_CONVERSATION_STATUSES = frozenset({"error", "stuck"})
MAX_BRIEF_BYTES = 256 * 1024


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RequestError(ValueError):
    """A control request is invalid; the message goes back to the CLI as-is."""


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
    def secrets_dir(self) -> Path:
        return self.root / "secrets"


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
        "Complete the brief, verify the result yourself, then finish.\n\n"
        "--- BRIEF ---\n"
        f"{brief.strip()}\n"
    )


def _new_run_id(now: datetime) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


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
        self._wake = threading.Event()
        self._reconcile_lock = threading.Lock()
        self._snapshots: dict[str, ConversationSnapshot] = {}

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
        return p

    def _server_ref(self, run_id: str) -> ServerRef:
        key = (self.paths(run_id).secrets_dir / "session_api_key").read_text().strip()
        url = f"http://{agent_container_name(run_id)}:{self._settings.agent_port}"
        return ServerRef(url=url, api_key=key)

    # --- commands (called from the control socket) ---------------------------------

    def handle(self, op: str, args: Mapping[str, Any]) -> Any:
        handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "ping": lambda a: {"pong": True},
            "launch": self.launch,
            "status": self.status,
            "runs": lambda a: [self._run_view(r) for r in self._state.list_runs()],
            "logs": self.logs,
            "inference.ensure": self.inference_ensure,
            "inference.status": lambda a: self._inference.status().as_dict(),
            "inference.request": self.inference_request,
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
        snap = self._snapshots.get(run.id)
        if snap is None and run.conversation_id is not None:
            snap = self._try_inspect(run)
        view["conversation_status"] = snap.status if snap else None
        view["last_event"] = asdict(snap.last_event) if snap and snap.last_event else None
        view["operations"] = [
            {"kind": o.kind, "status": o.status, "resource_id": o.resource_id, "error": o.error}
            for o in self._state.operations(run.id)
        ]
        return view

    def logs(self, args: Mapping[str, Any]) -> dict[str, Any]:
        run = self._resolve(args)
        since = max(0, int(args.get("since", 0)))
        limit = min(500, max(1, int(args.get("limit", 200))))
        if run.conversation_id is None:
            return {"run_id": run.id, "events": [], "next": since, "phase": run.phase}
        try:
            events = self._conversations.events(
                self._server_ref(run.id), run.conversation_id, since, limit
            )
        except ConversationError as exc:
            raise RequestError(f"cannot read events: {exc}") from None
        return {
            "run_id": run.id,
            "events": [asdict(e) for e in events],
            "next": since + len(events),
            "phase": run.phase,
        }

    def inference_ensure(self, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            model = self._catalog.get(args.get("model_key"))
            self._inference.ensure(model)
        except (ConfigError, InferenceError, DockerError) as exc:
            raise RequestError(str(exc)) from None
        return self._inference.status().as_dict()

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
            "model_key": run.model_key,
            "launched_at": run.launched_at.isoformat(),
            "deadline_at": run.deadline_at.isoformat(),
            "brief_path": run.brief_path,
            "conversation_id": run.conversation_id,
            "workspace_dir": str(self.paths(run.id).project_dir),
        }

    # --- reconciliation ------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

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

    def _reconcile_run(self, run: Run) -> None:
        if run.phase == "launched":
            if not self._step_inference(run):
                return
            server = self._step_workspace(run)
            if server is None:
                return
            if not self._step_conversation(run, server):
                return
            self._state.set_phase(run.id, "running")
            log.info("%s is running", run.id)
            run = self._state.get_run(run.id) or run
        if run.phase == "running" and run.conversation_id is not None:
            self._observe(run)

    def _fail(self, run: Run, op: Operation, error: str) -> None:
        log.error("%s: %s failed: %s", run.id, op.kind, error)
        self._state.fail_operation(op.id, error)
        self._state.set_phase(run.id, "failed")

    def _intent(self, run: Run, kind: OperationKind) -> Operation | None:
        """The intent for `kind`, or None when the run already failed at this step."""
        op = self._state.record_intent(run.id, kind, self._clock.now())
        if op.status == "failed":
            self._state.set_phase(run.id, "failed")
            return None
        return op

    def _timed_out(self, op: Operation, limit_s: float) -> bool:
        return (self._clock.now() - op.created_at).total_seconds() > limit_s

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
            paths = self._prepare_run_dirs(run.id)
            spec = WorkspaceSpec(
                run_id=run.id,
                op_id=op.id,
                agent_dir=paths.agent_dir,
                brief_file=paths.brief,
                session_api_key=server.api_key,
                secret_key=(paths.secrets_dir / "secret_key").read_text().strip(),
            )
            try:
                handle = self._runtime.ensure_workspace(spec)
            except DockerError as exc:
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
            if self._timed_out(op, self._settings.agent_start_timeout_s):
                self._fail(run, op, str(exc))
            else:
                log.warning("%s: conversation start will be retried: %s", run.id, exc)
            return False
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
            self._state.set_phase(run.id, "finished")
            log.info("%s finished", run.id)
        elif snap.status in FAILED_CONVERSATION_STATUSES:
            self._state.set_phase(run.id, "failed")
            log.warning("%s conversation ended %s", run.id, snap.status)
        else:
            return
        self._publish_project(run.id)

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
    stop = threading.Event()
    server = ControlServer(
        settings.socket_path,
        controller.handle,
        owner_uid=settings.operator_uid,
        owner_gid=settings.operator_gid,
    )

    def _shutdown(signum: int, _frame: object) -> None:
        log.info("signal %s: shutting down", signum)
        stop.set()
        controller.wake()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.start()
    log.info("control socket at %s", settings.socket_path)
    try:
        controller.run_forever(stop)
    finally:
        server.shutdown()
