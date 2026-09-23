"""A crashed controller, and a crashed DGX-side stack, continue the same run.

The tests run as the operator, who has no Docker access, so crashes are injected
through the controller (`fault.inject`): the controller SIGKILLs itself (compose
brings it back, like after `docker kill`), and it SIGKILLs the sandbox or the owned
llama-server the way a crash or a power loss would stop them.

1. The controller dies mid-run. The new one reattaches: same conversation, the same
   single agent container, no recovery, no second user message.
2. llama-server, the sandbox and the controller all die mid-run: everything a DGX
   restart takes down except the host. The new controller brings the model and the
   sandbox back and resumes the one conversation, which the restarted Agent Server
   marked ERROR. The run still finishes the brief.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy.control_api import ControlError

from dgx_helpers import wait_for

pytestmark = pytest.mark.dgx

BUDGET_HOURS = 0.75

BRIEF = """\
# Brief: three files, with waits in between

Do these steps in order. Each is required.

1. Create `step1.txt` in the project directory containing: one
2. Run `sleep 90` in the terminal, with the terminal timeout set to 120 seconds, and
   wait for it to complete.
3. Create `step2.txt` containing: two
4. Run `sleep 90` again, the same way.
5. Create `done.txt` containing: done
6. Check that all three files exist with `ls`, then finish.
"""


def _events(control: Callable[..., Any], run_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    since = 0
    while True:
        page = control("logs", {"run_id": run_id, "since": since, "limit": 500})
        out += page["events"]
        if not page["events"]:
            return out
        since = page["next"]


def _user_messages(control: Callable[..., Any], run_id: str) -> list[str]:
    return [
        e["text"]
        for e in _events(control, run_id)
        if e["kind"] == "MessageEvent" and e["source"] == "user"
    ]


def _launch_and_wait_for_sleep(control: Callable[..., Any]) -> dict[str, Any]:
    run_id = control("launch", {"brief_text": BRIEF, "budget_hours": BUDGET_HOURS})["run_id"]
    wait_for(
        lambda: _events(control, run_id),
        lambda evs: any(e["kind"] == "ActionEvent" and "sleep 90" in e["text"] for e in evs),
        timeout_s=15 * 60,
        every_s=10,
    )
    status = control("status", {"run_id": run_id})
    assert status["phase"] == "running", status
    return status


def _agent_containers(status: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in status["containers"] if c["name"].startswith("dgx-autonomy-agent-")]


def _crash_controller_and_wait(control: Callable[..., Any]) -> dict[str, Any]:
    before = control("ping")["controller"]
    assert control("fault.inject", {"target": "controller", "confirm": "controller"})["crashing"]

    def ping() -> dict[str, Any] | None:
        try:
            return dict(control("ping", timeout=10))
        except ControlError:
            return None

    # Compose restarts it; the new process has taken the writer lock over.
    after = wait_for(
        ping,
        lambda p: p is not None and p["controller"]["token"] != before["token"],
        timeout_s=5 * 60,
        every_s=3,
    )
    return dict(after["controller"])


def _wait_finished(control: Callable[..., Any], run_id: str) -> dict[str, Any]:
    status = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: s["phase"] in ("finished", "failed", "stopped"),
        timeout_s=BUDGET_HOURS * 3600 + 10 * 60,
        every_s=15,
    )
    print(json.dumps(status, indent=2, default=str))
    return dict(status)


def _assert_brief_done(status: dict[str, Any]) -> None:
    project = Path(status["workspace_dir"])
    assert (project / "done.txt").read_text().strip() == "done"
    assert (project / "step1.txt").exists() and (project / "step2.txt").exists()


def test_a_killed_controller_reattaches_to_the_same_run(control: Callable[..., Any]) -> None:
    before = _launch_and_wait_for_sleep(control)
    run_id = before["run_id"]
    [agent] = _agent_containers(before)

    _crash_controller_and_wait(control)

    after = control("status", {"run_id": run_id})
    assert after["phase"] == "running", after
    assert after["conversation_id"] == before["conversation_id"]
    assert after["deadline_at"] == before["deadline_at"]
    assert _agent_containers(after) == [agent]  # the same one, still running
    assert after["recoveries"] == []

    final = _wait_finished(control, run_id)
    assert final["phase"] == "finished", final
    assert final["conversation_id"] == before["conversation_id"]
    assert final["recoveries"] == []
    assert len(_user_messages(control, run_id)) == 1  # the brief, nothing re-sent
    _assert_brief_done(final)


def test_a_crashed_stack_resumes_the_same_conversation(control: Callable[..., Any]) -> None:
    before = _launch_and_wait_for_sleep(control)
    run_id = before["run_id"]
    [agent] = _agent_containers(before)

    for target in ("inference", "sandbox"):
        killed = control("fault.inject", {"target": target, "confirm": target, "run_id": run_id})
        assert killed["killed"], killed
    started = time.monotonic()
    _crash_controller_and_wait(control)

    status = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: bool(s["recoveries"]) and s["recoveries"][-1]["status"] != "intended",
        timeout_s=20 * 60,
        every_s=10,
    )
    print(f"recovered in {time.monotonic() - started:.0f}s:")
    print(json.dumps(status["recoveries"], indent=2))
    rec = status["recoveries"][-1]
    assert rec["status"] == "done", rec
    assert rec["status_before"] == "running"
    # The restarted Agent Server marks the interrupted conversation ERROR (SDK 1.49.4).
    assert rec["steps"]["resumed_from"] == "error", rec
    assert status["conversation_id"] == before["conversation_id"]
    assert status["deadline_at"] == before["deadline_at"]
    [again] = _agent_containers(status)
    assert again["id"] == agent["id"] and again["running"]  # started again, not a new one

    final = _wait_finished(control, run_id)
    assert final["phase"] == "finished", final
    assert final["conversation_id"] == before["conversation_id"]
    messages = _user_messages(control, run_id)
    assert len(messages) == 2, messages  # the brief and the one recovery notice
    assert "sandbox was restarted" in messages[1]
    _assert_brief_done(final)
