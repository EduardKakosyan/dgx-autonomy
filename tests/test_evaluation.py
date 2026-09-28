"""Protected evaluation: completion claims are decided by the frozen checks.

The evaluator containers are fakes (fakes.FakeRuntime "runs" them when they are
created and writes what the runner would report); snapshots use the real git.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy import frozen
from dgx_autonomy.controller import RequestError
from dgx_autonomy.evaluation import (
    LABEL_CRITERION,
    CriterionResult,
    classify,
    evaluator_container_name,
    overall,
)
from dgx_autonomy.state import CriterionRow

from fakes import (
    EvaluatorOutcome,
    Harness,
    load_egress_policy,
    playwright_fails,
    playwright_passes,
    playwright_report,
    pytest_fails,
    pytest_passes,
    runner_crashes,
)

BRIEF = "# Brief\n\nServe a page that says hello, and /version.txt, on port 3000.\n"
CHECKS = {
    "criteria.yaml": """\
criteria:
  - key: home
    description: The home page says hello
    test: home.spec.ts
  - key: version
    description: GET /version.txt returns 2
    test: test_version.py
  - key: tidy
    description: The page looks tidy on a phone
    kind: human_judgment
""",
    "home.spec.ts": "test('home', async ({ page }) => {})\n",
    "test_version.py": "def test_version():\n    assert False\n",
}


def _launch(h: Harness, checks: dict[str, str] | None = None) -> str:
    checks = CHECKS if checks is None else checks
    result = h.controller.handle(
        "launch",
        {
            "brief_text": BRIEF,
            "checks": checks,
            "bundle_digest": frozen.bundle_digest(BRIEF.encode(), frozen.encode(checks)),
            "budget_hours": 1,
        },
    )
    return str(result["run_id"])


def _project(h: Harness, run_id: str) -> Path:
    return h.controller.paths(run_id).project_dir


def _serve_demo(h: Harness, run_id: str) -> None:
    """The agent asks start_demo to serve the app; it comes up listening."""
    d = h.controller.paths(run_id).agent_dir / ".dgx"
    d.mkdir(exist_ok=True)
    body = {"id": f"req{len(h.trace)}", "command": "python3 -m http.server 3000", "port": 3000}
    (d / "demo-request.json").write_text(json.dumps(body))
    h.controller.reconcile_once()
    h.runtime.listening[run_id] = True
    h.controller.reconcile_once()
    demo = h.state.get_demo(run_id)
    assert demo is not None and demo.state == "running", demo


def _running_with_demo(h: Harness, checks: dict[str, str] | None = None) -> str:
    run_id = _launch(h, checks)
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    run = h.state.get_run(run_id)
    assert run is not None and run.phase == "running"
    (_project(h, run_id) / "index.html").write_text("hello\n")
    _serve_demo(h, run_id)
    return run_id


def _ticks(h: Harness, n: int = 8) -> None:
    for _ in range(n):
        h.controller.reconcile_once()


def _phase(h: Harness, run_id: str) -> str:
    run = h.state.get_run(run_id)
    assert run is not None
    return run.phase


# --- reading runner output -------------------------------------------------------------


def _classify(tmp_path: Path, runner: str, outcome: EvaluatorOutcome) -> CriterionResult:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    for name, text in outcome.files.items():
        (out / name).write_text(text)
    return classify(runner, outcome.exit_code, out, outcome.logs)


def test_pytest_results(tmp_path: Path) -> None:
    ok = _classify(tmp_path / "a", "pytest", pytest_passes())
    assert (ok.status, ok.counts) == (
        "passed",
        {"tests": 1, "failures": 0, "errors": 0, "skipped": 0},
    )
    bad = _classify(tmp_path / "b", "pytest", pytest_fails("assert '1' == '2'"))
    assert bad.status == "failed" and "1 test(s) failed" in bad.summary
    assert "test_app::test_version" in bad.excerpt and "assert '1' == '2'" in bad.excerpt


@pytest.mark.parametrize(
    ("outcome", "summary"),
    [
        (EvaluatorOutcome(5, {}, "no tests ran"), "no tests were collected"),
        (runner_crashes(), "internal error"),
        (EvaluatorOutcome(4, {}, "usage"), "invoked wrongly"),
        (EvaluatorOutcome(137, {}, ""), "killed"),
        (EvaluatorOutcome(1, {}, "FAILED"), "no readable report"),
    ],
)
def test_pytest_that_could_not_run_is_an_error_never_a_pass(
    tmp_path: Path, outcome: EvaluatorOutcome, summary: str
) -> None:
    result = _classify(tmp_path, "pytest", outcome)
    assert result.status == "error" and summary in result.summary


def test_playwright_results(tmp_path: Path) -> None:
    ok = _classify(tmp_path / "a", "playwright", playwright_passes())
    assert ok.status == "passed" and ok.summary == "1 test(s) passed"
    bad = _classify(
        tmp_path / "b", "playwright", playwright_fails("\x1b[31mexpect(locator).toHaveText\x1b[39m")
    )
    assert bad.status == "failed"
    assert "shows the greeting" in bad.excerpt and "\x1b" not in bad.excerpt


@pytest.mark.parametrize(
    ("outcome", "summary"),
    [
        (EvaluatorOutcome(1, {}, "Error: browserType.launch: Executable doesn't exist"),
         "wrote no report"),
        (EvaluatorOutcome(1, {"report.json": "{not json"}, ""), "not readable"),
        (EvaluatorOutcome(0, {"report.json": playwright_report(0, 0)}, ""), "no tests ran"),
        (EvaluatorOutcome(1, {"report.json": json.dumps({
            "suites": [], "errors": [{"message": "SyntaxError: Unexpected token"}],
            "stats": {"expected": 0, "unexpected": 0}})}, ""), "no tests ran"),
        (EvaluatorOutcome(137, {"report.json": playwright_report(0, 1, "x")}, ""), "killed"),
    ],
)  # fmt: skip
def test_playwright_that_could_not_run_is_an_error(
    tmp_path: Path, outcome: EvaluatorOutcome, summary: str
) -> None:
    result = _classify(tmp_path, "playwright", outcome)
    assert result.status == "error" and summary in result.summary


def test_a_report_planted_as_a_symlink_is_not_read(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    real = tmp_path / "elsewhere.json"
    real.write_text(playwright_report(1, 0))
    (out / "report.json").symlink_to(real)
    assert classify("playwright", 0, out, "").status == "error"


def _row(key: str, *, required: bool = True, kind: str = "automated") -> CriterionRow:
    return CriterionRow("r", key, 0, kind, required, key, f"{key}.py", "pytest", None, None)


def test_overall_needs_every_required_check_to_pass() -> None:
    criteria = [
        _row("a"),
        _row("b"),
        _row("opt", required=False),
        _row("look", kind="human_judgment"),
    ]
    passed = {"status": "passed"}
    assert overall(criteria, {"a": passed, "b": passed})[0] == "passed"
    assert overall(criteria, {"a": passed, "b": passed, "opt": {"status": "failed"}})[0] == "passed"
    assert overall(criteria, {"a": passed, "b": {"status": "failed"}}) == ("failed", "failed: b")
    # A failure is reported even when another check could not run.
    assert overall(criteria, {"a": {"status": "error"}, "b": {"status": "failed"}})[0] == "failed"
    assert overall(criteria, {"a": passed, "b": {"status": "error"}})[0] == "infra_error"
    assert overall(criteria, {"a": passed})[0] == "infra_error"


# --- the claim / repair loop -------------------------------------------------------------


def test_a_failing_check_goes_back_to_the_builder_and_a_repair_finishes_the_run(
    harness: Harness,
) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["version"] = pytest_fails("GET /version.txt returned 404")

    # 1. The builder claims completion. The claim is evaluated, not believed.
    h.conversation.claim("Done: the page says hello.")
    _ticks(h)
    assert _phase(h, run_id) == "running"
    [ev1] = h.state.evaluations(run_id)
    assert (ev1.trigger, ev1.status, ev1.delivered_at is not None) == ("claim", "failed", True)
    assert ev1.results["home"]["status"] == "passed"
    assert ev1.results["version"]["status"] == "failed"
    assert ev1.check_digest == h.state.get_run(run_id).frozen_digest  # type: ignore[union-attr]
    # The builder got the failure, with the evidence it needs, and resumed.
    [message] = h.conversation.delivered
    assert "FAILED  version" in message.text and "404" in message.text
    assert "PASSED  home" in message.text and "tidy" not in message.text
    assert "/brief/checks/test_version.py" in message.text
    assert h.conversation.status == "running"
    # Each criterion ran in its own disposable container, removed afterwards.
    assert [s.labels[LABEL_CRITERION] for s in h.runtime.evaluators] == ["home", "version"]
    assert not any(n.startswith("dgx-autonomy-eval-") for n in h.runtime.containers)

    # 2. The same claim is not evaluated again while the builder works.
    _ticks(h, 3)
    assert len(h.state.evaluations(run_id)) == 1

    # 3. The builder repairs and claims again. The demo it started before the repair
    #    serves an older snapshot, so it is relaunched before the checks run.
    (_project(h, run_id) / "version.txt").write_text("2\n")
    h.runtime.evaluator_outcomes["version"] = pytest_passes()
    h.conversation.claim("Added version.txt.")
    h.controller.reconcile_once()
    assert any("demo-start" in t and "replace=" in t for t in h.trace)
    h.runtime.listening[run_id] = True
    _ticks(h)
    ev1, ev2 = h.state.evaluations(run_id)
    assert (ev2.status, ev2.delivered_at) == ("passed", None)
    assert ev2.steps["demo_relaunched"]
    assert ev2.snapshot_id != ev1.snapshot_id
    assert _phase(h, run_id) == "finished"
    assert len(h.conversation.delivered) == 1

    report = h.controller.handle("report", {"run_id": run_id})
    assert report["verified"] is True
    assert [c["text"] for c in report["claims"]] == [
        'finish {"message": "Done: the page says hello."}',
        'finish {"message": "Added version.txt."}',
    ]
    assert [c["evaluations"] for c in report["claims"]] == [
        [{"n": 1, "status": "failed"}],
        [{"n": 2, "status": "passed"}],
    ]
    version = next(c for c in report["automated"] if c["key"] == "version")
    assert version["history"] == [
        {"evaluation": 1, "status": "failed"},
        {"evaluation": 2, "status": "passed"},
    ]
    assert (version["status"], version["evaluation"], version["snapshot"]) == (
        "passed",
        2,
        ev2.snapshot_id,
    )
    assert report["human_judgment"] == [
        {
            "key": "tidy",
            "description": "The page looks tidy on a phone",
            "required": True,
            "status": "awaiting human judgment",
        }
    ]
    assert report["evaluations"][1]["changes_since_previous"] == ["A\tversion.txt"]
    evidence = Path(ev2.evidence_dir)
    summary = json.loads((evidence / "evaluation.json").read_text())
    assert summary["status"] == "passed" and summary["snapshot"] == ev2.snapshot_id
    assert (evidence / "1-version.log").read_text() == "1 passed in 0.10s"


def test_a_runner_crash_is_an_infrastructure_error_never_a_pass(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["version"] = runner_crashes()
    claim = h.conversation.claim()
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "infra_error" and "version" in str(ev.detail)
    assert ev.results["version"]["status"] == "error"
    # Not the builder's failure: nothing is delivered, and the run is not finished.
    assert h.conversation.delivered == []
    assert _phase(h, run_id) == "running"

    # The same claim is evaluated again after the backoff, not before.
    h.runtime.evaluator_outcomes["version"] = pytest_passes()
    _ticks(h, 3)
    assert len(h.state.evaluations(run_id)) == 1
    h.clock.advance(seconds=h.settings.evaluation_retry_s + 1)
    _ticks(h)
    _, ev2 = h.state.evaluations(run_id)
    assert (ev2.claim_event_id, ev2.status) == (claim.id, "passed")
    assert _phase(h, run_id) == "finished"


def test_a_project_that_changes_during_the_checks_is_inconclusive(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    project = _project(h, run_id)

    def meanwhile(spec: Any) -> None:
        if spec.labels[LABEL_CRITERION] == "version":
            (project / "late.txt").write_text("written while the checks ran\n")

    h.runtime.on_evaluator.append(meanwhile)
    h.conversation.claim()
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "inconclusive"
    assert "project changed while the checks ran" in str(ev.detail)
    assert h.conversation.delivered == [] and _phase(h, run_id) == "running"


def test_without_a_demo_the_claim_fails_and_says_so(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    h.conversation.claim()
    _ticks(h, 2)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "failed" and "start_demo was never used" in str(ev.detail)
    assert {r["status"] for r in ev.results.values()} == {"not_run"}
    assert h.runtime.evaluators == []
    [message] = h.conversation.delivered
    assert "No demo is serving the app" in message.text


def test_a_demo_that_does_not_come_back_fails_the_claim(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    (_project(h, run_id) / "index.html").write_text("changed after the demo started\n")
    h.conversation.claim()
    h.controller.reconcile_once()  # relaunched from the new snapshot
    h.runtime.listening[run_id] = False  # and it never listens
    h.controller.reconcile_once()
    h.clock.advance(seconds=h.settings.demo_start_timeout_s + 1)
    _ticks(h, 2)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "failed" and "demo is not serving" in str(ev.detail)
    assert "The demo is not serving the app" in h.conversation.delivered[0].text


def test_changed_frozen_checks_are_never_evaluated(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    check = h.controller.paths(run_id).frozen_dir / "checks" / "test_version.py"
    check.write_text("def test_version():\n    pass\n")
    h.conversation.claim()
    _ticks(h)
    [ev, *_] = h.state.evaluations(run_id)
    assert ev.status == "infra_error" and "changed after launch" in str(ev.detail)
    assert h.runtime.evaluators == [] and _phase(h, run_id) == "running"
    report = h.controller.handle("report", {"run_id": run_id})
    assert report["verified"] is False and report["frozen"]["intact"] is False


def test_no_evaluator_starts_without_the_egress_policy(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    load_egress_policy(h.settings, boot_id="another-boot")
    h.conversation.claim()
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "infra_error" and "egress" in str(ev.detail)
    assert h.runtime.evaluators == []


def test_a_hung_evaluator_is_stopped_at_its_timeout(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["home"] = None  # never exits
    h.conversation.claim()
    _ticks(h, 3)
    [ev] = h.state.evaluations(run_id)
    assert ev.open
    h.clock.advance(seconds=h.settings.evaluator_timeout_s + 1)
    _ticks(h, 3)
    [ev] = h.state.evaluations(run_id)
    assert (
        ev.results["home"]["status"] == "error"
        and "no result after" in ev.results["home"]["summary"]
    )
    assert evaluator_container_name(run_id, 1, 0) in h.runtime.killed
    assert ev.status == "infra_error"


# --- deadline, restart, requested evaluations --------------------------------------------


def test_the_deadline_abandons_an_open_evaluation_and_checks_what_is_left(
    harness: Harness,
) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["home"] = None
    h.conversation.claim()
    _ticks(h, 2)
    hung = evaluator_container_name(run_id, 1, 0)
    assert hung in h.runtime.containers

    h.clock.advance(hours=2)
    h.controller.deadline_watchdog().check_once()
    assert _phase(h, run_id) == "stopped"
    ev1 = h.state.evaluations(run_id)[0]
    assert ev1.status == "inconclusive" and str(ev1.detail).startswith("abandoned")
    assert hung not in h.runtime.containers

    # The final evaluation checks what the run left behind, for the report only.
    h.runtime.evaluator_outcomes["home"] = playwright_passes()
    h.runtime.evaluator_outcomes["version"] = pytest_fails()
    h.runtime.listening[run_id] = True
    _ticks(h)
    _, ev2 = h.state.evaluations(run_id)
    assert (ev2.trigger, ev2.status) == ("final", "failed")
    assert h.conversation.delivered == []
    assert _phase(h, run_id) == "stopped"
    report = h.controller.handle("report", {"run_id": run_id})
    assert report["verified"] is False
    assert report["verdict"].startswith("not verified (run expired): evaluation #2 failed")


def test_a_restarted_controller_starts_an_open_evaluation_over(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["home"] = None
    h.conversation.claim()
    _ticks(h, 2)
    [ev] = h.state.evaluations(run_id)
    first_container = evaluator_container_name(run_id, 1, 0)
    assert ev.open and first_container in h.runtime.containers

    h.restart_controller().reconcile_on_start()
    assert first_container not in h.runtime.containers
    evidence = Path(ev.evidence_dir)
    assert not evidence.exists()
    assert [p.name.split(".")[1][:11] for p in evidence.parent.glob("eval-1.*")] == ["interrupted"]

    h.runtime.evaluator_outcomes["home"] = playwright_passes()
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.status == "passed" and _phase(h, run_id) == "finished"


def test_evaluate_reruns_the_checks_on_an_ended_run_only(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    with pytest.raises(RequestError, match="evaluated automatically"):
        h.controller.handle("evaluate", {"run_id": run_id})
    h.controller.handle("stop", {"run_id": run_id})
    h.runtime.listening[run_id] = True
    _ticks(h)
    view = h.controller.handle("evaluate", {"run_id": run_id})
    assert view["trigger"] == "requested"
    _ticks(h)
    assert [e.trigger for e in h.state.evaluations(run_id)] == ["final", "requested"]
    assert h.state.evaluations(run_id)[-1].status == "passed"
    # Passing checks after a stop do not turn the run into a verified completion.
    report = h.controller.handle("report", {"run_id": run_id})
    assert report["verified"] is False and report["phase"] == "stopped"


def test_a_run_without_checks_is_reported_as_claimed_only(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    h.conversation.claim("All done.")
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "finished"
    assert h.state.evaluations(run_id) == []
    report = h.controller.handle("report", {"run_id": run_id})
    assert report["verified"] is False and report["verdict"].startswith("claimed only")
    assert report["automated"] == [] and report["frozen"]["intact"] is True


def test_a_review_is_bundled_on_request_and_kept_apart(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.conversation.claim()
    _ticks(h)
    assert _phase(h, run_id) == "finished"
    review = h.controller.handle("review.request", {"run_id": run_id, "note": "check a11y"})
    bundle = Path(review["bundle_dir"])
    assert {p.name for p in bundle.iterdir()} == {
        "agreement",
        "project.tar",
        "report.json",
        "README.md",
    }
    assert (bundle / "agreement" / "checks" / "home.spec.ts").exists()
    assert "check a11y" in (bundle / "README.md").read_text()
    with pytest.raises(RequestError, match="reviewer"):
        h.controller.handle("review.record", {"run_id": run_id, "n": 1, "text": "ok"})
    recorded = h.controller.handle(
        "review.record",
        {"run_id": run_id, "n": 1, "reviewer": "claude-opus-5-5", "text": "Looks right."},
    )
    assert recorded["status"] == "recorded" and recorded["result_excerpt"] == "Looks right."
    with pytest.raises(RequestError, match="already recorded"):
        h.controller.handle(
            "review.record", {"run_id": run_id, "n": 1, "reviewer": "x", "text": "again"}
        )
    report = h.controller.handle("report", {"run_id": run_id})
    [rv] = report["reviews"]
    assert rv["reviewer"] == "claude-opus-5-5"
    # A review never changes the automated results or the verdict.
    assert report["verified"] is True and all(c["status"] == "passed" for c in report["automated"])


def test_status_shows_the_latest_evaluation(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["home"] = playwright_fails("expected hello")
    h.conversation.claim()
    _ticks(h)
    view = h.controller.handle("status", {"run_id": run_id})
    assert view["criteria"] == {"automated": 2, "human_judgment": 1}
    assert view["evaluation"]["status"] == "failed"
    assert view["evaluation"]["results"]["home"]["status"] == "failed"
    assert view["frozen_digest"] == h.state.get_run(run_id).frozen_digest  # type: ignore[union-attr]


def test_retries_back_off(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    h.runtime.evaluator_outcomes["version"] = runner_crashes()
    h.conversation.claim()
    _ticks(h)
    for attempt in (2, 3):
        delay = h.settings.evaluation_retry_s * 2 ** (attempt - 2)
        h.clock.advance(seconds=delay - 1)
        _ticks(h, 2)
        assert len(h.state.evaluations(run_id)) == attempt - 1
        h.clock.advance(seconds=2)
        _ticks(h)
        assert len(h.state.evaluations(run_id)) == attempt
    assert h.state.evaluations(run_id)[-1].finished_at is not None
    assert h.clock.now() - h.state.evaluations(run_id)[0].started_at < timedelta(minutes=5)


def test_the_claim_is_the_finish_action_even_after_a_thought(harness: Harness) -> None:
    h = harness
    run_id = _running_with_demo(h)
    event = h.conversation.claim("Done.")
    h.conversation.event_log[-1] = type(event)(
        event.id,
        event.timestamp,
        event.kind,
        event.source,
        'All checked -> finish {"message": "Done."}',
    )
    h.conversation.event_log.append(
        type(event)("ev-obs", "t", "ObservationEvent", "environment", "finish: Done.")
    )
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.claim_event_id == event.id and ev.claim_text.endswith('finish {"message": "Done."}')


def test_the_claim_is_found_when_a_long_thought_clips_the_finish_away(harness: Harness) -> None:
    """Seen with SGLang: the summary cut at 300 characters before `-> finish`, and a
    stats update was taken for the claim."""
    h = harness
    run_id = _running_with_demo(h)
    event = h.conversation.claim("Done.")
    long_thought = "Done. Summary of the work: " + "x" * 400
    h.conversation.event_log[-1] = replace(event, text=long_thought[:299] + "…", tool="finish")
    h.conversation.event_log.append(
        type(event)("ev-stats", "t", "ConversationStateUpdateEvent", "environment", "stats={}")
    )
    _ticks(h)
    [ev] = h.state.evaluations(run_id)
    assert ev.claim_event_id == event.id
