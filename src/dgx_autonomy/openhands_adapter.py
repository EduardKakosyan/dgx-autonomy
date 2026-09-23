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
    POST /api/conversations/{id}/run | /pause
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

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
_NAMESPACE = uuid.UUID("5f0c1d0e-8a57-4d8e-9c55-6a2f3f1d9b21")
_TEXT_LIMIT = 300


class ConversationError(RuntimeError):
    pass


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


def summarize_event(raw: Mapping[str, Any]) -> EventSummary:
    """One readable line per SDK event. Defensive: unknown shapes fall back to the kind."""
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
    return EventSummary(
        id=str(raw.get("id", "")),
        timestamp=str(raw.get("timestamp", "")),
        kind=kind,
        source=str(raw.get("source", "")),
        text=_clip(text) if text else kind,
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
        if not self.exists(request.server, cid):
            self._create(request)
        if not self._has_user_message(request.server, cid):
            self._send(request.server, cid, request.message, run=True)
        return cid

    def _create(self, request: ConversationRequest) -> None:
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
            usage_id="agent",
        )
        # cli_mode drops the browser tool set; Phase 1 needs terminal + file editor.
        agent = get_default_agent(llm=llm, cli_mode=True)
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
        status = str(body.get("execution_status", "unknown"))
        last = self._call(
            server,
            "GET",
            f"{CONVERSATIONS}/{conversation_id}/events/search?sort_order=TIMESTAMP_DESC&limit=1",
            timeout=10.0,
        )
        items = last.body.get("items") if isinstance(last.body, dict) else None
        last_event = summarize_event(items[0]) if items else None
        return ConversationSnapshot(conversation_id, status, last_event)

    def resume(self, server: ServerRef, conversation_id: str) -> None:
        # 409 = already running, which is what we want.
        self._call(
            server, "POST", f"{CONVERSATIONS}/{conversation_id}/run", accept=frozenset({409})
        )

    def pause(self, server: ServerRef, conversation_id: str) -> None:
        self._call(server, "POST", f"{CONVERSATIONS}/{conversation_id}/pause")

    def deliver(self, server: ServerRef, conversation_id: str, evidence: EvidenceMessage) -> None:
        self._send(server, conversation_id, evidence.text, run=True)

    def events(
        self, server: ServerRef, conversation_id: str, since: int, limit: int
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
                    out.append(summarize_event(raw))
                seen += 1
            page_id = body.get("next_page_id")
            if not page_id or not items:
                break
        return out
