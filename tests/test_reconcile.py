"""A restarted controller continues the same run: inspect first, never duplicate.

Two kinds of interruption:

- Startup reconciliation: an operation still `intended` when the controller died is
  inspected (what did the interrupted attempt leave behind?) before it is repeated.
  Table-driven below: each operation x {resource running, stopped, missing}.
- Recovery: a run whose llama-server or sandbox is down (the DGX restarted, a
  container crashed) is brought back, and its one conversation is resumed.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest

from dgx_autonomy.controller import RequestError
from dgx_autonomy.openhands_adapter import ConversationError, conversation_id_for
from dgx_autonomy.runtime import agent_container_name

from fakes import Harness, load_egress_policy

BRIEF = "Create hello.txt in the project directory containing today's date.\n"
DOWNTIME = timedelta(hours=2)  # longer than any start timeout


def _launch(h: Harness) -> str:
    return str(h.controller.handle("launch", {"brief_text": BRIEF, "budget_hours": 8})["run_id"])


def _phase(h: Harness, run_id: str) -> str:
    run = h.state.get_run(run_id)
    assert run is not None
    return run.phase


def _restart(h: Harness, downtime: timedelta = DOWNTIME) -> dict[str, list[str]]:
    """The controller process dies and compose brings a new one up after `downtime`."""
    h.clock.advance(seconds=downtime.total_seconds())
    controller = h.restart_controller()
    controller.acquire_writer()
    return controller.reconcile_on_start()


def _persist_status(h: Harness, run_id: str, status: str) -> None:
    """What the SDK wrote to base_state.json before the Agent Server went down."""
    cid = uuid.UUID(conversation_id_for(run_id)).hex
    d = h.controller.paths(run_id).conversations_dir / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "base_state.json").write_text(json.dumps({"execution_status": status}))


# --- startup reconciliation: each operation x what its attempt left behind --------


def _interrupted_at_inference(h: Harness) -> str:
    run_id = _launch(h)
    h.inference_loading()
    h.controller.reconcile_once()
    return run_id


def _interrupted_at_workspace(h: Harness) -> str:
    run_id = _launch(h)
    h.inference_ready()
    h.controller.reconcile_once()  # the sandbox starts; its Agent Server is not healthy yet
    return run_id


def _interrupted_at_conversation(h: Harness) -> str:
    run_id = _launch(h)
    h.inference_ready()
    h.agent_healthy(run_id)
    h.conversation.fail_start = ConversationError("POST /events: HTTP 503")
    h.controller.reconcile_once()
    return run_id


INTERRUPTED: dict[str, Callable[[Harness], str]] = {
    "inference.start": _interrupted_at_inference,
    "workspace.create": _interrupted_at_workspace,
    "conversation.start": _interrupted_at_conversation,
}


def _container_of(h: Harness, run_id: str, kind: str) -> str | None:
    if kind == "inference.start":
        return h.settings.inference_name
    if kind == "workspace.create":
        return agent_container_name(run_id)
    return None


@pytest.mark.parametrize("kind", list(INTERRUPTED))
@pytest.mark.parametrize("observed", ["running", "stopped", "missing"])
def test_an_interrupted_operation_is_inspected_before_it_is_repeated(
    harness: Harness, kind: str, observed: str
) -> None:
    h = harness
    run_id = INTERRUPTED[kind](h)
    op = h.state.get_operation(run_id, kind)
    assert op is not None and op.status == "intended"
    name = _container_of(h, run_id, kind)
    if name is not None and observed == "stopped":
        h.runtime.exit(name, code=255)
    elif name is not None and observed == "missing":
        del h.runtime.containers[name]
    runs_before = list(h.runtime.runs)

    summary = _restart(h)

    assert op.id in summary["retried"]
    retried = h.state.get_operation(run_id, kind)
    assert retried is not None and retried.status == "intended"
    # The attempt's timeouts count from the restart, not from before the downtime.
    assert retried.attempted_at == h.clock.now()
    assert retried.created_at == op.created_at
    if name is not None:
        container = h.runtime.containers.get(name)
        expected = container.id if observed == "running" and container else None
        assert retried.resource_id == expected

    # The loop then finishes the launch with whatever was there.
    h.conversation.fail_start = None
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "running"
    if name is not None:
        if observed == "running":
            assert h.runtime.runs.count(name) == runs_before.count(name)
            assert name not in h.runtime.started
        elif observed == "stopped":
            assert h.runtime.started.count(name) == 1
            assert h.runtime.runs.count(name) == runs_before.count(name)
        else:
            assert h.runtime.runs.count(name) == runs_before.count(name) + 1
    # One conversation, whatever happened: every start asked for the same id.
    assert {r.conversation_id for r in h.conversation.started} == {conversation_id_for(run_id)}
    run = h.state.get_run(run_id)
    assert run is not None and run.conversation_id == conversation_id_for(run_id)
    assert h.state.recoveries(run_id) == []


def test_without_the_restart_a_slow_step_times_out_as_before(harness: Harness) -> None:
    """Startup reconciliation is what restarts a timer, not the passage of time."""
    h = harness
    run_id = _interrupted_at_inference(h)
    h.clock.advance(seconds=h.settings.inference_load_timeout_s + 1)
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "failed"


# --- the controller dies while the run is working ---------------------------------


def test_a_killed_controller_reattaches_to_the_same_conversation(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    run = h.state.get_run(run_id)
    assert run is not None

    summary = _restart(h, timedelta(seconds=20))
    assert summary == {"expired": [], "retried": [], "resuming": [run_id]}
    for _ in range(3):
        h.controller.reconcile_once()

    after = h.state.get_run(run_id)
    assert after is not None and after.phase == "running"
    assert after.conversation_id == run.conversation_id
    assert after.deadline_at == run.deadline_at
    assert len(h.conversation.started) == 1
    assert h.runtime.runs.count(agent_container_name(run_id)) == 1
    assert h.runtime.started == []
    # Nothing was down, so nothing was recovered or told to the agent.
    assert h.state.recoveries(run_id) == []
    assert h.conversation.delivered == [] and h.conversation.resumed == []

    h.conversation.status = "finished"
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "finished"


# --- recovery: the DGX restarted -----------------------------------------------------


def _with_running_demo(h: Harness) -> tuple[str, int]:
    run_id = h.running_run(budget_hours=8)
    request = {"id": "d1", "command": "python3 -m http.server 3000", "port": 3000}
    state_dir = h.controller.paths(run_id).agent_dir / ".dgx"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "demo-request.json").write_text(json.dumps(request))
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.state == "running" and demo.session_id is not None
    return run_id, demo.session_id


def test_after_a_reboot_the_run_resumes_the_same_conversation(harness: Harness) -> None:
    h = harness
    run_id, old_session = _with_running_demo(h)
    _persist_status(h, run_id, "running")
    h.trace.clear()

    h.runtime.reboot()
    _restart(h, timedelta(minutes=10))

    # First llama-server, and nothing else until it is ready.
    h.inference_loading()
    h.controller.reconcile_once()
    assert h.runtime.started == [h.settings.inference_name]
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "intended" and rec.status_before == "running"
    assert "llama-server is exited" in rec.cause and "agent sandbox is exited" in rec.cause
    assert _phase(h, run_id) == "running"

    # Then the sandbox, its Agent Server, the demo, and the conversation.
    h.inference_ready()
    h.controller.reconcile_once()
    assert h.runtime.started == [h.settings.inference_name, agent_container_name(run_id)]
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "done", rec
    assert rec.steps["resumed_from"] == "error"  # the Agent Server's crash marker
    [notice] = h.conversation.delivered
    assert "sandbox was restarted" in notice.text
    assert "demo was relaunched" in notice.text
    assert h.conversation.status == "running"

    # Same run, same conversation, same containers, same deadline.
    assert len(h.conversation.started) == 1
    assert h.runtime.runs.count(agent_container_name(run_id)) == 1
    assert h.runtime.runs.count(h.settings.inference_name) == 1
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.session_id != old_session and demo.state == "starting"
    assert h.trace.index("deliver") > next(i for i, t in enumerate(h.trace) if "demo-start" in t)

    # The run carries on as before.
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    view = h.controller.handle("status", {"run_id": run_id})
    assert view["phase"] == "running" and view["demo"]["state"] == "running"
    assert [r["status"] for r in view["recoveries"]] == ["done"]
    assert [c["running"] for c in view["containers"]] == [True]
    h.controller.reconcile_once()
    assert len(h.conversation.delivered) == 1


@pytest.mark.parametrize(("before", "phase"), [("finished", "finished"), ("error", "failed")])
def test_a_conversation_that_had_ended_is_not_resumed(
    harness: Harness, before: str, phase: str
) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    _persist_status(h, run_id, before)
    h.conversation.status = before  # it ended just before the DGX went down
    h.runtime.reboot()
    _restart(h)
    h.inference_ready()
    h.controller.reconcile_once()  # starts llama-server
    h.controller.reconcile_once()  # the sandbox, then the conversation
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "done" and rec.status_before == before
    assert h.conversation.delivered == []
    h.controller.reconcile_once()
    assert _phase(h, run_id) == phase


def test_a_crashed_sandbox_is_brought_back_without_a_reboot(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    _persist_status(h, run_id, "running")
    h.controller.handle("fault.inject", {"target": "sandbox", "confirm": "sandbox"})
    assert h.runtime.killed == [agent_container_name(run_id)]
    h.controller.reconcile_once()
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "done" and "exit code 137" in rec.cause
    assert h.runtime.started == [agent_container_name(run_id)]
    [notice] = h.conversation.delivered
    assert "sandbox was restarted" in notice.text and "demo" not in notice.text


def test_a_crashed_llama_server_is_restarted_and_a_running_conversation_left_alone(
    harness: Harness,
) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    h.controller.handle("fault.inject", {"target": "inference", "confirm": "inference"})
    h.controller.reconcile_once()  # starts llama-server again
    h.controller.reconcile_once()  # ready: the rest is checked
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "done" and rec.cause.startswith("llama-server is exited")
    assert h.runtime.started == [h.settings.inference_name]
    assert rec.steps["conversation_status"] == "running"
    assert h.conversation.delivered == []


def test_a_failed_recovery_backs_off_and_keeps_what_it_read(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    _persist_status(h, run_id, "running")
    h.runtime.reboot()
    h.settings.egress_marker.unlink()  # the host policy is not loaded on this boot
    _restart(h)
    h.inference_ready()
    h.controller.reconcile_once()
    h.controller.reconcile_once()
    [first] = h.state.recoveries(run_id)
    assert first.status == "failed" and "egress policy" in (first.error or "")
    assert agent_container_name(run_id) not in h.runtime.started

    # The failed attempt may already have restarted the Agent Server, which rewrote
    # the persisted status; the next attempt goes by what the first one read.
    _persist_status(h, run_id, "error")
    load_egress_policy(h.settings)
    h.controller.reconcile_once()
    assert len(h.state.recoveries(run_id)) == 1  # backing off
    h.clock.advance(seconds=h.settings.recovery_backoff_s + 1)
    h.controller.reconcile_once()
    _, second = h.state.recoveries(run_id)
    assert second.status == "done" and second.status_before == "running"
    assert len(h.conversation.delivered) == 1
    assert _phase(h, run_id) == "running"


def test_backoff_doubles_within_the_window(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    waits = []
    for _ in range(4):
        h.runtime.kill_container(agent_container_name(run_id))
        waited = 0.0
        h.controller.reconcile_once()
        while h.state.open_recovery(run_id) is None and not h.runtime.agent_server_up(run_id):
            h.clock.advance(seconds=5)
            waited += 5
            h.controller.reconcile_once()
        waits.append(waited)
    assert waits[0] == 0
    assert waits[1] >= h.settings.recovery_backoff_s
    assert waits[2] >= 2 * h.settings.recovery_backoff_s
    assert waits[3] >= 4 * h.settings.recovery_backoff_s
    assert [r.status for r in h.state.recoveries(run_id)] == ["done"] * 4


def test_a_recovery_interrupted_by_a_controller_restart_starts_over(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    _persist_status(h, run_id, "running")
    h.runtime.reboot()
    _restart(h)
    h.inference_loading()
    h.controller.reconcile_once()
    rec = h.state.open_recovery(run_id)
    assert rec is not None and "inference_started" in rec.steps

    # The DGX goes down again mid-recovery, for longer than the load timeout.
    h.runtime.reboot()
    _persist_status(h, run_id, "error")  # whatever the disk says now
    summary = _restart(h)
    assert rec.id in summary["retried"]
    again = h.state.open_recovery(run_id)
    assert again is not None and again.id == rec.id
    assert again.steps == {} and again.attempted_at == h.clock.now()
    assert again.status_before == "running"

    h.inference_ready()
    h.controller.reconcile_once()
    h.controller.reconcile_once()
    [done] = h.state.recoveries(run_id)
    assert done.status == "done" and h.runtime.started.count(h.settings.inference_name) == 2
    assert len(h.conversation.delivered) == 1


def test_stopping_a_run_abandons_its_recovery(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    h.runtime.reboot()
    _restart(h)
    h.inference_loading()
    h.controller.reconcile_once()
    assert h.state.open_recovery(run_id) is not None

    view = h.controller.handle("stop", {"run_id": run_id})
    assert view["phase"] == "stopped"
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "failed" and (rec.error or "").startswith("abandoned")
    assert h.conversation.delivered == []
    assert agent_container_name(run_id) not in h.runtime.started


# --- fault injection -----------------------------------------------------------------


def test_fault_injection_needs_confirmation(harness: Harness) -> None:
    h = harness
    with pytest.raises(RequestError, match="confirm"):
        h.controller.handle("fault.inject", {"target": "controller"})
    with pytest.raises(RequestError, match="target"):
        h.controller.handle("fault.inject", {"target": "host", "confirm": "host"})


def test_fault_injection_crashes_the_controller_after_replying(harness: Harness) -> None:
    h = harness
    reply = h.controller.handle("fault.inject", {"target": "controller", "confirm": "controller"})
    assert reply == {"target": "controller", "crashing": True}
    end = time.monotonic() + 5
    while "controller crashed" not in h.trace and time.monotonic() < end:
        time.sleep(0.05)
    assert "controller crashed" in h.trace


def test_fault_injection_leaves_an_ended_run_alone(harness: Harness) -> None:
    h = harness
    run_id = h.running_run(budget_hours=8)
    h.controller.handle("stop", {"run_id": run_id})
    with pytest.raises(RequestError, match="stopped"):
        h.controller.handle("fault.inject", {"target": "sandbox", "confirm": "sandbox"})
    assert h.runtime.killed == []
