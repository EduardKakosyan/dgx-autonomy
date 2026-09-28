"""Seams between the controller and the outside world.

The controller only sees these protocols. Production wiring uses the docker CLI
(runtime.py), httpx (inference.py) and the OpenHands SDK (openhands_adapter.py);
unit tests use the fakes in tests/fakes.py. Nothing here claims an SDK method name:
these are this package's own contracts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .snapshot import Snapshot

# --- clock -------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...


# --- HTTP --------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    """status 0 means the request never got a response (refused, DNS, timeout)."""

    status: int
    body: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 5.0,
    ) -> HttpResponse: ...


# --- containers --------------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class PortBinding:
    """A published container port. runtime.py refuses any host address but loopback."""

    host_ip: str
    host_port: int
    container_port: int


@dataclass(frozen=True)
class ContainerSpec:
    """Everything `docker run` is allowed to receive. Hardening is on by default."""

    name: str
    image: str
    labels: Mapping[str, str] = field(default_factory=dict)
    networks: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    user: str | None = None
    cap_drop_all: bool = True
    no_new_privileges: bool = True
    gpus: bool = False
    memory: str | None = None
    cpus: str | None = None
    pids_limit: int | None = None
    restart: str | None = None
    ports: tuple[PortBinding, ...] = ()
    dns: tuple[str, ...] = ()
    entrypoint: str | None = None
    workdir: str | None = None
    shm_size: str | None = None


@dataclass(frozen=True)
class ContainerState:
    id: str
    name: str
    running: bool
    status: str
    exit_code: int
    labels: Mapping[str, str] = field(default_factory=dict)


class ContainerPort(Protocol):
    def inspect_container(self, name: str) -> ContainerState | None: ...
    def ensure_container(self, spec: ContainerSpec) -> ContainerState: ...
    def remove_container(self, name: str) -> bool:
        """Stop and remove; False when there was nothing to remove."""
        ...

    def container_logs(self, name: str, tail: int = 40) -> str: ...
    def kill_container(self, name: str) -> bool:
        """SIGKILL the container, like a crash (`fault.inject`). False if not running."""
        ...


@dataclass(frozen=True)
class WorkspaceSpec:
    run_id: str
    op_id: str
    agent_dir: Path
    # The frozen brief and checks (frozen.py); read-only at /brief in the sandbox.
    frozen_dir: Path
    session_api_key: str
    secret_key: str
    # Controller-owned, read-only in the sandbox: the supervisor mode and demo status.
    control_dir: Path
    # 127.0.0.1:<demo_host_port> on the DGX -> the demo port in the sandbox.
    demo_host_port: int


@dataclass(frozen=True)
class WorkspaceHandle:
    container_id: str
    running: bool
    url: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    run_id: str
    containers: tuple[ContainerState, ...]


@dataclass(frozen=True)
class SandboxProcess:
    """One process inside the agent sandbox, as the in-sandbox helper classified it.

    role: supervisor (PID 1) | controller (created by docker exec) | demo (the
    recorded demo session) | zombie | agent (everything else: the Agent Server, its
    tools, and anything they started, daemonized or not).
    """

    pid: int
    ppid: int
    sid: int
    state: str
    cmd: str
    role: str = "agent"


@dataclass(frozen=True)
class KillReport:
    """What ending the `agent` processes inside the sandbox found and left behind."""

    container_running: bool
    before: tuple[SandboxProcess, ...] = ()
    survivors: tuple[SandboxProcess, ...] = ()


@dataclass(frozen=True)
class DemoSpec:
    """A demo the controller runs in the sandbox, in a session of its own."""

    run_id: str
    command: str
    port: int
    # A previous demo session to end first (the agent asked for a new demo).
    replace_session: int | None = None


@dataclass(frozen=True)
class DemoHandle:
    session_id: int


@dataclass(frozen=True)
class DemoStatus:
    alive: bool
    listening: bool
    log_tail: str = ""
    # Why the port is not the demo's to serve: another process holds it, or the demo
    # listens on loopback only.
    problem: str = ""


class RuntimePort(ContainerPort, Protocol):
    def inspect(self, run_id: str) -> RuntimeSnapshot: ...
    def network_bridge(self, network: str) -> str | None:
        """The host interface of a Docker network, or None when it does not exist."""
        ...

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> tuple[int, str]:
        """`docker run --rm`: exit code and stdout of a short-lived container."""
        ...

    def ensure_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle: ...
    def sandbox_processes(
        self, run_id: str, keep_session: int | None
    ) -> tuple[SandboxProcess, ...] | None:
        """Classified processes, or None when the sandbox is not running."""
        ...

    def stop_agent(self, run_id: str, keep_session: int | None, grace_s: float) -> KillReport:
        """End every `agent` process; spare the demo session `keep_session`."""
        ...

    def restart_sandbox(self, run_id: str) -> None:
        """Hard reset. The supervisor comes back in the mode the control dir says."""
        ...

    def ensure_demo(self, spec: DemoSpec) -> DemoHandle: ...
    def demo_status(self, run_id: str, session_id: int, port: int) -> DemoStatus: ...


# --- conversations -----------------------------------------------------------------


@dataclass(frozen=True)
class ServerRef:
    """An Agent Server as the controller reaches it on the private network."""

    url: str
    api_key: str


@dataclass(frozen=True)
class LlmEndpoint:
    model: str
    base_url: str
    max_input_tokens: int
    max_output_tokens: int | None = None
    timeout_s: int | None = None


@dataclass(frozen=True)
class ConversationRequest:
    server: ServerRef
    conversation_id: str
    working_dir: str
    llm: LlmEndpoint
    message: str
    # The builder gets start_demo, write_handoff and declare_blocked; the planner
    # gets none of them (it serves nothing and hands nothing over).
    builder_tools: bool = True


@dataclass(frozen=True)
class EventSummary:
    id: str
    timestamp: str
    kind: str
    source: str
    text: str
    # The tool an ActionEvent calls. The text may be clipped before it names the tool.
    tool: str | None = None


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: str
    # The SDK's execution status: idle | running | paused | waiting_for_confirmation
    # | finished | error | stuck | deleting.
    status: str
    last_event: EventSummary | None
    # Tokens of the agent's latest LLM request (prompt + completion), from the SDK's
    # usage metrics: how full its context is. None when not reported yet.
    context_tokens: int | None = None


@dataclass(frozen=True)
class EvidenceMessage:
    text: str
    # True: the message also runs the conversation. False: it is context only.
    run: bool = True


class ConversationPort(Protocol):
    def start(self, request: ConversationRequest) -> str: ...
    def inspect(self, server: ServerRef, conversation_id: str) -> ConversationSnapshot: ...
    def resume(self, server: ServerRef, conversation_id: str) -> None: ...
    def pause(self, server: ServerRef, conversation_id: str) -> None: ...
    def interrupt(self, server: ServerRef, conversation_id: str) -> None:
        """Cancel the in-flight LLM call; the conversation ends up paused."""
        ...

    def deliver(
        self, server: ServerRef, conversation_id: str, evidence: EvidenceMessage
    ) -> None: ...
    def events(
        self,
        server: ServerRef,
        conversation_id: str,
        since: int,
        limit: int,
        *,
        text_limit: int = 300,
    ) -> Sequence[EventSummary]:
        """Events [since, since+limit) in order; each text clipped to `text_limit`."""
        ...

    def recent(self, server: ServerRef, conversation_id: str, limit: int) -> Sequence[EventSummary]:
        """The newest `limit` events, newest first."""
        ...


# --- project snapshots -------------------------------------------------------------


class SnapshotPort(Protocol):
    """Content-addressed snapshots of a project directory (snapshot.py)."""

    def take(self, store: Path, project: Path, label: str) -> Snapshot: ...
    def changed(self, store: Path, before: str, after: str, limit: int = 200) -> list[str]: ...
    def archive(self, store: Path, commit: str, dest: Path) -> None: ...
