"""Interactive planning: a controller-owned session that produces the frozen agreement.

The planner runs in a sandbox of its own and keeps a draft in its workspace. The
tests drive the controller through its control operations, with the fakes standing
in for Docker, the Agent Server and the model.
"""

from __future__ import annotations

import io
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy import frozen
from dgx_autonomy.controller import RequestError
from dgx_autonomy.evaluation import EVALUATOR_CHECKS_DIR
from dgx_autonomy.openhands_adapter import conversation_id_for
from dgx_autonomy.plan_repl import PlanSession, finish_message
from dgx_autonomy.planning import DRAFT_DIR, EMPTY_TARGET, read_draft
from dgx_autonomy.ports import EventSummary
from dgx_autonomy.runtime import (
    AGENT_CONTROL_DIR,
    AGENT_FROZEN_DIR,
    AGENT_WORKDIR,
    LABEL_PLAN,
    LABEL_ROLE,
    agent_container_name,
    planner_container_name,
)

from fakes import EvaluatorOutcome, Harness, playwright_passes, runner_crashes

REQUEST = "I want a page that greets visitors."
BRIEF = "# Greeting page\n\nServe a page whose heading says hello.\n"
CRITERIA = """\
criteria:
  - key: home
    description: The home page shows the heading hello
    test: home.spec.ts
  - key: version
    description: GET /version.txt returns 2
    test: test_version.py
  - key: tidy
    description: It looks tidy on a phone
    kind: human_judgment
"""
HOME_SPEC = "import { test } from '@playwright/test'\ntest('home', async () => {})\n"
VERSION_TEST = "def test_version() -> None:\n    assert False\n"


def _open_plan(h: Harness, request: str = REQUEST) -> str:
    """A plan whose planner is up and whose conversation has started."""
    plan_id = str(h.controller.handle("plan.start", {"request": request})["plan_id"])
    h.inference_ready()
    h.planner_healthy(plan_id)
    h.controller.reconcile_once()  # the model starts
    h.controller.reconcile_once()  # the planner sandbox starts
    h.controller.reconcile_once()  # it answers: the conversation starts
    plan = h.state.get_plan(plan_id)
    assert plan is not None and plan.state == "open", plan
    h.conversation.status = "finished"  # the planner replied and waits for the operator
    return plan_id


def _draft_dir(h: Harness, plan_id: str) -> Path:
    return h.controller.plan_paths(plan_id).agent_dir / "draft"


def _write_draft(
    h: Harness, plan_id: str, *, brief: str = BRIEF, criteria: str | None = CRITERIA
) -> Path:
    d = _draft_dir(h, plan_id)
    (d / "checks").mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text(brief)
    if criteria is not None:
        (d / "checks" / "criteria.yaml").write_text(criteria)
        (d / "checks" / "home.spec.ts").write_text(HOME_SPEC)
        (d / "checks" / "test_version.py").write_text(VERSION_TEST)
    return d


def test_a_plan_gets_its_own_sandbox_and_a_planning_conversation(harness: Harness) -> None:
    h = harness
    started = h.controller.handle("plan.start", {"request": REQUEST})
    plan_id = started["plan_id"]
    assert started["state"] == "starting"
    # Nothing is a run: no deadline anywhere yet.
    assert h.state.list_runs() == []

    # The model is loading: the planner waits for it.
    h.inference_loading()
    h.controller.reconcile_once()
    assert h.runtime.runs == [h.settings.inference_name]

    h.inference_ready()
    h.controller.reconcile_once()
    name = planner_container_name(plan_id)
    spec = h.runtime.specs[name]
    assert spec.labels == {LABEL_PLAN: plan_id, LABEL_ROLE: "planner"}
    assert spec.user == "10001:10001" and spec.cap_drop_all and spec.no_new_privileges
    assert spec.ports == ()  # the planner serves nothing
    assert {m.target: m.read_only for m in spec.mounts} == {
        AGENT_WORKDIR: False,
        AGENT_CONTROL_DIR: True,
    }
    assert AGENT_FROZEN_DIR not in {m.target for m in spec.mounts}
    assert spec.networks == (h.settings.internal_network, h.settings.egress_network)
    assert spec.memory == h.settings.planner_memory

    h.planner_healthy(plan_id)
    h.controller.reconcile_once()
    [request] = h.conversation.started
    assert request.conversation_id == conversation_id_for(f"plan-{plan_id}")
    assert request.demo_tool is False
    assert request.working_dir == AGENT_WORKDIR
    assert REQUEST in request.message
    assert f"{DRAFT_DIR}/brief.md" in request.message
    assert request.server.url == f"http://{name}:{h.settings.agent_port}"
    plan = h.state.get_plan(plan_id)
    assert plan is not None and plan.state == "open"
    assert plan.conversation_id == request.conversation_id


