"""launch --from-run: a new run continues an ended run's project, with the operator's report."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dgx_autonomy import frozen
from dgx_autonomy.controller import RequestError

from fakes import Harness

BRIEF = "# Brief\n\nServe a page that says hello on port 3000.\n"
REPORT = "The page works. Make the greeting warmer and the text larger."


def _launch(h: Harness, **extra: Any) -> str:
    result = h.controller.handle(
        "launch",
        {
            "brief_text": BRIEF,
            "checks": {},
            "bundle_digest": frozen.bundle_digest(BRIEF.encode(), frozen.encode({})),
            "budget_hours": 2,
            **extra,
        },
    )
    return str(result["run_id"])


def _ended_run(h: Harness) -> str:
    run_id = h.running_run(brief_text=BRIEF, budget_hours=2)
    project = h.controller.paths(run_id).project_dir
    (project / "index.html").write_text("<h1>hello</h1>\n")
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / "test-results").mkdir()
    (project / "test-results" / "trace.zip").write_text("old")
    (project / "outside").symlink_to("/etc/passwd")
    h.controller.handle("stop", {"run_id": run_id})
    for _ in range(3):
        h.controller.reconcile_once()
    run = h.state.get_run(run_id)
    assert run is not None and run.terminal
    return run_id


def test_a_run_continues_the_project_of_an_ended_one(harness: Harness) -> None:
    h = harness
    old = _ended_run(h)
    h.clock.advance(seconds=5)
    new = _launch(h, from_run=old, report_text=REPORT, review_hold=True)

    project = h.controller.paths(new).project_dir
    assert (project / "index.html").read_text() == "<h1>hello</h1>\n"
    assert (project / ".git" / "HEAD").exists()  # the history comes along
    assert not (project / "test-results").exists()
    assert (project / "outside").is_symlink()  # copied as a link, never followed
    seed = json.loads(h.controller.paths(new).seed.read_text())
    assert (seed["from_run"], seed["report"]) == (old, REPORT)
    run = h.state.get_run(new)
    assert run is not None and run.review_hold

    # The builder is told it continues, and gets the report before the brief.
    h.inference_ready()
    h.agent_healthy(new)
    h.controller.reconcile_once()
    message = h.conversation.started[-1].message
    assert "continues earlier work" in message and f"run {old} left it" in message
    assert message.index("--- OPERATOR REPORT ---") < message.index("--- BRIEF ---")
    assert REPORT in message

    # The report is the run's first feedback: rollovers carry it, the next one is #2.
    result = h.controller.handle("feedback", {"run_id": new, "text": "Bigger still."})
    assert result["feedback_n"] == 2


def test_only_an_ended_run_with_a_report_can_be_continued(harness: Harness) -> None:
    h = harness
    running = h.running_run(brief_text=BRIEF, budget_hours=2)
    with pytest.raises(RequestError, match="stop it before continuing it"):
        _launch(h, from_run=running, report_text=REPORT)
    with pytest.raises(RequestError, match="no run nope"):
        _launch(h, from_run="nope", report_text=REPORT)
    h.controller.handle("stop", {"run_id": running})
    for _ in range(3):
        h.controller.reconcile_once()
    with pytest.raises(RequestError, match="needs the operator's report"):
        _launch(h, from_run=running, report_text=" ")
