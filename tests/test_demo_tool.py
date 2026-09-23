"""The start_demo tool (runs in the Agent Server) against a fake controller answer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from dgx_autonomy import demo_tool
from dgx_autonomy.config import Settings
from dgx_autonomy.demo_tool import StartDemoAction, StartDemoExecutor, StartDemoTool
from dgx_autonomy.openhands_adapter import DEMO_TOOL_NAME
from dgx_autonomy.runtime import AGENT_CONTROL_DIR, AGENT_STATE_DIR


def test_the_protocol_matches_the_controller_side() -> None:
    assert StartDemoTool.name == DEMO_TOOL_NAME == "start_demo"
    assert f"{AGENT_STATE_DIR}/demo-request.json" == demo_tool.REQUEST_PATH
    assert f"{AGENT_CONTROL_DIR}/demo.json" == demo_tool.STATUS_PATH
    assert Settings.demo_port == demo_tool.DEMO_PORT


def _executor(tmp_path: Path) -> StartDemoExecutor:
    return StartDemoExecutor(
        request_path=str(tmp_path / ".dgx" / "demo-request.json"),
        status_path=str(tmp_path / "control" / "demo.json"),
        wait_s=5,
        poll_s=0.01,
    )


def _answer_when_asked(tmp_path: Path, state: str, message: str = "") -> threading.Thread:
    """A controller that answers the first request it sees."""
    request = tmp_path / ".dgx" / "demo-request.json"
    status = tmp_path / "control" / "demo.json"
    status.parent.mkdir(parents=True, exist_ok=True)

    def run() -> None:
        for _ in range(500):
            if request.exists():
                rid = json.loads(request.read_text())["id"]
                status.write_text(
                    json.dumps({"request_id": rid, "state": state, "message": message})
                )
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_the_tool_writes_a_request_and_reports_the_answer(tmp_path: Path) -> None:
    helper = _answer_when_asked(tmp_path, "running", "listening on port 3000")
    obs = _executor(tmp_path)(StartDemoAction(command="python3 -m http.server 3000"))
    helper.join(5)
    assert not obs.is_error
    assert "running" in obs.text and "3000" in obs.text
    request = json.loads((tmp_path / ".dgx" / "demo-request.json").read_text())
    assert request["command"] == "python3 -m http.server 3000" and request["port"] == 3000


def test_a_refusal_is_an_error_observation(tmp_path: Path) -> None:
    helper = _answer_when_asked(tmp_path, "refused", "the demo must listen on port 3000")
    obs = _executor(tmp_path)(StartDemoAction(command="serve", port=8080))
    helper.join(5)
    assert obs.is_error and "refused" in obs.text and "port 3000" in obs.text


def test_an_old_answer_is_not_mistaken_for_this_one(tmp_path: Path) -> None:
    status = tmp_path / "control" / "demo.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"request_id": "older", "state": "running"}))
    ex = _executor(tmp_path)
    ex.wait_s = 0.1
    obs = ex(StartDemoAction(command="serve"))
    assert obs.is_error and "not picked up" in obs.text
