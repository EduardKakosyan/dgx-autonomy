"""REST side of the OpenHands adapter, against a fake Agent Server. No SDK import."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from dgx_autonomy.openhands_adapter import (
    ConversationError,
    OpenHandsConversations,
    conversation_id_for,
    persisted_events,
    persisted_status,
    summarize_event,
)
from dgx_autonomy.ports import HttpResponse, ServerRef

from fakes import FakeHttp

SERVER = ServerRef(url="http://agent:8000", api_key="k")
CID = "7f2c0a8e-0000-0000-0000-000000000000"
BASE = f"http://agent:8000/api/conversations/{CID}"


def test_conversation_ids_are_stable_per_run() -> None:
    assert conversation_id_for("r1") == conversation_id_for("r1")
    assert conversation_id_for("r1") != conversation_id_for("r2")


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (
            {"kind": "MessageEvent", "llm_message": {"content": [{"type": "text", "text": "hi"}]}},
            "hi",
        ),
        (
            {
                "kind": "ActionEvent",
                "tool_name": "terminal",
                "action": {"kind": "X", "command": "ls"},
            },
            'terminal {"command": "ls"}',
        ),
        (
            {
                "kind": "ObservationEvent",
                "tool_name": "terminal",
                "observation": {"content": [{"type": "text", "text": "hello.txt"}]},
            },
            "terminal: hello.txt",
        ),
        ({"kind": "AgentErrorEvent", "error": "bad tool call"}, "bad tool call"),
        ({"kind": "SomethingNew"}, "SomethingNew"),
    ],
)
def test_summarize_event(raw: dict[str, object], text: str) -> None:
    assert summarize_event(raw).text == text


def test_long_event_text_is_clipped() -> None:
    raw = {"kind": "AgentErrorEvent", "error": "x" * 1000}
    assert len(summarize_event(raw).text) == 300


def test_inspect_reads_status_and_newest_event() -> None:
    http = FakeHttp()
    http.routes[BASE] = HttpResponse(200, {"execution_status": "running"})
    http.routes[f"{BASE}/events/search?sort_order=TIMESTAMP_DESC&limit=1"] = HttpResponse(
        200, {"items": [{"kind": "AgentErrorEvent", "id": "e9", "error": "oops"}]}
    )
    snap = OpenHandsConversations(http).inspect(SERVER, CID)
    assert snap.status == "running"
    assert snap.last_event is not None and snap.last_event.text == "oops"


def test_start_attaches_to_an_existing_conversation_without_resending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp()
    http.routes[BASE] = HttpResponse(200, {"execution_status": "running"})
    http.routes[f"{BASE}/events/search?source=user&limit=100"] = HttpResponse(
        200, {"items": [{"kind": "MessageEvent"}]}
    )
    adapter = OpenHandsConversations(http)
    monkeypatch.setattr(adapter, "_create", lambda request: pytest.fail("must not create"))
    from dgx_autonomy.ports import ConversationRequest, LlmEndpoint

    request = ConversationRequest(
        server=SERVER,
        conversation_id=CID,
        working_dir="/workspace/project",
        llm=LlmEndpoint("m", "http://inference:8080/v1", 65536),
        message="brief",
    )
    assert adapter.start(request) == CID
    assert not any(method == "POST" for method, _, _ in http.calls)


def test_start_sends_the_brief_once_after_creating(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp()
    created: list[str] = []
    http.routes[BASE] = lambda method, body: HttpResponse(200 if created else 404, {})
    http.routes[f"{BASE}/events/search?source=user&limit=100"] = HttpResponse(200, {"items": []})
    http.routes[f"{BASE}/events"] = HttpResponse(200, {"success": True})
    adapter = OpenHandsConversations(http)
    monkeypatch.setattr(adapter, "_create", lambda request: created.append(request.conversation_id))
    from dgx_autonomy.ports import ConversationRequest, LlmEndpoint

    request = ConversationRequest(
        server=SERVER,
        conversation_id=CID,
        working_dir="/workspace/project",
        llm=LlmEndpoint("m", "http://inference:8080/v1", 65536),
        message="the brief",
    )
    adapter.start(request)
    assert created == [CID]
    posts = [(url, body) for method, url, body in http.calls if method == "POST"]
    assert posts == [
        (
            f"{BASE}/events",
            {"role": "user", "content": [{"type": "text", "text": "the brief"}], "run": True},
        )
    ]


def test_http_failures_raise_conversation_error() -> None:
    with pytest.raises(ConversationError, match="connection refused"):
        OpenHandsConversations(FakeHttp()).inspect(SERVER, CID)


def test_events_page_and_offset() -> None:
    http = FakeHttp()
    page1 = [{"kind": "AgentErrorEvent", "error": f"e{i}"} for i in range(100)]
    page2 = [{"kind": "AgentErrorEvent", "error": f"e{i}"} for i in range(100, 130)]
    http.routes[f"{BASE}/events/search?limit=100"] = HttpResponse(
        200, {"items": page1, "next_page_id": "p2"}
    )
    http.routes[f"{BASE}/events/search?limit=100&page_id=p2"] = HttpResponse(
        200, {"items": page2, "next_page_id": None}
    )
    events = OpenHandsConversations(http).events(SERVER, CID, since=98, limit=5)
    assert [e.text for e in events] == ["e98", "e99", "e100", "e101", "e102"]


def _persist(root: Path, cid: str, events: list[dict[str, object]], status: str) -> Path:
    conv = root / "conversations" / uuid.UUID(cid).hex
    (conv / "events").mkdir(parents=True)
    (conv / "base_state.json").write_text(json.dumps({"execution_status": status}))
    for i, ev in enumerate(events):
        (conv / "events" / f"event-{i:05d}-{uuid.uuid4()}.json").write_text(json.dumps(ev))
    return root / "conversations"


def test_persisted_events_and_status_after_the_agent_server_is_gone(tmp_path: Path) -> None:
    evs = [{"kind": "AgentErrorEvent", "error": f"e{i}"} for i in range(12)]
    convs = _persist(tmp_path, CID, evs, "paused")
    assert persisted_status(convs, CID) == "paused"
    assert [e.text for e in persisted_events(convs, CID, 9, 5)] == ["e9", "e10", "e11"]
    assert [e.text for e in persisted_events(convs, CID, 0, 1, last=True)] == ["e11"]
    assert persisted_events(convs, "8f2c0a8e-0000-0000-0000-000000000000", 0, 5) == []


def test_persisted_events_do_not_follow_agent_symlinks(tmp_path: Path) -> None:
    convs = _persist(tmp_path, CID, [{"kind": "AgentErrorEvent", "error": "ok"}], "paused")
    outside = tmp_path / "controller-only.json"
    outside.write_text(json.dumps({"kind": "AgentErrorEvent", "error": "leaked"}))
    events_dir = convs / uuid.UUID(CID).hex / "events"
    os.symlink(outside, events_dir / f"event-00001-{uuid.uuid4()}.json")
    assert [e.text for e in persisted_events(convs, CID, 0, 10)] == ["ok"]
    state = convs / uuid.UUID(CID).hex / "base_state.json"
    state.unlink()
    os.symlink(outside, state)
    assert persisted_status(convs, CID) is None
