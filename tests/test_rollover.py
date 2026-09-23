"""Rollover: a stuck, erroring, full or failing conversation is replaced by a fresh one
that continues from a checkpoint, in the same run, before the same deadline."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy.controller import RequestError
from dgx_autonomy.openhands_adapter import BUILDER_TOOLS, ConversationError, conversation_id_for
from dgx_autonomy.ports import EventSummary

from fakes import Harness

BRIEF = "Create step1.txt to step6.txt in the project, one per step.\n"


CHECKS = {
    "criteria.yaml": "criteria:\n  - key: home\n    description: GET / works\n"
    "    test: test_home.py\n",
    "test_home.py": "def test_home() -> None:\n    assert False\n",
}


def _run(h: Harness, **launch: Any) -> str:
    run_id = h.running_run(brief_text=BRIEF, budget_hours=8, **launch)
    project = h.controller.paths(run_id).project_dir
    for i in (1, 2, 3):
        (project / f"step{i}.txt").write_text(f"step {i}\n")
    return run_id


def _agent_dir(h: Harness, run_id: str) -> Path:
    return h.controller.paths(run_id).agent_dir


def _request_id(h: Harness, run_id: str) -> str:
    row = h.state.open_rollover(run_id)
    assert row is not None and row.handoff_request_id is not None, row
    return row.handoff_request_id


def _write(h: Harness, run_id: str, name: str, body: dict[str, Any]) -> None:
    d = _agent_dir(h, run_id) / ".dgx"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(body))


def _handoff(request_id: str, file_id: str = "h1", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": file_id,
        "request_id": request_id,
        "summary": "Steps 1-3 are written.",
        "roadmap": [
            {"item": f"step {i}", "status": "done", "evidence": [f"step{i}.txt"]} for i in (1, 2, 3)
        ]
        + [{"item": f"step {i}", "status": "todo"} for i in (4, 5, 6)],
        "attempts": [{"approach": "echo into files", "outcome": "worked"}],
        "next_steps": ["write step4.txt"],
    }
    body.update(over)
    return body


def _answer(h: Harness, run_id: str, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((h.controller.paths(run_id).control_dir / name).read_text())
    return data


def test_a_stuck_conversation_hands_over_to_a_fresh_one(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    before = h.state.get_run(run_id)
    assert before is not None
    first = before.conversation_id
    assert [(c.n, c.status) for c in h.state.conversations(run_id)] == [(1, "active")]

    h.conversation.event_log.append(EventSummary("e9", "t", "ActionEvent", "agent", "ls"))
    h.conversation.status = "stuck"
    h.controller.reconcile_once()  # the rollover is recorded
    h.controller.reconcile_once()  # the old conversation is asked for its handoff
    request_id = _request_id(h, run_id)
    [(to, ask)] = h.conversation.sent_to
    assert to == first and ask.run is True
    assert f"HANDOFF REQUEST {request_id}" in ask.text and "write_handoff" in ask.text

    # Nothing yet: the controller waits.
    h.controller.reconcile_once()
    assert h.state.checkpoints(run_id) == []

    _write(h, run_id, "handoff.json", _handoff(request_id))
    h.controller.reconcile_once()
    answer = _answer(h, run_id, "handoff.json")
    assert (answer["request_id"], answer["accepted"], answer["problems"]) == ("h1", True, [])

    [ckpt] = h.state.checkpoints(run_id)
    assert (ckpt.source, ckpt.reason, ckpt.conversation_id) == ("agent_handoff", "stuck", first)
    assert [r["status"] for r in ckpt.handoff["roadmap"]] == ["done"] * 3 + ["todo"] * 3
    assert ckpt.verified == {"latest_evaluation": None}
    assert ckpt.workspace_sha is not None
    assert first in h.conversation.interrupted and first in h.conversation.paused

    # The fresh conversation: same run, same deadline, a derived id, the recovery context.
    run = h.state.get_run(run_id)
    assert run is not None
    second = conversation_id_for(f"{run_id}#2")
    assert run.conversation_id == second != first
    assert (run.deadline_at, run.phase) == (before.deadline_at, "running")
    assert run.current_checkpoint_id == ckpt.id
    request = h.conversation.started[-1]
    assert request.conversation_id == second and request.builder_tools
    assert "fresh conversation (#2)" in request.message
    assert BRIEF.strip() in request.message
    assert "- [done] step 1  [evidence: step1.txt]" in request.message
    assert "Diagnose first" in request.message
    assert [(c.n, c.status) for c in h.state.conversations(run_id)] == [(1, "ended"), (2, "active")]

    # The run goes on with the new conversation; the old one is left alone.
    h.controller.reconcile_once()
    assert h.state.get_run(run_id).phase == "running"  # type: ignore[union-attr]
    assert len(h.conversation.started) == 2
    status = h.controller.handle("status", {"run_id": run_id})
    assert [c["n"] for c in status["conversations"]] == [1, 2]
    assert status["current_checkpoint"] == ckpt.id
    chain = h.controller.handle("checkpoints", {"run_id": run_id})
    assert chain["checkpoints"][0]["handoff"]["next_steps"] == ["write step4.txt"]


def test_an_invalid_handoff_is_answered_with_its_problems(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    h.controller.handle("rollover", {"run_id": run_id, "detail": "test"})
    h.controller.reconcile_once()
    request_id = _request_id(h, run_id)
    bad = _handoff(request_id, "h1", verified={"all": "passed"})
    bad["roadmap"][0]["evidence"] = ["nope.txt"]
    _write(h, run_id, "handoff.json", bad)
    h.controller.reconcile_once()
    answer = _answer(h, run_id, "handoff.json")
    assert answer["accepted"] is False
    assert any("records verified results itself" in p for p in answer["problems"])
    assert any("'nope.txt' does not exist" in p for p in answer["problems"])
    assert h.state.checkpoints(run_id) == []

    _write(h, run_id, "handoff.json", _handoff(request_id, "h2"))
    h.controller.reconcile_once()
    assert _answer(h, run_id, "handoff.json")["accepted"] is True
    assert [c.source for c in h.state.checkpoints(run_id)] == ["agent_handoff"]


def test_without_a_handoff_the_controller_records_a_fallback(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    # A first rollover with a good handoff...
    h.controller.handle("rollover", {"run_id": run_id})
    h.controller.reconcile_once()
    _write(h, run_id, "handoff.json", _handoff(_request_id(h, run_id)))
    h.controller.reconcile_once()
    [first] = h.state.checkpoints(run_id)

    # ...and a second one where the conversation never answers.
    h.conversation.event_log.append(
        EventSummary("e5", "t", "ActionEvent", "agent", 'terminal {"command": "ls"}')
    )
    h.controller.handle("rollover", {"run_id": run_id})
    h.controller.reconcile_once()
    h.clock.advance(seconds=h.settings.handoff_timeout_s - 1)
    h.controller.reconcile_once()
    assert len(h.state.checkpoints(run_id)) == 1
    h.clock.advance(seconds=2)
    h.controller.reconcile_once()
    second = h.state.checkpoints(run_id)[-1]
    assert (second.source, second.supersedes_id) == ("controller_fallback", first.id)
    assert "no valid handoff within 10 min" in second.problems[0]
    # The last valid handoff is carried forward, marked as older; nothing new is done.
    assert second.handoff["previous_handoff"]["roadmap"] == first.handoff["roadmap"]
    assert "roadmap" not in second.handoff
    message = h.conversation.started[-1].message
    assert "the previous conversation wrote no usable handoff" in message
    assert "older; the work may have moved on" in message
    run = h.state.get_run(run_id)
    assert run is not None and run.conversation_id == conversation_id_for(f"{run_id}#3")


def test_an_erroring_conversation_is_nudged_then_replaced(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    h.conversation.event_log.append(
        EventSummary("e1", "t", "AgentErrorEvent", "agent", "tool call parse error")
    )
    for i in range(h.settings.error_nudges):
        h.conversation.status = "error"
        h.controller.reconcile_once()
        assert len(h.conversation.delivered) == i + 1
        assert "tool call parse error" in h.conversation.delivered[-1].text
        h.controller.reconcile_once()  # too soon for another nudge
        assert len(h.conversation.delivered) == i + 1
        h.clock.advance(seconds=h.settings.error_backoff_s * 2**i)
    h.conversation.status = "error"
    h.controller.reconcile_once()
    row = h.state.open_rollover(run_id)
    assert row is not None and row.reason == "errors"
    assert h.state.get_run(run_id).phase == "running"  # type: ignore[union-attr]


def test_a_full_context_rolls_over_but_not_again_right_away(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    ctx = 65536
    h.conversation.context_tokens = int(ctx * 0.5)
    h.controller.reconcile_once()
    assert h.state.open_rollover(run_id) is None
    h.conversation.context_tokens = int(ctx * 0.9)
    h.controller.reconcile_once()
    row = h.state.open_rollover(run_id)
    assert row is not None and row.reason == "context" and "58982 of 65536" in str(row.detail)
    h.controller.reconcile_once()
    _write(h, run_id, "handoff.json", _handoff(_request_id(h, run_id)))
    h.controller.reconcile_once()
    assert h.state.open_rollover(run_id) is None
    # The fresh conversation reports a full context too early: held back for a while.
    h.controller.reconcile_once()
    assert h.state.open_rollover(run_id) is None
    h.clock.advance(seconds=h.settings.rollover_min_interval_s + 1)
    h.controller.reconcile_once()
    assert h.state.open_rollover(run_id) is not None


def test_repeated_failed_claims_go_to_a_fresh_conversation(harness: Harness) -> None:
    h = harness
    run_id = _run(h, checks=CHECKS)
    run = h.state.get_run(run_id)
    assert run is not None
    for i in range(h.settings.failures_before_rollover):
        ev = h.state.begin_evaluation(
            run_id, trigger="claim", check_digest="d", evidence_root="/e",
            now=h.clock.now(), claim_event_id=f"c{i}", claim_text="done",
        )  # fmt: skip
        ev = h.state.finish_evaluation(
            ev.id, status="failed", detail="failed: home",
            results={"home": {"status": "failed", "summary": "404", "excerpt": "GET / 404"}},
            now=h.clock.now(),
        )  # fmt: skip
        h.controller._deliver_failure(run, ev)
        h.clock.advance(minutes=1)
    # Two failures went back to the conversation; the third starts a rollover instead.
    assert len(h.conversation.delivered) == h.settings.failures_before_rollover - 1
    row = h.state.open_rollover(run_id)
    assert row is not None and row.reason == "failures"
    assert "3 completion claims in a row failed the checks (home)" in str(row.detail)
    h.controller.reconcile_once()
    _write(h, run_id, "handoff.json", _handoff(_request_id(h, run_id)))
    h.controller.reconcile_once()
    message = h.conversation.started[-1].message
    assert "--- RECENT FAILURES ---" in message and "GET / 404" in message
    assert "Evaluation #3 (failed)" in message
    # The failure the old conversation never got is in the new context: delivered.
    assert all(e.delivered_at for e in h.state.evaluations(run_id))


def test_a_blocker_is_reviewed_by_a_fresh_conversation_before_the_run_ends(
    harness: Harness,
) -> None:
    h = harness
    run_id = _run(h)
    _write(h, run_id, "blocked.json", {"id": "b1", "missing_capability": "hard",
                                        "alternatives_tried": ["x"], "needed": "y"})  # fmt: skip
    h.controller.reconcile_once()
    answer = _answer(h, run_id, "blocked.json")
    assert answer["accepted"] is False and answer["request_id"] == "b1"
    assert h.state.open_rollover(run_id) is None

    blocker = {
        "id": "b2",
        "missing_capability": "The weather API now needs a paid key for every request",
        "alternatives_tried": ["open-meteo: requires key since 2026", "wttr.in: blocked"],
        "needed": "an API key",
    }
    _write(h, run_id, "blocked.json", blocker)
    h.controller.reconcile_once()
    assert _answer(h, run_id, "blocked.json")["accepted"] is True
    row = h.state.open_rollover(run_id)
    assert row is not None and row.reason == "blocked-review"
    h.controller.reconcile_once()
    _write(h, run_id, "handoff.json", _handoff(_request_id(h, run_id)))
    h.controller.reconcile_once()
    message = h.conversation.started[-1].message
    assert "--- BLOCKER TO REVIEW ---" in message
    assert "Missing: The weather API now needs a paid key" in message
    assert h.state.get_run(run_id).phase == "running"  # type: ignore[union-attr]

    # The fresh conversation confirms it: the run ends, blocked, with the evidence.
    _write(h, run_id, "blocked.json", {**blocker, "id": "b3"})
    h.controller.reconcile_once()
    assert "confirmed" in _answer(h, run_id, "blocked.json")["message"]
    run = h.state.get_run(run_id)
    assert run is not None and (run.phase, run.outcome) == ("stopped", "blocked")
    report = h.controller.handle("report", {"run_id": run_id})
    assert report["outcome"] == "blocked"
    assert report["blocked"]["blocker"]["needed"] == "an API key"
    assert report["blocked"]["confirmed_by_conversation"] == 2
    assert report["blocked"]["first_declaration"]["missing_capability"].startswith("The weather")


def test_a_controller_restart_mid_rollover_starts_one_conversation(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    h.controller.handle("rollover", {"run_id": run_id})
    h.controller.reconcile_once()
    _write(h, run_id, "handoff.json", _handoff(_request_id(h, run_id)))
    h.conversation.fail_start = ConversationError("Agent Server busy")
    h.controller.reconcile_once()  # checkpoint recorded; the start fails
    row = h.state.open_rollover(run_id)
    assert row is not None and row.status == "starting"
    assert len(h.state.checkpoints(run_id)) == 1

    h.restart_controller()
    summary = h.controller.reconcile_on_start()
    assert row.id in summary["retried"]
    h.conversation.fail_start = None
    h.controller.reconcile_once()
    h.controller.reconcile_once()
    second = conversation_id_for(f"{run_id}#2")
    assert [r.conversation_id for r in h.conversation.started].count(second) == 1
    assert len(h.state.checkpoints(run_id)) == 1  # not recorded twice
    run = h.state.get_run(run_id)
    assert run is not None and run.conversation_id == second


def test_stop_during_a_rollover_abandons_it(harness: Harness) -> None:
    h = harness
    run_id = _run(h)
    h.controller.handle("rollover", {"run_id": run_id})
    h.controller.reconcile_once()
    h.controller.handle("stop", {"run_id": run_id})
    assert [c.status for c in h.state.conversations(run_id)] == ["active", "abandoned"]
    with pytest.raises(RequestError, match="only a running run rolls over"):
        h.controller.handle("rollover", {"run_id": run_id})


def test_the_builder_gets_the_handoff_and_blocker_tools(harness: Harness) -> None:
    h = harness
    _run(h)
    assert h.conversation.started[0].builder_tools
    assert BUILDER_TOOLS == ("start_demo", "write_handoff", "declare_blocked")


def test_recovery_during_a_rollover_does_not_resume_the_old_conversation(
    harness: Harness,
) -> None:
    h = harness
    run_id = _run(h)
    h.controller.handle("rollover", {"run_id": run_id})
    h.controller.reconcile_once()  # handoff requested
    h.runtime.reboot()
    h.conversation.status = "error"
    h.controller.reconcile_once()  # llama-server
    h.controller.reconcile_once()  # sandbox, then the rollover goes on
    [rec] = h.state.recoveries(run_id)
    assert rec.status == "done" and rec.steps.get("conversation_status") == "rolling over"
    assert not any("restarted" in m.text for m in h.conversation.delivered)
    assert h.state.open_rollover(run_id) is not None
    assert timedelta(0) <= h.clock.now() - h.state.open_rollover(run_id).started_at  # type: ignore[union-attr]
