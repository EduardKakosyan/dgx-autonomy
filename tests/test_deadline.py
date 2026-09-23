"""The deadline watchdog: independent of SDK calls, driven by the injected clock."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from dgx_autonomy.deadline import DeadlineWatchdog
from dgx_autonomy.runtime import agent_container_name

from fakes import Harness

BRIEF = "Serve a static page on port 3000.\n"


def _phase(h: Harness, run_id: str) -> str:
    run = h.state.get_run(run_id)
    assert run is not None
    return run.phase


def test_nothing_expires_before_the_deadline(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    watchdog = h.controller.deadline_watchdog()
    h.clock.advance(minutes=59)
    assert watchdog.check_once() == []
    assert _phase(h, run_id) == "running"
    assert h.conversation.paused == []


def test_expiry_stops_the_agent_and_records_the_outcome(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    watchdog = h.controller.deadline_watchdog()
    h.clock.advance(hours=1)  # exactly at the deadline counts as expired
    assert watchdog.check_once() == [run_id]
    run = h.state.get_run(run_id)
    assert run is not None
    assert (run.phase, run.outcome, run.stop_requested) == ("stopped", "expired", True)
    assert h.conversation.paused == [run.conversation_id]
    # The deadline itself never moves.
    assert run.deadline_at - run.launched_at == timedelta(hours=1)
    # Expired runs are not expired twice.
    h.clock.advance(hours=1)
    assert watchdog.check_once() == []


def test_expiry_fires_while_an_sdk_call_is_blocked(harness: Harness) -> None:
    """The reconcile loop hangs inside the SDK; the watchdog stops the run anyway."""
    h = harness
    run_id = str(h.controller.handle("launch", {"brief_text": BRIEF, "budget_hours": 1})["run_id"])
    h.inference_ready()
    h.agent_healthy(run_id)
    gate = threading.Event()
    h.conversation.start_gate = gate  # conversation start will hang until released
    loop = threading.Thread(target=h.controller.reconcile_once, daemon=True)
    loop.start()
    assert h.conversation.start_entered.wait(5), "reconcile never reached the SDK call"

    h.clock.advance(hours=1, seconds=1)
    assert h.controller.deadline_watchdog().check_once() == [run_id]

    # Decided while the reconcile loop is still stuck in the SDK call.
    assert loop.is_alive()
    run = h.state.get_run(run_id)
    assert run is not None and (run.phase, run.outcome) == ("stopped", "expired")
    assert "kill-agent keep=None" in h.trace
    assert [p for p in h.runtime.procs[run_id] if p.role == "agent"] == []

    # When the SDK call finally returns, the run does not come back to life.
    gate.set()
    loop.join(5)
    assert not loop.is_alive()
    assert _phase(h, run_id) == "stopped"
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "stopped"
    assert h.runtime.runs.count(agent_container_name(run_id)) == 1


def test_the_watchdog_thread_fires_on_its_own(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    watchdog = DeadlineWatchdog(
        state=h.state, clock=h.clock, on_expired=h.controller.expire, interval_s=0.01
    )
    watchdog.start()
    try:
        h.clock.advance(hours=2)
        end = time.monotonic() + 5
        while _phase(h, run_id) != "stopped" and time.monotonic() < end:
            time.sleep(0.01)
    finally:
        watchdog.stop()
    assert _phase(h, run_id) == "stopped"


def test_a_deadline_that_passed_while_the_controller_was_down(harness: Harness) -> None:
    """Launched, never reconciled, and the clock is already past the deadline."""
    h = harness
    run_id = str(h.controller.handle("launch", {"brief_text": BRIEF, "budget_hours": 1})["run_id"])
    h.clock.advance(hours=3)
    assert h.controller.deadline_watchdog().check_once() == [run_id]
    run = h.state.get_run(run_id)
    assert run is not None and (run.phase, run.outcome) == ("stopped", "expired")
    # Nothing was ever started for it, and the reconcile loop starts nothing now.
    h.inference_ready()
    h.controller.reconcile_once()
    assert h.runtime.runs == []
    assert h.conversation.started == []


def test_watchdog_errors_do_not_stop_the_checks(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    calls: list[str] = []

    def boom(rid: str) -> None:
        calls.append(rid)
        raise RuntimeError("docker is gone")

    watchdog = DeadlineWatchdog(state=h.state, clock=h.clock, on_expired=boom)
    h.clock.advance(hours=2)
    assert watchdog.check_once() == [run_id]
    assert calls == [run_id]
