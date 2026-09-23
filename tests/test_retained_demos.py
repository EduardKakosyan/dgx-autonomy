"""The demo of an ended run stays reachable across controller and DGX restarts.

Found on hugo-dgx1 after the Phase 4 reboots: a finished run's sandbox stayed down
after the DGX restarted, a demo relaunched during a stop stayed `starting` in
`status` although it was listening, and an expired run showed its conversation as
`running` (the last status the killed Agent Server had saved).
"""

from __future__ import annotations

import json
import uuid

from dgx_autonomy.openhands_adapter import conversation_id_for
from dgx_autonomy.runtime import agent_container_name

from fakes import Harness


def _with_demo(h: Harness) -> str:
    run_id = h.running_run(budget_hours=8)
    request = {"id": "d1", "command": "python3 -m http.server 3000", "port": 3000}
    state_dir = h.controller.paths(run_id).agent_dir / ".dgx"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "demo-request.json").write_text(json.dumps(request))
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    return run_id


def _persist_status(h: Harness, run_id: str, status: str) -> None:
    cid = uuid.UUID(conversation_id_for(run_id)).hex
    d = h.controller.paths(run_id).conversations_dir / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "base_state.json").write_text(json.dumps({"execution_status": status}))


def _restart(h: Harness) -> dict[str, list[str]]:
    controller = h.restart_controller()
    controller.acquire_writer()
    return controller.reconcile_on_start()


def _roles(h: Harness, run_id: str) -> list[str]:
    demo = h.state.get_demo(run_id)
    procs = h.runtime.sandbox_processes(run_id, demo.session_id if demo else None)
    return sorted(p.role for p in procs or ())


def test_a_finished_runs_demo_comes_back_demo_only_after_a_reboot(harness: Harness) -> None:
    h = harness
    run_id = _with_demo(h)
    h.conversation.status = "finished"
    h.controller.reconcile_once()
    _persist_status(h, run_id, "finished")
    old = h.state.get_demo(run_id)
    assert old is not None

    h.runtime.reboot()
    summary = _restart(h)

    assert summary["demos"] == [run_id]
    assert h.runtime.started == [agent_container_name(run_id)]
    assert (h.controller.paths(run_id).control_dir / "mode").read_text() == "demo-only\n"
    assert _roles(h, run_id) == ["demo", "supervisor"]  # no Agent Server
    assert h.settings.inference_name not in h.runtime.started
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.state == "starting" and demo.session_id != old.session_id

    # The loop sees it listening, although the run itself is not reconciled any more.
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    view = h.controller.handle("status", {"run_id": run_id})
    assert view["phase"] == "finished"
    assert view["demo"]["state"] == "running" and view["demo"]["listening"] is True
    # The Agent Server is not back; the status comes from what the SDK saved.
    assert view["conversation_status"] == "finished"
    assert h.conversation.delivered == []


def test_a_demo_relaunched_by_a_stop_is_seen_running(harness: Harness) -> None:
    h = harness
    run_id = _with_demo(h)
    h.runtime.reboot()
    h.controller.handle("stop", {"run_id": run_id})  # the sandbox was down: relaunched
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.state == "starting"
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.state == "running"
    assert (
        json.loads((h.controller.paths(run_id).control_dir / "demo.json").read_text())["state"]
        == "running"
    )


def test_an_expired_run_cut_off_mid_step_reads_interrupted(harness: Harness) -> None:
    h = harness
    run_id = _with_demo(h)
    _persist_status(h, run_id, "running")  # what the killed Agent Server left on disk
    h.runtime.reboot()
    h.controller.expire(run_id)
    view = h.controller.handle("status", {"run_id": run_id})
    assert (view["phase"], view["outcome"]) == ("stopped", "expired")
    assert view["conversation_status"] == "interrupted"


def test_a_removed_sandbox_and_a_failed_demo_stay_down(harness: Harness) -> None:
    h = harness
    kept_away = _with_demo(h)
    h.controller.handle("stop", {"run_id": kept_away})
    broken = _with_demo(h)
    h.state.set_demo_state(broken, state="failed", message="exited", now=h.clock.now())
    h.controller.handle("stop", {"run_id": broken})

    h.runtime.reboot()
    del h.runtime.containers[agent_container_name(kept_away)]  # the operator removed it
    summary = _restart(h)

    assert summary["demos"] == []
    assert h.runtime.started == []
