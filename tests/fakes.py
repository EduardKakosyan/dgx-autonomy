"""In-memory stand-ins for Docker, HTTP, the Agent Server and time.

Unit tests never need Docker, SSH or a model: every side effect goes through one of
these and is recorded for assertions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from dgx_autonomy.config import Settings
from dgx_autonomy.controller import Controller
from dgx_autonomy.ports import (
    ContainerSpec,
    ContainerState,
    ConversationRequest,
    ConversationSnapshot,
    EventSummary,
    EvidenceMessage,
    HttpResponse,
    RuntimeSnapshot,
    ServerRef,
    WorkspaceHandle,
    WorkspaceSpec,
)
from dgx_autonomy.runtime import (
    LABEL_RUN,
    CommandResult,
    DockerError,
    agent_container_name,
    agent_container_spec,
    docker_run_argv,
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
    """RuntimePort + ContainerPort with containers kept in a dict."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env({})
        self.containers: dict[str, ContainerState] = {}
        self.specs: dict[str, ContainerSpec] = {}
        self.runs: list[str] = []  # names passed to a (fake) docker run
        self.logs: dict[str, str] = {}
        self.fail_run: dict[str, str] = {}

    def inspect_container(self, name: str) -> ContainerState | None:
        return self.containers.get(name)

    def ensure_container(self, spec: ContainerSpec) -> ContainerState:
        docker_run_argv(spec)  # same safety checks as the real adapter
        current = self.containers.get(spec.name)
        if current is not None and current.running:
            return current
        if spec.name in self.fail_run:
            raise DockerError(self.fail_run[spec.name])
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
        return state

    def container_logs(self, name: str, tail: int = 40) -> str:
        return self.logs.get(name, "")

    def exit(self, name: str, code: int = 1, logs: str = "") -> None:
        self.containers[name] = replace(
            self.containers[name], running=False, status="exited", exit_code=code
        )
        self.logs[name] = logs

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
    """ConversationPort. Set `status` to steer what the controller observes."""

    status: str = "running"
    started: list[ConversationRequest] = field(default_factory=list)
    delivered: list[EvidenceMessage] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    resumed: list[str] = field(default_factory=list)
    event_log: list[EventSummary] = field(default_factory=list)
    fail_start: Exception | None = None

    def start(self, request: ConversationRequest) -> str:
        if self.fail_start is not None:
            raise self.fail_start
        self.started.append(request)
        return request.conversation_id

    def inspect(self, server: ServerRef, conversation_id: str) -> ConversationSnapshot:
        last = self.event_log[-1] if self.event_log else None
        return ConversationSnapshot(conversation_id, self.status, last)

    def resume(self, server: ServerRef, conversation_id: str) -> None:
        self.resumed.append(conversation_id)

    def pause(self, server: ServerRef, conversation_id: str) -> None:
        self.paused.append(conversation_id)

    def deliver(self, server: ServerRef, conversation_id: str, evidence: EvidenceMessage) -> None:
        self.delivered.append(evidence)

    def events(
        self, server: ServerRef, conversation_id: str, since: int, limit: int
    ) -> Sequence[EventSummary]:
        return self.event_log[since : since + limit]


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
        url = f"http://dgx-autonomy-agent-{run_id}:{self.settings.agent_port}/health"
        self.http.routes[url] = HttpResponse(200, "OK")


def make_harness(settings: Settings) -> Harness:
    """A Controller wired to fakes, with every side effect observable."""
    from dgx_autonomy.config import load_models
    from dgx_autonomy.inference import InferenceManager

    clock = FakeClock()
    state = StateStore(":memory:")
    runtime = FakeRuntime(settings)
    http = FakeHttp()
    conversation = FakeConversation()
    chowned: list[tuple[str, int, int]] = []
    controller = Controller(
        settings=settings,
        state=state,
        catalog=load_models(settings.models_file),
        runtime=runtime,
        inference=InferenceManager(settings, runtime, http),
        conversations=conversation,
        http=http,
        clock=clock,
        chown=lambda path, uid, gid: chowned.append((str(path), uid, gid)),
    )
    return Harness(settings, clock, state, runtime, http, conversation, controller, chowned)
