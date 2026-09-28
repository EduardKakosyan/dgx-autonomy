"""ConversationPort over the OpenHands Agent Server (SDK v1.49.4).

OpenHands owns the model/tool loop. The controller only creates a conversation,
reads its status and events, and pauses, resumes or messages it.

Creation goes through the public SDK (`Conversation(agent, workspace=RemoteWorkspace)`)
so the agent, its tools and the condenser are serialized exactly as the SDK expects.
Everything after that uses the Agent Server's REST routes, the same ones
`RemoteConversation` calls, so a status poll never opens a WebSocket or blocks
on a run:

    GET  /api/conversations/{id}                  -> execution_status
    GET  /api/conversations/{id}/events/search    -> paged events
    POST /api/conversations/{id}/events           -> user message (+ run)
    POST /api/conversations/{id}/run | /pause | /interrupt

`/pause` takes effect between agent steps and waits for an in-flight LLM call;
`/interrupt` cancels that call. Neither ends the processes the agent's tools
started: on hugo-dgx1 the tmux server, its shell and a backgrounded command all
outlived both (the stop evidence records this for every stop), so the controller
ends agent execution by killing the sandbox's agent processes (runtime.stop_agent).

Once agent execution has been ended (stop or deadline) the Agent Server is gone,
so `persisted_status` / `persisted_events` read the conversation the SDK persisted
under the workspace. They only read, and never follow a symlink the agent planted.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .agent_files import open_dir, read_json, read_json_at
from .ports import (
    ConversationRequest,
    ConversationSnapshot,
    EventSummary,
    EvidenceMessage,
    HttpClient,
    HttpResponse,
    ServerRef,
)

CONVERSATIONS = "/api/conversations"
TOOLS = "/api/tools/"
_NAMESPACE = uuid.UUID("5f0c1d0e-8a57-4d8e-9c55-6a2f3f1d9b21")
_TEXT_LIMIT = 300
_EVENT_FILE = re.compile(r"^event-(\d+)-[0-9a-fA-F-]+\.json$")
_MAX_EVENT_BYTES = 4 * 1024 * 1024
# The custom tools the Agent Server loads with --import-modules (demo_tool.py,
# handoff_tool.py).
DEMO_TOOL_NAME = "start_demo"
HANDOFF_TOOL_NAME = "write_handoff"
BLOCKED_TOOL_NAME = "declare_blocked"
PROGRESS_TOOL_NAME = "report_progress"
BUILDER_TOOLS = (DEMO_TOOL_NAME, HANDOFF_TOOL_NAME, BLOCKED_TOOL_NAME, PROGRESS_TOOL_NAME)


# The SDK's default condenser summarizes after 240 events, whatever their size. On
# hugo-dgx1 a planner at 256K context was condensed twice with its window mostly
# empty, lost track of which edits it had made, and spent an hour re-checking files.
# The event count is set out of reach, so only the token limit (the model's context)
# condenses; the controller's handoff and rollover at 85% of it come first.
CONDENSER_MAX_EVENTS = 100_000
CONDENSER_KEEP_FIRST = 4


class ConversationError(RuntimeError):
    pass


def with_token_condenser(agent: Any, llm: Any) -> Any:
    """The agent with a summarizing condenser that only the context limit triggers."""
    from openhands.sdk.context.condenser import LLMSummarizingCondenser

    condenser = LLMSummarizingCondenser(
        llm=llm.model_copy(update={"usage_id": "condenser"}),
        max_size=CONDENSER_MAX_EVENTS,
        keep_first=CONDENSER_KEEP_FIRST,
    )
    return agent.model_copy(update={"condenser": condenser})


def conversation_id_for(run_id: str) -> str:
    """Deterministic, so a retried start after a crash attaches instead of duplicating."""
    return str(uuid.uuid5(_NAMESPACE, run_id))


def _clip(text: str, limit: int = _TEXT_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _texts(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [str(c.get("text", "")) for c in content if isinstance(c, dict)]
    return " ".join(p for p in parts if p)


def summarize_event(raw: Mapping[str, Any], text_limit: int = _TEXT_LIMIT) -> EventSummary:
    """One readable line per SDK event. Defensive: unknown shapes fall back to the kind.

    Messages keep their line breaks when `text_limit` is above the default (the
    planning REPL shows them in full); everything else is one line."""
    kind = str(raw.get("kind", "Event"))
    text = ""
    if kind == "MessageEvent":
        msg = raw.get("llm_message") or {}
        text = _texts(msg.get("content") if isinstance(msg, dict) else None)
    elif kind == "ActionEvent":
        action = raw.get("action") or {}
        args = {k: v for k, v in action.items() if k != "kind"} if isinstance(action, dict) else {}
        thought = _texts(raw.get("thought"))
        text = f"{raw.get('tool_name', '?')} {json.dumps(args, ensure_ascii=False)}"
        if thought:
            text = f"{thought} -> {text}"
    elif kind == "ObservationEvent":
        obs = raw.get("observation") or {}
        body = _texts(obs.get("content")) if isinstance(obs, dict) else ""
        text = f"{raw.get('tool_name', '?')}: {body}"
    elif kind == "AgentErrorEvent":
        text = str(raw.get("error", ""))
    elif kind == "ConversationErrorEvent":
        text = f"{raw.get('code', '')}: {raw.get('detail', '')}"
    elif kind == "ConversationStateUpdateEvent":
        text = f"{raw.get('key', '')}={_clip(json.dumps(raw.get('value'), default=str), 80)}"
    if not text:
        text = kind
    elif kind == "MessageEvent" and text_limit > _TEXT_LIMIT:
        text = text.strip() if len(text) <= text_limit else text[: text_limit - 1] + "…"
    else:
        text = _clip(text, text_limit)
    return EventSummary(
        id=str(raw.get("id", "")),
        timestamp=str(raw.get("timestamp", "")),
        kind=kind,
        source=str(raw.get("source", "")),
        text=text,
        tool=str(raw.get("tool_name")) if kind == "ActionEvent" and raw.get("tool_name") else None,
    )


class OpenHandsConversations:
    """ConversationPort. The HTTP client is injected; the SDK is imported lazily."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    # --- REST helpers --------------------------------------------------------------

    def _call(
        self,
        server: ServerRef,
        method: str,
        path: str,
        body: Any = None,
        *,
        accept: frozenset[int] = frozenset(),
        timeout: float = 30.0,
    ) -> HttpResponse:
        res = self._http.request(
            method,
            f"{server.url}{path}",
            json_body=body,
            headers={"X-Session-API-Key": server.api_key},
            timeout=timeout,
        )
        if not res.ok and res.status not in accept:
            detail = res.error or f"HTTP {res.status}: {str(res.body)[:300]}"
            raise ConversationError(f"{method} {path}: {detail}")
        return res

    def exists(self, server: ServerRef, conversation_id: str) -> bool:
        res = self._call(
            server, "GET", f"{CONVERSATIONS}/{conversation_id}", accept=frozenset({404})
        )
        return res.status != 404

    # --- ConversationPort ----------------------------------------------------------

    def start(self, request: ConversationRequest) -> str:
        cid = request.conversation_id
        server = request.server
        if not self.exists(server, cid):
            self._create(request, self._builder_tools(request))
        if not self._has_user_message(server, cid):
            try:
                self._send(server, cid, request.message, run=True)
            except ConversationError as exc:
                if "is not registered" not in str(exc):
                    raise
                # Created with a tool this Agent Server lacks (a sandbox from an older
                # image): nothing has run in it yet, so it is replaced.
                self._call(server, "DELETE", f"{CONVERSATIONS}/{cid}")
                self._create(request, self._builder_tools(request))
                self._send(server, cid, request.message, run=True)
        return cid

    def _builder_tools(self, request: ConversationRequest) -> tuple[str, ...]:
        """The builder tools this Agent Server has registered (all of them from the
        current agent image; a sandbox created from an older image may lack some)."""
        if not request.builder_tools:
            return ()
        res = self._call(request.server, "GET", TOOLS, accept=frozenset({404}))
        if res.status == 404 or not isinstance(res.body, list):
            return BUILDER_TOOLS
        return tuple(t for t in BUILDER_TOOLS if t in res.body)

    def _create(self, request: ConversationRequest, extra_tools: Sequence[str]) -> None:
        # Imported here: the SDK pulls in LiteLLM and the tool registry, which the
        # CLI and the unit tests never need.
        from openhands.sdk import LLM, Conversation
        from openhands.sdk.workspace import RemoteWorkspace
        from openhands.tools.preset.default import get_default_agent
        from pydantic import SecretStr

        llm = LLM(
            # LiteLLM's OpenAI-compatible provider pointed at our llama-server.
            model=f"openai/{request.llm.model}",
            base_url=request.llm.base_url,
            api_key=SecretStr("local-llama-server"),
            max_input_tokens=request.llm.max_input_tokens,
            max_output_tokens=request.llm.max_output_tokens,
            timeout=request.llm.timeout_s,
            usage_id="agent",
        )
        # cli_mode drops the browser tool set; the run needs terminal + file editor,
        # plus start_demo, which the Agent Server image loads with --import-modules.
        from openhands.sdk import Tool

        agent = with_token_condenser(get_default_agent(llm=llm, cli_mode=True), llm)
        if extra_tools:
            extra = [Tool(name=name) for name in extra_tools]
            agent = agent.model_copy(update={"tools": [*agent.tools, *extra]})
        workspace = RemoteWorkspace(
            host=request.server.url,
            working_dir=request.working_dir,
            api_key=request.server.api_key,
        )
        conversation = Conversation(
            agent=agent,
            workspace=workspace,
            conversation_id=uuid.UUID(request.conversation_id),
            delete_on_close=False,  # the Agent Server keeps it after we disconnect
            visualizer=None,
        )
        try:
            if str(conversation.id) != request.conversation_id:
                raise ConversationError(
                    f"Agent Server returned {conversation.id}, expected {request.conversation_id}"
                )
        finally:
            conversation.close()
            workspace.reset_client()

    def _has_user_message(self, server: ServerRef, conversation_id: str) -> bool:
        # The server's `kind` filter wants the event's full module path, which is an
        # SDK internal; filter on `source` and match the kind here instead.
        res = self._call(
            server,
            "GET",
            f"{CONVERSATIONS}/{conversation_id}/events/search?source=user&limit=100",
        )
        items = res.body.get("items") if isinstance(res.body, dict) else None
        return any(isinstance(i, dict) and i.get("kind") == "MessageEvent" for i in items or [])

    def _send(self, server: ServerRef, conversation_id: str, text: str, *, run: bool) -> None:
        body = {"role": "user", "content": [{"type": "text", "text": text}], "run": run}
        self._call(server, "POST", f"{CONVERSATIONS}/{conversation_id}/events", body)

    def inspect(self, server: ServerRef, conversation_id: str) -> ConversationSnapshot:
        info = self._call(server, "GET", f"{CONVERSATIONS}/{conversation_id}", timeout=10.0)
        body = info.body if isinstance(info.body, dict) else {}
        # The SDK's ConversationExecutionStatus values: idle, running, paused,
        # waiting_for_confirmation, finished, error, stuck, deleting.
        status = str(body.get("execution_status", "unknown")).lower()
        last = self._call(
            server,
            "GET",
            f"{CONVERSATIONS}/{conversation_id}/events/search?sort_order=TIMESTAMP_DESC&limit=1",
            timeout=10.0,
        )
        items = last.body.get("items") if isinstance(last.body, dict) else None
        last_event = summarize_event(items[0]) if items else None
        return ConversationSnapshot(conversation_id, status, last_event, context_tokens(body))

    def resume(self, server: ServerRef, conversation_id: str) -> None:
        # 409 = already running, which is what we want.
        self._call(
            server, "POST", f"{CONVERSATIONS}/{conversation_id}/run", accept=frozenset({409})
        )

    def pause(self, server: ServerRef, conversation_id: str) -> None:
        self._call(server, "POST", f"{CONVERSATIONS}/{conversation_id}/pause", timeout=15.0)

    def interrupt(self, server: ServerRef, conversation_id: str) -> None:
        self._call(server, "POST", f"{CONVERSATIONS}/{conversation_id}/interrupt", timeout=15.0)

    def deliver(self, server: ServerRef, conversation_id: str, evidence: EvidenceMessage) -> None:
        self._send(server, conversation_id, evidence.text, run=evidence.run)

    def recent(self, server: ServerRef, conversation_id: str, limit: int) -> Sequence[EventSummary]:
        res = self._call(
            server,
            "GET",
            f"{CONVERSATIONS}/{conversation_id}/events/search"
            f"?sort_order=TIMESTAMP_DESC&limit={max(1, min(limit, 100))}",
            timeout=10.0,
        )
        items = res.body.get("items") if isinstance(res.body, dict) else None
        return [summarize_event(i) for i in items or [] if isinstance(i, dict)]

    def events(
        self,
        server: ServerRef,
        conversation_id: str,
        since: int,
        limit: int,
        *,
        text_limit: int = _TEXT_LIMIT,
    ) -> Sequence[EventSummary]:
        """Events [since, since+limit) in timestamp order."""
        out: list[EventSummary] = []
        seen = 0
        page_id: str | None = None
        while len(out) < limit:
            query = "limit=100" + (f"&page_id={quote(page_id)}" if page_id else "")
            res = self._call(
                server, "GET", f"{CONVERSATIONS}/{conversation_id}/events/search?{query}"
            )
            body = res.body if isinstance(res.body, dict) else {}
            items = body.get("items") or []
            for raw in items:
                if seen >= since and len(out) < limit:
                    out.append(summarize_event(raw, text_limit))
                seen += 1
            page_id = body.get("next_page_id")
            if not page_id or not items:
                break
        return out


