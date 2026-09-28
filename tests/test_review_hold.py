"""The operator's review: a held run waits after its checks pass; feedback sends it back.

Without a hold, a claim that passes every required check finishes the run (see
test_evaluation.py). With one, the operator accepts the work or sends the builder
product direction, and the builder's next claim is evaluated like any other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dgx_autonomy import frozen
from dgx_autonomy.controller import RequestError

from fakes import Harness, pytest_passes

BRIEF = "# Brief\n\nServe a page that says hello, and /version.txt, on port 3000.\n"
CHECKS = {
    "criteria.yaml": """\
criteria:
  - key: version
    description: GET /version.txt returns 2
    test: test_version.py
""",
    "test_version.py": "def test_version():\n    assert True\n",
}
FEEDBACK = "The forecast reads like a spreadsheet. Lead with what to wear today."


def _held_run(h: Harness) -> str:
    result = h.controller.handle(
        "launch",
        {
            "brief_text": BRIEF,
            "checks": CHECKS,
            "bundle_digest": frozen.bundle_digest(BRIEF.encode(), frozen.encode(CHECKS)),
            "budget_hours": 8,
        },
    )
    run_id = str(result["run_id"])
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    h.controller.handle("review.hold", {"run_id": run_id})
    (h.controller.paths(run_id).project_dir / "version.txt").write_text("2\n")
    d = h.controller.paths(run_id).agent_dir / ".dgx"
    d.mkdir(exist_ok=True)
    body = {"id": "req1", "command": "python3 -m http.server 3000", "port": 3000}
    (d / "demo-request.json").write_text(json.dumps(body))
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    h.runtime.evaluator_outcomes["version"] = pytest_passes()
    _ticks(h)
    return run_id


def _ticks(h: Harness, n: int = 8) -> None:
    for _ in range(n):
        h.controller.reconcile_once()


def _phase(h: Harness, run_id: str) -> str:
    run = h.state.get_run(run_id)
    assert run is not None
    return run.phase


def _kinds(h: Harness) -> list[str]:
    return [e["kind"] for e in h.controller.handle("notifications", {"since": 0})["events"]]


def test_a_held_run_waits_for_the_operator_after_its_checks_pass(harness: Harness) -> None:
    h = harness
    run_id = _held_run(h)
    h.conversation.claim("Done.")
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "passed" and ev.delivered_at is not None
    assert _phase(h, run_id) == "running"
    assert _kinds(h).count("run.review") == 1
    assert h.conversation.delivered == []  # the builder is not told anything

    # More ticks change nothing: the notice is sent once, the run keeps waiting.
    _ticks(h, 4)
    assert _kinds(h).count("run.review") == 1 and _phase(h, run_id) == "running"

    status = h.controller.handle("review.accept", {"run_id": run_id})
    assert status["phase"] == "finished" and status["review_hold"] is False
    assert "run.ended" in _kinds(h)


def test_feedback_sends_the_builder_back_and_its_next_claim_is_evaluated(
    harness: Harness,
) -> None:
    h = harness
    run_id = _held_run(h)
    h.conversation.claim("Done.")
    _ticks(h)
    with pytest.raises(RequestError, match="non-empty"):
        h.controller.handle("feedback", {"run_id": run_id, "text": "  "})

    result = h.controller.handle("feedback", {"run_id": run_id, "text": FEEDBACK})
    assert result["feedback_n"] == 1
    [message] = h.conversation.delivered
    assert message.run is True
    assert message.text.startswith("OPERATOR FEEDBACK #1") and FEEDBACK in message.text
    assert "still decide completion" in message.text
    assert h.conversation.status == "running"
    assert "feedback" in _kinds(h)

    # Accept is refused while the builder works on the feedback.
    _ticks(h, 2)
    with pytest.raises(RequestError, match="not waiting for review"):
        h.controller.handle("review.accept", {"run_id": run_id})

    # The builder claims again: a new evaluation, held again.
    h.conversation.claim("Leads with what to wear.")
    _ticks(h)
    ev1, ev2 = h.state.evaluations(run_id)
    assert ev2.status == "passed" and ev2.claim_event_id != ev1.claim_event_id
    assert _kinds(h).count("run.review") == 2
    assert _phase(h, run_id) == "running"

    # Releasing the hold lets the verified claim finish the run.
    h.controller.handle("review.hold", {"run_id": run_id, "hold": False})
    _ticks(h, 2)
    assert _phase(h, run_id) == "finished"


def test_feedback_is_kept_for_a_fresh_conversation(harness: Harness) -> None:
    h = harness
    run_id = _held_run(h)
    h.controller.handle("feedback", {"run_id": run_id, "text": FEEDBACK})
    [line] = h.controller.paths(run_id).feedback.read_text().splitlines()
    assert json.loads(line)["text"] == FEEDBACK

    h.controller.handle("rollover", {"run_id": run_id, "detail": "test"})
    h.controller.reconcile_once()
    row = h.state.open_rollover(run_id)
    assert row is not None and row.handoff_request_id is not None
    handoff = {
        "id": "h1",
        "request_id": row.handoff_request_id,
        "summary": "version.txt is served.",
        "roadmap": [{"item": "version", "status": "done", "evidence": ["version.txt"]}],
        "attempts": [],
        "next_steps": ["lead with what to wear"],
    }
    (h.controller.paths(run_id).agent_dir / ".dgx" / "handoff.json").write_text(json.dumps(handoff))
    h.controller.reconcile_once()
    first_message = h.conversation.started[-1].message
    assert "fresh conversation (#2)" in first_message
    assert "OPERATOR FEEDBACK (oldest first)" in first_message and FEEDBACK in first_message


def test_feedback_and_holds_need_a_running_run(harness: Harness) -> None:
    h = harness
    run_id = _held_run(h)
    h.controller.handle("stop", {"run_id": run_id})
    _ticks(h, 3)
    with pytest.raises(RequestError, match="feedback goes to a running run"):
        h.controller.handle("feedback", {"run_id": run_id, "text": FEEDBACK})
    assert _phase(h, run_id) == "stopped"
    with pytest.raises(RequestError, match="nothing left to hold"):
        h.controller.handle("review.hold", {"run_id": run_id})
    assert not Path(h.controller.paths(run_id).feedback).exists()
