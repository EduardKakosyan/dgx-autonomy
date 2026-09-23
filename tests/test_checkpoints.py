"""Handoff validation, the controller's fallback, and the recovery context."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dgx_autonomy.checkpoints import (
    EvidenceIndex,
    RecoveryInputs,
    assemble_recovery_context,
    fallback_handoff,
    project_path_exists,
    validate_blocker,
    validate_handoff,
)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
LATEST = {
    "n": 1,
    "status": "failed",
    "snapshot": "abc123def4567",
    "results": {"home": {"status": "failed", "summary": "404"}},
}


def _index(*paths: str, evaluations: tuple[int, ...] = ()) -> EvidenceIndex:
    return EvidenceIndex(path_exists=lambda p: p in paths, evaluations=frozenset(evaluations))


def _handoff(**over: Any) -> dict[str, Any]:
    h: dict[str, Any] = {
        "request_id": "r1",
        "summary": "Steps 1-3 are done; step 4 is next.",
        "roadmap": [
            {"item": "step 1", "status": "done", "evidence": ["step1.txt"]},
            {"item": "step 2", "status": "done", "evidence": ["/workspace/project/step2.txt"]},
            {"item": "step 4", "status": "todo"},
        ],
        "decisions": [{"decision": "plain files", "why": "the brief says so"}],
        "attempts": [{"approach": "touch", "outcome": "worked"}],
        "open_failures": [],
        "next_steps": ["write step4.txt"],
    }
    h.update(over)
    return h


def test_a_valid_handoff_is_normalized() -> None:
    handoff, problems = validate_handoff(
        _handoff(), _index("step1.txt", "step2.txt"), request_id="r1"
    )
    assert problems == []
    assert handoff["roadmap"][1]["evidence"] == ["step2.txt"]  # made relative
    assert handoff["next_steps"] == ["write step4.txt"]
    assert "request_id" not in handoff


def test_a_missing_evidence_reference_is_rejected() -> None:
    _, problems = validate_handoff(
        _handoff(), _index("step1.txt", evaluations=(1,)), request_id="r1"
    )
    assert any("'/workspace/project/step2.txt' does not exist" in p for p in problems)
    bad_eval = _handoff(roadmap=[{"item": "checks pass", "status": "done", "evidence": ["eval:7"]}])
    _, problems = validate_handoff(bad_eval, _index(evaluations=(1,)), request_id="r1")
    assert problems == ["roadmap[0].evidence: there is no evaluation #7 ('eval:7')",
                        "roadmap[0] is done but names no evidence (a file in the project,"
                        " or eval:N)"]  # fmt: skip


def test_claims_cannot_pass_themselves_off_as_verified() -> None:
    _, problems = validate_handoff(
        _handoff(verified={"home": "passed"}),
        _index("step1.txt", "step2.txt"),
        request_id="r1",
    )
    assert problems == ["unknown fields verified; the environment records verified results itself"]


def test_structure_problems_are_listed_for_the_agent_to_fix() -> None:
    raw = _handoff(
        request_id="r0",
        summary="",
        roadmap=[{"item": "x", "status": "finished"}, "step 3"],
        attempts=[{"approach": "y", "outcome": "meh"}],
        next_steps=[],
    )
    _, problems = validate_handoff(raw, _index(), request_id="r1")
    assert "request_id must be 'r1' (the id in the handoff request), got 'r0'" in problems
    assert "summary is required" in problems
    assert "roadmap[0].status must be one of done, in_progress, todo, blocked" in problems
    assert "roadmap[1] must be an object with item, status, evidence" in problems
    assert "attempts[0].outcome must be one of worked, failed, abandoned" in problems
    assert "next_steps must not be empty" in problems
    assert validate_handoff("not an object", _index(), request_id="r1")[1]


def test_project_paths_are_checked_without_following_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.js").write_text("x")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "key").write_text("k")
    os.symlink(outside, project / "link")
    assert project_path_exists(project, "src/app.js")
    assert project_path_exists(project, "src")
    assert project_path_exists(project, "link")  # the link itself is in the project
    assert not project_path_exists(project, "link/key")  # but it is not followed
    assert not project_path_exists(project, "../secret/key")
    assert not project_path_exists(project, "missing.txt")


def test_the_fallback_carries_the_last_handoff_and_adds_only_observations() -> None:
    previous, _ = validate_handoff(_handoff(), _index("step1.txt", "step2.txt"), request_id="r1")
    fb = fallback_handoff(previous, why="no valid handoff within 10 min",
                          recent_activity=["ActionEvent: terminal ls"])  # fmt: skip
    assert "No handoff from the previous conversation" in fb["summary"]
    assert fb["previous_handoff"] == previous
    assert "roadmap" not in fb  # nothing is marked done that the agent did not claim
    assert fallback_handoff(None, why="x", recent_activity=[])["previous_handoff"] is None


def _inputs(**over: Any) -> RecoveryInputs:
    handoff, _ = validate_handoff(_handoff(), _index("step1.txt", "step2.txt"), request_id="r1")
    base: dict[str, Any] = {
        "run_id": "run1",
        "n": 2,
        "reason": "stuck",
        "detail": "terminal ls, 5 times",
        "now": NOW,
        "deadline": NOW + timedelta(hours=10),
        "brief": "# Brief\n\nMake step files 1 to 6.\n",
        "checkpoint_source": "agent_handoff",
        "handoff": handoff,
        "verified": {"latest_evaluation": LATEST},
        "demo": "running: `python3 -m http.server 3000` on port 3000",
        "recent_failures": "FAILED home: GET / returned 404",
        "recent_activity": ["ActionEvent: terminal ls"],
    }
    base.update(over)
    return RecoveryInputs(**base)


def test_the_recovery_context_keeps_claims_and_verified_results_apart() -> None:
    text = assemble_recovery_context(_inputs(), budget_chars=20000)
    assert "fresh conversation (#2)" in text and "10.0 h left" in text
    assert "stuck, repeating the same actions" in text and "Diagnose first" in text
    assert "--- BRIEF (the agreement; also at /brief/brief.md) ---\n# Brief" in text
    claims = text.index("--- CHECKPOINT (written by the previous conversation (its claims)) ---")
    verified = text.index("--- VERIFIED BY THE ENVIRONMENT ---")
    assert claims < verified
    assert "- [done] step 1  [evidence: step1.txt]" in text[claims:verified]
    assert "Evaluation #1 (failed) at project snapshot abc123def456" in text[verified:]
    assert "- home: failed (404)" in text[verified:]
    assert "--- RECENT FAILURES ---\nFAILED home" in text
    fallback = assemble_recovery_context(
        _inputs(checkpoint_source="controller_fallback", handoff=fallback_handoff(
            None, why="none", recent_activity=["MessageEvent: hi"])),
        budget_chars=20000,
    )  # fmt: skip
    assert "recorded by the environment: the previous conversation wrote no usable" in fallback


def test_the_recovery_context_fits_its_budget_dropping_the_least_important_first() -> None:
    big = _inputs(
        brief="# Brief\n" + "requirement\n" * 400,
        recent_failures="F" * 3000,
        recent_activity=[f"ActionEvent: step {i} " + "x" * 200 for i in range(30)],
    )
    full = assemble_recovery_context(big, budget_chars=100_000)
    assert "LAST ACTIONS" in full
    text = assemble_recovery_context(big, budget_chars=5000)
    assert len(text) <= 5000
    assert text.startswith("You are continuing an unattended run")
    assert "--- BRIEF" in text and "--- CHECKPOINT" in text  # kept, clipped if need be
    assert "LAST ACTIONS" not in text  # dropped first
    tiny = assemble_recovery_context(big, budget_chars=500)
    # Over budget rather than without the brief's start or the checkpoint.
    assert "# Brief" in tiny and "/brief/brief.md" in tiny and "--- CHECKPOINT" in tiny


def test_a_blocker_needs_a_concrete_capability_and_two_alternatives() -> None:
    blocker, problems = validate_blocker(
        {
            "missing_capability": "No GPU in the sandbox for CUDA model training",
            "alternatives_tried": ["CPU training: 400 h estimated", "a smaller model: no fit"],
            "needed": "a GPU in the sandbox",
        }
    )
    assert problems == [] and blocker["needed"] == "a GPU in the sandbox"
    _, problems = validate_blocker(
        {"missing_capability": "stuck", "alternatives_tried": ["retry"], "needed": ""}
    )
    assert "missing_capability must say concretely what is missing" in problems
    assert any("at least two different approaches" in p for p in problems)
    assert "needed is required" in problems


def test_a_blocker_review_asks_the_fresh_conversation_to_verify_it() -> None:
    blocker = {"missing_capability": "no GPU in the sandbox", "alternatives_tried": ["a", "b"],
               "needed": "a GPU"}  # fmt: skip
    text = assemble_recovery_context(
        _inputs(reason="blocked-review", detail=None, blocker=blocker), budget_chars=20000
    )
    assert "--- BLOCKER TO REVIEW ---" in text and "Missing: no GPU in the sandbox" in text
    assert "try at least one approach not listed above" in text