def test_detach_and_reattach_keep_the_conversation(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    cid = h.conversation.started[0].conversation_id
    h.conversation.event_log.append(
        EventSummary("e1", "t", "MessageEvent", "agent", "What should the page say?")
    )

    # The operator's CLI goes away; a new one asks for the same plan.
    h.controller.handle("plan.send", {"plan_id": plan_id, "text": "It says hello."})
    assert [m.text for m in h.conversation.delivered] == ["It says hello."]
    assert h.conversation.delivered[0].run is True

    # The controller itself restarts: nothing starts a second conversation.
    h.restart_controller()
    h.controller.reconcile_on_start()
    h.controller.reconcile_once()
    assert len(h.conversation.started) == 1
    status = h.controller.handle("plan.status", {})  # the latest open plan
    assert (status["plan_id"], status["conversation_id"]) == (plan_id, cid)
    page = h.controller.handle("plan.events", {"plan_id": plan_id, "since": 0})
    assert page["events"][0]["text"] == "What should the page say?"
    assert h.conversation.event_limits[-1] > 300  # messages come in full


def test_a_planner_sandbox_that_went_down_comes_back(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    name = planner_container_name(plan_id)
    h.runtime.reboot()
    h.controller.reconcile_once()  # the model first
    h.controller.reconcile_once()
    assert name in h.runtime.started  # the same container, docker start
    assert len(h.conversation.started) == 1


def test_the_draft_is_read_without_following_symlinks(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    view = h.controller.handle("plan.draft", {"plan_id": plan_id})
    assert view["brief"] is None and "no draft yet" in view["problem"]

    d = _write_draft(h, plan_id)
    view = h.controller.handle("plan.draft", {"plan_id": plan_id})
    assert view["problem"] is None
    assert view["brief"] == BRIEF
    assert [c["key"] for c in view["criteria"]] == ["home", "version", "tidy"]
    assert view["files"] == ["criteria.yaml", "home.spec.ts", "test_version.py"]
    brief, checks = (
        BRIEF.encode(),
        {
            "criteria.yaml": CRITERIA.encode(),
            "home.spec.ts": HOME_SPEC.encode(),
            "test_version.py": VERSION_TEST.encode(),
        },
    )
    assert view["digest"] == frozen.bundle_digest(brief, checks)

    # The planner plants a symlink to controller state: the draft is refused.
    secret = h.settings.data_dir / "state-secret.txt"
    secret.write_text("controller state")
    os.symlink(secret, d / "checks" / "leak.py")
    view = h.controller.handle("plan.draft", {"plan_id": plan_id})
    assert view["brief"] is None and "leak.py" in view["problem"]
    (d / "checks" / "leak.py").unlink()
    (d / "brief.md").unlink()
    os.symlink(secret, d / "brief.md")
    assert "brief.md" in str(read_draft(h.controller.plan_paths(plan_id).agent_dir).problem)


def test_a_draft_without_an_automated_criterion_cannot_launch(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    _write_draft(
        h,
        plan_id,
        criteria="criteria:\n  - key: tidy\n    description: tidy\n    kind: human_judgment\n",
    )
    view = h.controller.handle("plan.draft", {"plan_id": plan_id})
    assert "no automated acceptance criterion" in view["problem"]
    with pytest.raises(RequestError, match="no automated acceptance criterion"):
        h.controller.handle(
            "plan.launch", {"plan_id": plan_id, "digest": view["digest"], "budget_hours": 1}
        )
    assert h.state.list_runs() == []


def test_launch_freezes_exactly_the_reviewed_draft(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    d = _write_draft(h, plan_id)
    reviewed = h.controller.handle("plan.draft", {"plan_id": plan_id})["digest"]

    # The planner edits the draft after the operator reviewed it.
    (d / "brief.md").write_text(BRIEF + "\nAlso a footer.\n")
    with pytest.raises(RequestError, match="changed since you reviewed it"):
        h.controller.handle(
            "plan.launch", {"plan_id": plan_id, "digest": reviewed, "budget_hours": 1}
        )
    reviewed = h.controller.handle("plan.draft", {"plan_id": plan_id})["digest"]

    # While the planner is working, the draft may be half-written.
    h.conversation.status = "running"
    with pytest.raises(RequestError, match="still working"):
        h.controller.handle(
            "plan.launch", {"plan_id": plan_id, "digest": reviewed, "budget_hours": 1}
        )
    h.conversation.status = "finished"
    with pytest.raises(RequestError, match="budget_hours"):
        h.controller.handle(
            "plan.launch", {"plan_id": plan_id, "digest": reviewed, "budget_hours": 41}
        )
    assert h.state.list_runs() == []

    now = h.clock.now()
    result = h.controller.handle(
        "plan.launch", {"plan_id": plan_id, "digest": reviewed, "budget_hours": 2}
    )
    run = h.state.get_run(result["run_id"])
    assert run is not None
    assert run.frozen_digest == reviewed == result["frozen_digest"]
    assert run.launched_at == now and run.deadline_at == now + timedelta(hours=2)
    assert run.model_key == "qwen3.6-35b-a3b"
    assert [(c.key, c.kind) for c in h.state.criteria(run.id)] == [
        ("home", "automated"),
        ("version", "automated"),
        ("tidy", "human_judgment"),
    ]
    frozen_dir = h.controller.paths(run.id).frozen_dir
    assert (frozen_dir / "brief.md").read_text() == BRIEF + "\nAlso a footer.\n"
    assert (frozen_dir / "checks" / "home.spec.ts").read_text() == HOME_SPEC
    assert frozen.load(frozen_dir, reviewed).digest == reviewed

    # The plan is launched; its sandbox is gone; it cannot launch again.
    plan = h.state.get_plan(plan_id)
    assert plan is not None
    assert (plan.state, plan.run_id, plan.launched_digest) == ("launched", run.id, reviewed)
    assert planner_container_name(plan_id) in h.runtime.removed
    with pytest.raises(RequestError, match="launched"):
        h.controller.handle(
            "plan.launch", {"plan_id": plan_id, "digest": reviewed, "budget_hours": 1}
        )
    assert h.controller.handle("status", {"run_id": run.id})["plan_id"] == plan_id

    # The run then goes the usual way, with the frozen agreement read-only at /brief.
    h.agent_healthy(run.id)
    h.controller.reconcile_once()
    spec = h.runtime.specs[agent_container_name(run.id)]
    assert {m.target: (m.source, m.read_only) for m in spec.mounts}[AGENT_FROZEN_DIR] == (
        str(frozen_dir),
        True,
    )
    assert h.state.get_run(run.id).phase == "running"  # type: ignore[union-attr]


def test_dry_run_runs_each_check_against_an_empty_target(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    _write_draft(h, plan_id)
    digest = h.controller.handle("plan.draft", {"plan_id": plan_id})["digest"]

    result = h.controller.handle("plan.checks", {"plan_id": plan_id})
    assert result["ok"] is True and result["digest"] == digest
    assert [(c["key"], c["status"], c["ok"]) for c in result["checks"]] == [
        ("home", "failed", True),
        ("version", "failed", True),
    ]
    specs = h.runtime.completed
    assert [s.labels[LABEL_ROLE] for s in specs] == ["dry-run", "dry-run"]
    for spec in specs:
        assert spec.env["APP_URL"] == EMPTY_TARGET
        assert spec.ports == () and spec.networks == (h.settings.egress_network,)
        checks = {m.target: m for m in spec.mounts}[EVALUATOR_CHECKS_DIR]
        # A controller-owned copy of the draft, not the planner's workspace.
        assert checks.read_only
        assert checks.source.startswith(str(h.controller.plan_paths(plan_id).dry_runs_dir))
    # The planner learns the result as context, without being run.
    [note] = h.conversation.delivered
    assert note.run is False and "dry-ran the draft checks" in note.text
    view = h.controller.handle("plan.draft", {"plan_id": plan_id})
    assert view["dry_run_current"] is True and view["dry_run"]["n"] == 1

    # A check that passes with no app checks nothing; one that crashes does not run.
    h.runtime.dry_run_outcomes = {"home": playwright_passes(), "version": runner_crashes()}
    result = h.controller.handle("plan.checks", {"plan_id": plan_id})
    assert result["ok"] is False and result["n"] == 2
    home, version = result["checks"]
    assert (home["status"], home["ok"]) == ("passed", False)
    assert "does not check anything" in home["verdict"]
    assert (version["status"], version["ok"]) == ("error", False)
    assert "PROBLEM" in h.conversation.delivered[-1].text

    # A check that never finishes is a problem too, not a hang.
    h.runtime.dry_run_outcomes = {"home": None}
    result = h.controller.handle("plan.checks", {"plan_id": plan_id})
    assert result["checks"][0]["status"] == "error"
    assert "did not complete" in result["checks"][0]["summary"]


def test_close_removes_the_sandbox_and_keeps_the_draft(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    d = _write_draft(h, plan_id)
    with pytest.raises(RequestError, match="still uses the model"):
        h.controller.handle("inference.stop", {})
    closed = h.controller.handle("plan.close", {"plan_id": plan_id})
    assert closed["state"] == "closed"
    assert planner_container_name(plan_id) in h.runtime.removed
    assert (d / "brief.md").read_text() == BRIEF
    with pytest.raises(RequestError, match="closed"):
        h.controller.handle("plan.send", {"plan_id": plan_id, "text": "hello?"})
    with pytest.raises(RequestError, match="no open plan"):
        h.controller.handle("plan.send", {"text": "hello?"})
    h.controller.reconcile_once()
    assert h.runtime.containers.get(planner_container_name(plan_id)) is None


def test_a_planner_that_never_comes_up_fails(harness: Harness) -> None:
    h = harness
    plan_id = h.controller.handle("plan.start", {"request": REQUEST})["plan_id"]
    h.inference_ready()
    h.controller.reconcile_once()
    h.controller.reconcile_once()  # sandbox created, but its Agent Server never answers
    h.clock.advance(seconds=h.settings.planner_start_timeout_s + 1)
    h.controller.reconcile_once()
    plan = h.state.get_plan(plan_id)
    assert plan is not None and plan.state == "failed"
    assert "did not come up" in str(plan.error)
    assert planner_container_name(plan_id) in h.runtime.removed


def test_plan_start_rejects_bad_requests(harness: Harness) -> None:
    with pytest.raises(RequestError, match="non-empty request"):
        harness.controller.handle("plan.start", {"request": "  "})
    with pytest.raises(RequestError, match="unknown model"):
        harness.controller.handle("plan.start", {"request": REQUEST, "model_key": "gpt-9"})
    assert harness.state.list_plans() == []


# --- the REPL -------------------------------------------------------------------------


class FakeControl:
    def __init__(self, pages: list[dict[str, Any]], draft: dict[str, Any]) -> None:
        self.pages = pages
        self.draft = draft
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, op: str, args: dict[str, Any] | None = None, **_: Any) -> Any:
        args = args or {}
        self.calls.append((op, args))
        if op == "plan.events":
            if self.pages:
                return self.pages.pop(0)
            return {"events": [], "next": args["since"], "conversation_status": "finished"}
        if op == "plan.draft":
            return self.draft
        if op == "plan.launch":
            return {"run_id": "r1", "deadline_at": "d", "frozen_digest": args["digest"]}
        if op == "plan.status":
            return {"plan_id": "p1", "state": "open"}
        return {}


def _ev(kind: str, source: str, text: str) -> dict[str, Any]:
    return {"id": "x", "timestamp": "t", "kind": kind, "source": source, "text": text}


def test_the_repl_streams_the_reply_and_launches_the_draft_it_showed() -> None:
    draft = {
        "digest": "sha256:abc",
        "problem": None,
        "brief": BRIEF,
        "files": ["criteria.yaml", "home.spec.ts"],
        "criteria": [
            {"key": "home", "kind": "automated", "required": True, "test": "home.spec.ts",
             "runner": "playwright", "description": "hello"},
        ],
        "dry_run": {"n": 1, "ok": True, "digest": "sha256:abc"},
        "dry_run_current": True,
    }  # fmt: skip
    pages = [
        {"events": [], "next": 1, "conversation_status": "finished"},  # before it runs
        {
            "events": [
                _ev("MessageEvent", "user", "Make it say hello"),
                _ev("ActionEvent", "agent", 'file_editor {"command": "create"}'),
                _ev("ActionEvent", "agent", 'Done -> finish {"message": "Draft ready.\\nLook."}'),
            ],
            "next": 4,
            "conversation_status": "finished",
        },
    ]
    control = FakeControl(pages, draft)
    out = io.StringIO()
    answers = iter(["y"])
    session = PlanSession(
        control, "p1", out=out, read=lambda _p: next(answers), sleep=lambda _s: None
    )
    session.since = 1
    assert session.handle("Make it say hello") is True
    printed = out.getvalue()
    assert "planner> Draft ready.\nLook." in printed
    assert "· file_editor" in printed
    assert "you> Make it say hello" not in printed  # the operator's own words are not echoed

    assert session.handle("/launch 3") is False  # launched: the REPL ends
    op, args = control.calls[-1]
    assert (op, args) == ("plan.launch", {"plan_id": "p1", "digest": "sha256:abc",
                                          "budget_hours": 3.0})  # fmt: skip
    assert session.launched is not None and session.launched["run_id"] == "r1"


def test_the_repl_insists_before_launching_checks_that_never_dry_ran() -> None:
    draft = {"digest": "sha256:abc", "problem": None, "brief": BRIEF, "files": [],
             "criteria": [], "dry_run": None, "dry_run_current": False}  # fmt: skip
    control = FakeControl([], draft)
    answers = iter(["", "force", "y"])
    session = PlanSession(
        control, "p1", out=io.StringIO(), read=lambda _p: next(answers), sleep=lambda _s: None
    )
    assert session.handle("/launch") is True  # declined
    assert "plan.launch" not in [c[0] for c in control.calls]
    assert session.handle("/launch") is False
    assert control.calls[-1][1]["budget_hours"] == 40.0


def test_finish_message_is_read_from_the_summarized_action() -> None:
    assert finish_message('finish {"message": "a\\nb"}') == "a\nb"
    assert finish_message('I am done -> finish {"message": "ok"}') == "ok"
    assert finish_message('terminal {"command": "echo finish {"}') is None
    assert finish_message("finish {broken") is None


def test_a_dry_run_that_times_out_leaves_evidence(harness: Harness) -> None:
    h = harness
    plan_id = _open_plan(h)
    _write_draft(h, plan_id)
    h.runtime.dry_run_outcomes = {"version": EvaluatorOutcome(5, {}, "no tests ran")}
    result = h.controller.handle("plan.checks", {"plan_id": plan_id})
    version = result["checks"][1]
    assert (version["status"], version["ok"]) == ("error", False)
    evidence = Path(result["evidence_dir"])
    assert (evidence / "dry-run.json").is_file()
    assert (evidence / "1-version.log").read_text() == "no tests ran"
    assert (evidence / "agreement" / "checks" / "test_version.py").read_text() == VERSION_TEST


def test_a_message_that_starts_like_a_command_is_not_run_as_one() -> None:
    reply = {"events": [_ev("MessageEvent", "agent", "Fixed.")], "next": 1,
             "conversation_status": "finished"}  # fmt: skip
    control = FakeControl([reply], {})
    out = io.StringIO()
    session = PlanSession(control, "p1", out=out, read=lambda _p: "", sleep=lambda _s: None)
    assert session.handle("/draft says the draft cannot launch") is True
    assert control.calls == [] and "takes no argument; nothing was sent" in out.getvalue()
    session.handle(" /draft is wrong, fix it")
    assert control.calls[0] == ("plan.send", {"plan_id": "p1", "text": "/draft is wrong, fix it"})
