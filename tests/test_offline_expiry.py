"""A deadline that passed while the controller, or the DGX, was down.

The run is recorded as expired and its agent execution ends; nothing resumes it.
The demo is kept: a sandbox that went down with the DGX comes back demo-only, with
the recorded demo relaunched.
"""

from __future__ import annotations

import json
from datetime import timedelta

from dgx_autonomy.runtime import agent_container_name

from fakes import Harness


def _with_demo(h: Harness) -> str:
    run_id = h.running_run()
    request = {"id": "d1", "command": "python3 -m http.server 3000", "port": 3000}
    state_dir = h.controller.paths(run_id).agent_dir / ".dgx"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "demo-request.json").write_text(json.dumps(request))
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    return run_id


def _past_deadline(h: Harness, run_id: str) -> None:
    run = h.state.get_run(run_id)
    assert run is not None
    h.clock.advance(seconds=(run.deadline_at - h.clock.now()).total_seconds() + 3600)


def _roles(h: Harness, run_id: str) -> list[str]:
    demo = h.state.get_demo(run_id)
    procs = h.runtime.sandbox_processes(run_id, demo.session_id if demo else None)
    return sorted(p.role for p in procs or ())


def test_a_deadline_that_passed_while_the_dgx_was_off_expires_the_run(harness: Harness) -> None:
    h = harness
    run_id = _with_demo(h)
    before = h.state.get_run(run_id)
    assert before is not None

    h.runtime.reboot()
    _past_deadline(h, run_id)
    controller = h.restart_controller()
    controller.acquire_writer()
    summary = controller.reconcile_on_start()
    assert summary["expired"] == [run_id] and summary["resuming"] == []

    run = h.state.get_run(run_id)
    assert run is not None
    assert (run.phase, run.outcome) == ("stopped", "expired")
    assert run.deadline_at == before.deadline_at
    evidence = json.loads(run.stop_evidence or "{}")
    assert evidence["failed"] is False and evidence["container_running"] is False

    # Nothing resumed: no model, no Agent Server, no message to the conversation.
    assert h.settings.inference_name not in h.runtime.started
    assert h.conversation.delivered == [] and h.conversation.resumed == []
    assert h.state.recoveries(run_id) == []
    # The sandbox is back only for the demo, which runs the recorded command again.
    assert h.runtime.started == [agent_container_name(run_id)]
    assert (h.controller.paths(run_id).control_dir / "mode").read_text() == "demo-only\n"
    assert _roles(h, run_id) == ["demo", "supervisor"]
    assert evidence["demo"]["relaunched"] is True
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.command == "python3 -m http.server 3000"

    # Later ticks leave it alone.
    for _ in range(3):
        controller.reconcile_once()
    assert _roles(h, run_id) == ["demo", "supervisor"]
    assert h.runtime.started == [agent_container_name(run_id)]


def test_a_run_still_launching_when_the_dgx_went_down_expires_without_starting(
    harness: Harness,
) -> None:
    h = harness
    run_id = str(h.controller.handle("launch", {"brief_text": "x", "budget_hours": 1})["run_id"])
    h.inference_loading()
    h.controller.reconcile_once()
    h.runtime.reboot()
    _past_deadline(h, run_id)
    controller = h.restart_controller()
    controller.acquire_writer()
    assert controller.reconcile_on_start()["expired"] == [run_id]
    controller.reconcile_once()

    run = h.state.get_run(run_id)
    assert run is not None and (run.phase, run.outcome) == ("stopped", "expired")
    assert h.runtime.started == []
    assert h.runtime.runs == [h.settings.inference_name]
    assert h.conversation.started == []


def test_a_deadline_that_passed_while_only_the_controller_was_down(harness: Harness) -> None:
    """The sandbox kept running: its agent is ended in place and the demo is untouched."""
    h = harness
    run_id = _with_demo(h)
    session = h.state.get_demo(run_id)
    assert session is not None
    tool = h.runtime.agent_tool(run_id)

    _past_deadline(h, run_id)
    controller = h.restart_controller()
    controller.acquire_writer()
    assert controller.reconcile_on_start()["expired"] == [run_id]

    run = h.state.get_run(run_id)
    assert run is not None and (run.phase, run.outcome) == ("stopped", "expired")
    evidence = json.loads(run.stop_evidence or "{}")
    assert evidence["container_running"] is True and evidence["survivors"] == []
    assert tool in [p["pid"] for p in evidence["after_pause"]]
    assert evidence["demo"]["relaunched"] is False
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.session_id == session.session_id
    assert _roles(h, run_id) == ["demo", "supervisor"]
    assert h.runtime.started == []


def test_the_deadline_passing_during_a_recovery_ends_it(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    h.runtime.reboot()
    h.clock.advance(seconds=60)
    controller = h.restart_controller()
    controller.acquire_writer()
    controller.reconcile_on_start()
    h.inference_loading()
    controller.reconcile_once()
    assert h.state.open_recovery(run_id) is not None

    # The model takes longer to load than the run has left.
    _past_deadline(h, run_id)
    assert controller.deadline_watchdog().check_once() == [run_id]
    h.inference_ready()
    controller.reconcile_once()

    run = h.state.get_run(run_id)
    assert run is not None and (run.phase, run.outcome) == ("stopped", "expired")
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "failed" and "expired" in (rec.error or "")
    assert h.conversation.delivered == []
    assert agent_container_name(run_id) not in h.runtime.started
    assert timedelta(0) < run.deadline_at - run.launched_at
