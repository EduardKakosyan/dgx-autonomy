"""write_handoff and declare_blocked (they run in the Agent Server) against a fake answer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from dgx_autonomy import handoff_tool
from dgx_autonomy.handoff_tool import (
    DeclareBlockedAction,
    DeclareBlockedExecutor,
    RoadmapItem,
    WriteHandoffAction,
    WriteHandoffExecutor,
)
from dgx_autonomy.openhands_adapter import BLOCKED_TOOL_NAME, HANDOFF_TOOL_NAME
from dgx_autonomy.runtime import AGENT_CONTROL_DIR, AGENT_STATE_DIR


def test_the_protocol_matches_the_controller_side() -> None:
    assert handoff_tool.WriteHandoffTool.name == HANDOFF_TOOL_NAME
    assert handoff_tool.DeclareBlockedTool.name == BLOCKED_TOOL_NAME
    assert f"{AGENT_STATE_DIR}/handoff.json" == handoff_tool.HANDOFF_REQUEST_PATH
    assert f"{AGENT_CONTROL_DIR}/handoff.json" == handoff_tool.HANDOFF_STATUS_PATH
    assert f"{AGENT_STATE_DIR}/blocked.json" == handoff_tool.BLOCKED_REQUEST_PATH
    assert f"{AGENT_CONTROL_DIR}/blocked.json" == handoff_tool.BLOCKED_STATUS_PATH


def _answer(request: Path, status: Path, accepted: bool, problems: list[str]) -> threading.Thread:
    status.parent.mkdir(parents=True, exist_ok=True)

    def run() -> None:
        for _ in range(500):
            if request.exists():
                rid = json.loads(request.read_text())["id"]
                status.write_text(json.dumps({"request_id": rid, "accepted": accepted,
                                              "problems": problems, "message": "m"}))  # fmt: skip
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_write_handoff_sends_the_handoff_and_reports_the_problems(tmp_path: Path) -> None:
    request, status = tmp_path / ".dgx" / "handoff.json", tmp_path / "control" / "handoff.json"
    helper = _answer(request, status, False, ["roadmap[0] is done but names no evidence"])
    executor = WriteHandoffExecutor(str(request), str(status), wait_s=5, poll_s=0.01)
    obs = executor(
        WriteHandoffAction(
            request_id="r1",
            summary="half done",
            roadmap=[RoadmapItem(item="step 1", status="done")],
            next_steps=["step 2"],
        )
    )
    helper.join(5)
    assert obs.is_error and "names no evidence" in obs.text
    sent = json.loads(request.read_text())
    assert sent["request_id"] == "r1" and sent["roadmap"][0]["status"] == "done"
    assert "kind" not in sent


def test_declare_blocked_reports_acceptance(tmp_path: Path) -> None:
    request, status = tmp_path / ".dgx" / "blocked.json", tmp_path / "control" / "blocked.json"
    helper = _answer(request, status, True, [])
    executor = DeclareBlockedExecutor(str(request), str(status), wait_s=5, poll_s=0.01)
    obs = executor(
        DeclareBlockedAction(
            missing_capability="a GPU", alternatives_tried=["a", "b"], needed="a GPU"
        )
    )
    helper.join(5)
    assert not obs.is_error and obs.text == "m"
    assert json.loads(request.read_text())["alternatives_tried"] == ["a", "b"]


def test_no_answer_is_reported_as_such(tmp_path: Path) -> None:
    executor = DeclareBlockedExecutor(
        str(tmp_path / "b.json"), str(tmp_path / "s.json"), wait_s=0.05, poll_s=0.01
    )
    obs = executor(DeclareBlockedAction(missing_capability="x", alternatives_tried=[], needed=""))
    assert obs.is_error and "has not answered" in obs.text
