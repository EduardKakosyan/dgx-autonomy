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
from typing import Any, Protocol

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
    def container_logs(self, name: str, tail: int = 40) -> str: ...


@dataclass(frozen=True)
class WorkspaceSpec:
    run_id: str
    op_id: str
    agent_dir: Path
    brief_file: Path
    session_api_key: str
    secret_key: str


@dataclass(frozen=True)
class WorkspaceHandle:
    container_id: str
    running: bool
    url: str


@dataclass(frozen=True)
class RuntimeSnapshot:
    run_id: str
    containers: tuple[ContainerState, ...]


class RuntimePort(ContainerPort, Protocol):
    def inspect(self, run_id: str) -> RuntimeSnapshot: ...
    def ensure_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle: ...


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


@dataclass(frozen=True)
class ConversationRequest:
    server: ServerRef
    conversation_id: str
    working_dir: str
    llm: LlmEndpoint
    message: str


@dataclass(frozen=True)
class EventSummary:
    id: str
    timestamp: str
    kind: str
    source: str
    text: str


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: str
    # The SDK's execution status: idle | running | paused | waiting_for_confirmation
    # | finished | error | stuck | deleting.
    status: str
    last_event: EventSummary | None


@dataclass(frozen=True)
class EvidenceMessage:
    text: str


class ConversationPort(Protocol):
    def start(self, request: ConversationRequest) -> str: ...
    def inspect(self, server: ServerRef, conversation_id: str) -> ConversationSnapshot: ...
    def resume(self, server: ServerRef, conversation_id: str) -> None: ...
    def pause(self, server: ServerRef, conversation_id: str) -> None: ...
    def deliver(
        self, server: ServerRef, conversation_id: str, evidence: EvidenceMessage
    ) -> None: ...
    def events(
        self, server: ServerRef, conversation_id: str, since: int, limit: int
    ) -> Sequence[EventSummary]: ...
