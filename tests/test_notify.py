"""The operator's feed: milestones arrive as they happen, so nobody polls `status`."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from dgx_autonomy.handoff_tool import ReportProgressAction, ReportProgressExecutor
from dgx_autonomy.notify import Notifier, format_line
from dgx_autonomy.ports import EventSummary
from test_evaluation import _phase, _project, _running_with_demo, _ticks
from test_planning import _open_plan

from fakes import Harness, pytest_fails, pytest_passes

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _kinds(h: Harness, since: int = 0) -> list[str]:
    return [e["kind"] for e in h.controller.handle("notifications", {"since": since})["events"]]


def test_the_feed_is_numbered_durable_and_waits_for_the_next_line(tmp_path: Path) -> None:
    feed = Notifier(tmp_path / "state" / "notifications.jsonl")
    feed.emit("run.launched", "launched", now=NOW, run_id="r1")
    feed.emit("plan.waiting", "x" * 5000, now=NOW, plan_id="p1")
    page = feed.read(0)
    assert [e["n"] for e in page["events"]] == [1, 2] and page["next"] == 2
    assert len(page["events"][1]["text"]) == 1500
    assert "[r1] launched" in format_line(page["events"][0])
    # A new reader (a restarted controller) continues the numbering.
    feed = Notifier(tmp_path / "state" / "notifications.jsonl")
    assert feed.last == 2 and feed.read(-1) == {"events": [], "next": 2, "last": 2}

    # A waiting reader gets the next line as soon as it is written.
    threading.Timer(0.2, lambda: feed.emit("demo", "up", now=NOW, run_id="r1")).start()
    started = time.monotonic()
    page = feed.read(2, wait_s=5)
    assert [e["kind"] for e in page["events"]] == ["demo"]
    assert time.monotonic() - started < 3


def test_a_run_reports_launch_claims_evaluations_progress_and_its_end(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    assert _kinds(h)[:3] == ["run.launched", "run.running", "demo"]

    # The builder's progress reports are forwarded once, labelled as its own account.
    progress = h.controller.paths(run_id).agent_dir / ".dgx" / "progress.jsonl"
    executor = ReportProgressExecutor(str(progress))
    executor(ReportProgressAction(item="Home page", status="done", summary="It says hello."))
    _ticks(h, 2)
    executor(ReportProgressAction(item="Version file", status="started", summary="Next."))
    _ticks(h, 2)
    feed = h.controller.handle("notifications", {"since": 0})["events"]
    reports = [e["text"] for e in feed if e["kind"] == "progress"]
    assert reports == [
        "(builder's report) done: Home page - It says hello.",
        "(builder's report) started: Version file - Next.",
    ]
    assert all(e["run_id"] == run_id for e in feed)

    h.runtime.evaluator_outcomes["version"] = pytest_fails("GET /version.txt returned 404")
    h.conversation.claim("Done: the page says hello.")
    _ticks(h)
    (_project(h, run_id) / "version.txt").write_text("2\n")
    h.runtime.evaluator_outcomes["version"] = pytest_passes()
    h.conversation.claim("Added version.txt.")
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    _ticks(h)
    assert _phase(h, run_id) == "finished"

    feed = h.controller.handle("notifications", {"since": 0})["events"]
    texts = {e["kind"]: [x["text"] for x in feed if x["kind"] == e["kind"]] for e in feed}
    assert texts["claim"][0].startswith("the builder claims completion (its words")
    assert texts["evaluation"][0] == "evaluation #1 (claim) failed: 1/2 passed; not passed: version"
    assert texts["evaluation"][1] == "evaluation #2 (claim) passed: 2/2 passed"
    assert texts["run.ended"] == ["VERIFIED: evaluation #2 passed every required check; finished"]


def test_the_progress_log_is_not_forwarded_again_after_a_controller_restart(
    harness: Harness,
) -> None:
    h = harness
    run_id = _running_with_demo(h)
    progress = h.controller.paths(run_id).agent_dir / ".dgx" / "progress.jsonl"
    ReportProgressExecutor(str(progress))(
        ReportProgressAction(item="A", status="done", summary="ok")
    )
    _ticks(h, 2)
    h.controller._progress_seen.clear()  # what a restarted controller remembers
    _ticks(h, 2)
    assert _kinds(h).count("progress") == 1
    # A symlink the agent planted is not followed.
    progress.unlink()
    progress.symlink_to(h.settings.state_db)
    _ticks(h, 2)
    assert _kinds(h).count("progress") == 1


def test_the_planner_waiting_for_the_operator_is_announced_with_what_it_said(
    harness: Harness,
) -> None:
    h = harness
    plan_id = _open_plan(h)
    h.controller.reconcile_once()
    h.conversation.status = "running"
    h.controller.reconcile_once()
    h.conversation.event_log.append(
        EventSummary("e1", "t", "MessageEvent", "agent", "Which currency should I show?")
    )
    h.conversation.status = "finished"
    h.controller.reconcile_once()
    h.controller.reconcile_once()
    waiting = [
        e
        for e in h.controller.handle("notifications", {"since": 0})["events"]
        if e["kind"] == "plan.waiting"
    ]
    assert len(waiting) == 1 and waiting[0]["plan_id"] == plan_id
    assert waiting[0]["text"].endswith("it waits for you: Which currency should I show?")
    assert json.dumps(waiting[0])
