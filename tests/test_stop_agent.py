"""stop_agent: pause -> bounded wait -> kill the agent group -> verify gone -> keep the demo."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy.controller import RequestError
from dgx_autonomy.openhands_adapter import ConversationError
from dgx_autonomy.runtime import agent_container_name

from fakes import Harness


def _request_demo(
    h: Harness, run_id: str, command: str = "python3 -m http.server 3000", **extra: Any
) -> str:
    """What the start_demo tool does inside the sandbox: write the request file."""
    body = {"id": f"req{len(h.trace)}", "command": command, "port": 3000, **extra}
    d = h.controller.paths(run_id).agent_dir / ".dgx"
    d.mkdir(exist_ok=True)
    (d / "demo-request.json").write_text(json.dumps(body))
    return str(body["id"])


def _demo_answer(h: Harness, run_id: str) -> dict[str, Any]:
    return dict(json.loads((h.controller.paths(run_id).control_dir / "demo.json").read_text()))


def _with_demo(h: Harness) -> tuple[str, int]:
    run_id = h.running_run()
    rid = _request_demo(h, run_id)
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    answer = _demo_answer(h, run_id)
    assert (answer["request_id"], answer["state"]) == (rid, "running")
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.session_id is not None
    return run_id, demo.session_id


def _roles(h: Harness, run_id: str) -> list[str]:
    procs = h.runtime.sandbox_processes(run_id, None)
    assert procs is not None
    demo = h.state.get_demo(run_id)
    keep = demo.session_id if demo else None
    return ["demo" if keep and p.sid == keep else p.role for p in procs if p.role != "supervisor"]


def test_stop_sequence_pauses_waits_kills_verifies_and_keeps_the_demo(harness: Harness) -> None:
    h = harness
    run_id, demo_sid = _with_demo(h)
    tool = h.runtime.agent_tool(run_id, "sleep 100000")  # e.g. backgrounded under tmux
    h.trace.clear()

    view = h.controller.handle("stop", {"run_id": run_id})

    # Order: cancel the LLM call and pause, then kill what is left, sparing the demo.
    kill = f"kill-agent keep={demo_sid}"
    assert h.trace[:2] == ["interrupt", "pause"]
    assert kill in h.trace and h.trace.index(kill) > 1
    assert "restart" not in h.trace

    assert (view["phase"], view["outcome"]) == ("stopped", "stopped")
    ev = view["stop_evidence"]
    assert ev["failed"] is False
    assert ev["quiescent"] is True and ev["conversation_status"] == "paused"
    # The evidence says what outlived the pause: the Agent Server and the tool process.
    assert {p["cmd"] for p in ev["after_pause"]} == {
        "/usr/local/bin/openhands-agent-server",
        "sleep 100000",
    }
    assert ev["tool_processes_after_pause"] == 1
    assert ev["survivors"] == []
    assert ev["sandbox_restarted"] is False

    # Agent execution is gone; the demo process group is not.
    assert tool not in {p.pid for p in h.runtime.procs[run_id]}
    assert _roles(h, run_id) == ["demo"]
    assert view["demo"]["alive"] is True and view["demo"]["listening"] is True
    assert ev["demo"]["alive"] is True
    # A restarted sandbox would not start the Agent Server again.
    assert (h.controller.paths(run_id).control_dir / "mode").read_text() == "demo-only\n"


def test_stop_persists_intent_before_touching_anything(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.conversation.fail_pause = RuntimeError("controller crashed mid-stop")
    with pytest.raises(RuntimeError):
        h.controller.handle("stop", {"run_id": run_id})
    run = h.state.get_run(run_id)
    assert run is not None
    assert (run.phase, run.outcome, run.stop_requested) == ("stopping", "stopped", True)

    # The reconcile loop finishes the job from the durable intent.
    h.conversation.fail_pause = None
    h.controller.reconcile_once()
    run = h.state.get_run(run_id)
    assert run is not None and run.phase == "stopped"


def test_a_pause_that_does_not_take_is_waited_out_then_killed(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.conversation.pause_to = None  # stuck mid-step: stays "running"
    started = h.clock.now()
    view = h.controller.handle("stop", {"run_id": run_id})
    ev = view["stop_evidence"]
    assert ev["quiescent"] is False and ev["conversation_status"] == "running"
    # Bounded: the wait ended at the grace period, then the kill did the job.
    waited = (h.clock.now() - started).total_seconds()
    assert h.settings.stop_grace_s <= waited <= 3 * h.settings.stop_grace_s
    assert ev["failed"] is False and view["phase"] == "stopped"
    assert _roles(h, run_id) == []


def test_an_unreachable_agent_server_does_not_prevent_the_stop(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.conversation.fail_pause = ConversationError("connection refused")
    view = h.controller.handle("stop", {"run_id": run_id})
    ev = view["stop_evidence"]
    assert any("interrupt" in n for n in ev["notes"]) and any("pause" in n for n in ev["notes"])
    assert ev["failed"] is False and view["phase"] == "stopped"


def test_survivors_force_a_sandbox_restart_and_the_demo_is_relaunched(harness: Harness) -> None:
    h = harness
    run_id, demo_sid = _with_demo(h)
    stubborn = h.runtime.agent_tool(run_id, "unkillable")
    h.runtime.unkillable.add(stubborn)
    h.trace.clear()

    view = h.controller.handle("stop", {"run_id": run_id})
    ev = view["stop_evidence"]
    assert "restart" in h.trace
    assert ev["sandbox_restarted"] is True
    assert ev["survivors"] == [], "after the restart nothing of the agent is left"
    assert ev["failed"] is False and view["phase"] == "stopped"
    # The sandbox came back demo-only: no Agent Server, and the recorded demo spec,
    # and only that, was started again.
    assert _roles(h, run_id) == ["demo"]
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.session_id != demo_sid
    assert demo.command == "python3 -m http.server 3000"
    assert [t for t in h.trace if t.startswith("demo-start")] == [
        "demo-start 'python3 -m http.server 3000' replace=None"
    ]
    assert ev["demo"]["relaunched"] is True


def test_cessation_that_cannot_be_shown_is_reported_not_assumed(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.runtime.fail_sandbox = "OCI runtime exec failed"
    view = h.controller.handle("stop", {"run_id": run_id})
    ev = view["stop_evidence"]
    assert ev["failed"] is True
    assert ev["survivors"] is None
    assert any("OCI runtime exec failed" in n for n in ev["notes"])
    # Not stopped: the run stays `stopping` and the reconcile loop keeps trying.
    assert view["phase"] == "stopping"
    h.runtime.fail_sandbox = None
    h.controller.reconcile_once()
    run = h.state.get_run(run_id)
    assert run is not None and run.phase == "stopped"


def test_a_crashed_sandbox_comes_back_demo_only_with_the_recorded_demo(harness: Harness) -> None:
    h = harness
    run_id, _ = _with_demo(h)
    h.runtime.exit(agent_container_name(run_id), code=137)
    h.trace.clear()
    view = h.controller.handle("stop", {"run_id": run_id})
    ev = view["stop_evidence"]
    assert ev["container_running"] is False and ev["failed"] is False
    assert h.conversation.paused == []  # nothing to pause
    assert _roles(h, run_id) == ["demo"]
    assert ev["demo"]["relaunched"] is True


def test_stop_during_launch_before_any_container(harness: Harness) -> None:
    h = harness
    run_id = str(h.controller.handle("launch", {"brief_text": "x", "budget_hours": 1})["run_id"])
    view = h.controller.handle("stop", {"run_id": run_id})
    assert view["phase"] == "stopped" and view["stop_evidence"]["failed"] is False
    h.inference_ready()
    h.controller.reconcile_once()
    assert h.runtime.runs == []


def test_stopping_an_ended_run_is_a_no_op(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.conversation.status = "finished"
    h.controller.reconcile_once()
    view = h.controller.handle("stop", {"run_id": run_id})
    assert view["phase"] == "finished" and "already finished" in view["message"]
    assert h.conversation.paused == []


def test_stop_agent_needs_a_persisted_stop(harness: Harness) -> None:
    run_id = harness.running_run()
    with pytest.raises(RequestError, match="persist the stop"):
        harness.controller.stop_agent(run_id)


def test_first_stop_reason_wins(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.controller.handle("stop", {"run_id": run_id})
    h.clock.advance(hours=5)
    assert h.controller.deadline_watchdog().check_once() == []
    run = h.state.get_run(run_id)
    assert run is not None and run.outcome == "stopped"


# --- the demo request protocol ------------------------------------------------------


def test_demo_request_is_recorded_before_it_is_started(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    rid = _request_demo(h, run_id, "npx serve -l 3000 dist")
    h.controller.reconcile_once()
    demo = h.state.get_demo(run_id)
    assert demo is not None
    assert (demo.request_id, demo.command, demo.state) == (
        rid,
        "npx serve -l 3000 dist",
        "starting",
    )
    assert _demo_answer(h, run_id)["state"] == "starting"
    # The same request is not started twice.
    h.controller.reconcile_once()
    assert len([t for t in h.trace if t.startswith("demo-start")]) == 1


def test_a_new_demo_request_replaces_the_running_demo(harness: Harness) -> None:
    h = harness
    run_id, first = _with_demo(h)
    _request_demo(h, run_id, "pnpm start")
    h.controller.reconcile_once()
    assert f"demo-start 'pnpm start' replace={first}" in h.trace


@pytest.mark.parametrize(
    ("command", "port", "why"),
    [
        ("", 3000, "non-empty"),
        ("x" * 5000, 3000, "longer than"),
        ("serve", 8080, "port 3000"),
        ("serve", "3000", "port 3000"),
        (["serve"], 3000, "non-empty string"),
    ],
)
def test_bad_demo_requests_are_refused(
    harness: Harness, command: object, port: object, why: str
) -> None:
    h = harness
    run_id = h.running_run()
    rid = _request_demo(h, run_id, command=command, port=port)  # type: ignore[arg-type]
    h.controller.reconcile_once()
    answer = _demo_answer(h, run_id)
    assert (answer["request_id"], answer["state"]) == (rid, "refused")
    assert why in answer["message"]
    assert not any(t.startswith("demo-start") for t in h.trace)


def test_a_demo_that_exits_is_reported_failed_with_its_output(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    _request_demo(h, run_id)
    h.controller.reconcile_once()
    demo = h.state.get_demo(run_id)
    assert demo is not None
    h.runtime.procs[run_id] = [p for p in h.runtime.procs[run_id] if p.sid != demo.session_id]
    h.controller.reconcile_once()
    answer = _demo_answer(h, run_id)
    assert answer["state"] == "failed" and "exited" in answer["message"]
    assert "boom" in answer["message"]


def test_a_demo_that_never_listens_times_out(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    _request_demo(h, run_id)
    h.controller.reconcile_once()
    h.clock.advance(seconds=h.settings.demo_start_timeout_s + 1)
    h.controller.reconcile_once()
    answer = _demo_answer(h, run_id)
    assert answer["state"] == "failed" and "not listening" in answer["message"]


def test_symlinked_demo_requests_are_not_followed(harness: Harness, tmp_path: Path) -> None:
    """The agent owns its workspace; the controller must not read through its links."""
    h = harness
    run_id = h.running_run()
    secret = tmp_path / "controller-secret.json"
    secret.write_text(json.dumps({"id": "stolen", "command": "cat /etc/shadow", "port": 3000}))
    dgx = h.controller.paths(run_id).agent_dir / ".dgx"
    dgx.mkdir()
    os.symlink(secret, dgx / "demo-request.json")
    h.controller.reconcile_once()
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.request_id is None
    assert not any(t.startswith("demo-start") for t in h.trace)

    (dgx / "demo-request.json").unlink()
    dgx.rmdir()
    os.symlink(tmp_path, dgx)  # a symlinked directory is refused too
    secret.rename(tmp_path / "demo-request.json")
    h.controller.reconcile_once()
    assert not any(t.startswith("demo-start") for t in h.trace)


def test_status_shows_the_demo_and_tunnel_port(harness: Harness) -> None:
    h = harness
    run_id, _ = _with_demo(h)
    view = h.controller.handle("status", {"run_id": run_id})
    demo = view["demo"]
    assert demo["host_port"] == h.settings.demo_host_port_base
    assert demo["url"] == f"http://127.0.0.1:{h.settings.demo_host_port_base}/"
    assert demo["state"] == "running" and demo["listening"] is True
    spec = h.runtime.specs[agent_container_name(run_id)]
    assert [(p.host_ip, p.host_port, p.container_port) for p in spec.ports] == [
        ("127.0.0.1", h.settings.demo_host_port_base, 3000)
    ]


def test_each_run_gets_its_own_demo_host_port(harness: Harness) -> None:
    h = harness
    first = h.running_run()
    h.clock.advance(minutes=1)
    second = h.running_run()
    ports = [h.state.get_demo(r).host_port for r in (first, second)]  # type: ignore[union-attr]
    assert ports == [h.settings.demo_host_port_base, h.settings.demo_host_port_base + 1]