def context_tokens(info: Mapping[str, Any]) -> int | None:
    """The agent's latest request size from the conversation's usage stats.

    SDK 1.49.4: stats.usage_to_metrics[<usage id>].accumulated_token_usage.per_turn_token
    is prompt + completion tokens of the latest call (accumulation keeps the latest).
    """
    stats = info.get("stats")
    usage = stats.get("usage_to_metrics") if isinstance(stats, dict) else None
    agent = usage.get("agent") if isinstance(usage, dict) else None
    acc = agent.get("accumulated_token_usage") if isinstance(agent, dict) else None
    value = acc.get("per_turn_token") if isinstance(acc, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --- the conversation as the SDK persisted it ---------------------------------------


def persisted_event_count(conversations_dir: Path, conversation_id: str) -> int | None:
    """How many events the SDK has saved for the conversation (None if unreadable)."""
    try:
        cid = uuid.UUID(conversation_id).hex
        with open_dir(conversations_dir.parent, conversations_dir.name, cid, "events") as fd:
            return sum(1 for n in os.listdir(fd) if _EVENT_FILE.match(n))
    except (OSError, ValueError):
        return None


def persisted_status(conversations_dir: Path, conversation_id: str) -> str | None:
    """execution_status from base_state.json, or None if it cannot be read.

    Stale by nature: an Agent Server killed mid-step never wrote its last status.
    """
    try:
        state = read_json(
            conversations_dir.parent,
            conversations_dir.name,
            uuid.UUID(conversation_id).hex,
            "base_state.json",
            max_bytes=_MAX_EVENT_BYTES,
        )
    except (OSError, ValueError):
        return None
    status = state.get("execution_status") if isinstance(state, dict) else None
    return str(status) if status is not None else None


def persisted_events(
    conversations_dir: Path,
    conversation_id: str,
    since: int,
    limit: int,
    *,
    last: bool = False,
    text_limit: int = _TEXT_LIMIT,
) -> list[EventSummary]:
    """Events [since, since+limit) from the SDK's event files, in index order.

    `last=True` returns the final `limit` events instead.
    """
    try:
        cid = uuid.UUID(conversation_id).hex
        with open_dir(conversations_dir.parent, conversations_dir.name, cid, "events") as fd:
            numbered = sorted(
                (int(m.group(1)), n) for n in os.listdir(fd) if (m := _EVENT_FILE.match(n))
            )
            chosen = numbered[-limit:] if last else numbered[since : since + limit]
            out = []
            for _, name in chosen:
                try:
                    raw = read_json_at(fd, name, _MAX_EVENT_BYTES)
                except (OSError, ValueError):
                    continue
                if isinstance(raw, dict):
                    out.append(summarize_event(raw, text_limit))
            return out
    except (OSError, ValueError):
        return []
