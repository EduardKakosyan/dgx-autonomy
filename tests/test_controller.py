from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from dgx_autonomy.config import PACKAGED_MODELS_FILE, load_models
from dgx_autonomy.controller import RequestError
from dgx_autonomy.openhands_adapter import ConversationError, conversation_id_for
from dgx_autonomy.ports import EventSummary
from dgx_autonomy.runtime import AGENT_PROJECT_DIR, agent_container_name

from fakes import Harness

BRIEF = "Create hello.txt in the project directory containing today's date.\n"
DEFAULT_MODEL = load_models(PACKAGED_MODELS_FILE).default


def _launch(h: Harness, **extra: object) -> str:
    result = h.controller.handle("launch", {"brief_text": BRIEF, "budget_hours": 1, **extra})
    return str(result["run_id"])


def _phase(h: Harness, run_id: str) -> str:
    run = h.state.get_run(run_id)
    assert run is not None
    return run.phase


def test_launch_persists_run_deadline_and_brief(harness: Harness) -> None:
    result = harness.controller.handle(
        "launch", {"brief_text": BRIEF, "budget_hours": 2.5, "model_key": "qwen3.6-35b-a3b"}
    )
    run = harness.state.get_run(result["run_id"])
    assert run is not None
    assert run.phase == "launched"
    assert run.deadline_at - run.launched_at == timedelta(hours=2.5)
    assert result["deadline_at"] == run.deadline_at.isoformat()
    assert Path(run.brief_path).read_text() == BRIEF
    # launch only records intent; nothing external has happened yet
    assert harness.runtime.runs == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"brief_text": ""}, "brief_text"),
        ({"brief_text": BRIEF, "budget_hours": 41}, "budget_hours"),
        ({"brief_text": BRIEF, "budget_hours": 0}, "budget_hours"),
        ({"brief_text": BRIEF, "budget_hours": "soon"}, "number"),
        ({"brief_text": BRIEF, "model_key": "gpt-9"}, "unknown model"),
    ],
)
def test_launch_rejects_bad_requests(
    harness: Harness, args: dict[str, object], message: str
) -> None:
    with pytest.raises(RequestError, match=message):
        harness.controller.handle("launch", args)
    assert harness.state.list_runs() == []


def test_unknown_operation_is_a_request_error(harness: Harness) -> None:
    with pytest.raises(RequestError, match="unknown operation"):
        harness.controller.handle("rm -rf", {})


def test_launch_to_running_to_finished(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)

    # 1. The model is still loading: the run waits at inference, nothing else starts.
    h.inference_loading()
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "launched"
    assert h.runtime.runs == [h.settings.inference_name]
    op = h.state.get_operation(run_id, "inference.start")
    assert op is not None and op.status == "intended"

    # 2. The model is ready; the Agent Server container starts but is not healthy yet.
    h.inference_ready()
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "launched"
    assert h.runtime.runs == [h.settings.inference_name, agent_container_name(run_id)]
    assert h.conversation.started == []

    # 3. Agent Server healthy: the conversation starts and the run is running.
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "running"
    [request] = h.conversation.started
    assert request.conversation_id == conversation_id_for(run_id)
    assert request.working_dir == AGENT_PROJECT_DIR
    assert request.llm.base_url == f"{h.settings.inference_url}/v1"
    assert request.llm.model == DEFAULT_MODEL  # the catalog's default
    assert request.llm.max_output_tokens == 16384
    assert BRIEF.strip() in request.message
    run = h.state.get_run(run_id)
    assert run is not None and run.conversation_id == request.conversation_id
    assert {o.kind: o.status for o in h.state.operations(run_id)} == {
        "inference.start": "done",
        "workspace.create": "done",
        "conversation.start": "done",
    }

    # 4. Further ticks while running do not restart anything.
    h.controller.reconcile_once()
    assert len(h.conversation.started) == 1
    assert h.runtime.runs.count(agent_container_name(run_id)) == 1

    # 5. OpenHands reports FINISHED.
    h.conversation.event_log.append(
        EventSummary("e1", "2026-09-22T12:05:00", "ActionEvent", "agent", "finish")
    )
    h.conversation.status = "finished"
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "finished"

    status = h.controller.handle("status", {"run_id": run_id})
    assert status["phase"] == "finished"
    assert status["conversation_status"] == "finished"
    assert status["last_event"]["text"] == "finish"
    assert status["workspace_dir"].endswith(f"runs/{run_id}/agent/project")


def test_a_finished_project_is_readable_by_the_operator(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "running"

    # The agent's file tools write 0600; a symlink must not be followed.
    project = h.controller.paths(run_id).project_dir
    (project / "src").mkdir(mode=0o700)
    hello = project / "src" / "hello.txt"
    hello.write_text("2026-09-22\n")
    hello.chmod(0o600)
    outside = project.parent / "outside.txt"
    outside.write_text("not the operator's business\n")
    outside.chmod(0o600)
    (project / "link").symlink_to(outside)

    h.conversation.status = "finished"
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "finished"
    assert (project / "src").stat().st_mode & 0o005 == 0o005
    assert hello.stat().st_mode & 0o777 == 0o604
    assert outside.stat().st_mode & 0o777 == 0o600


def test_status_defaults_to_latest_run(harness: Harness) -> None:
    _launch(harness)
    harness.clock.advance(minutes=1)
    second = _launch(harness)
    assert harness.controller.handle("status", {})["run_id"] == second


def test_status_without_runs_is_a_request_error(harness: Harness) -> None:
    with pytest.raises(RequestError, match="no runs"):
        harness.controller.handle("status", {})


def test_agent_container_is_unprivileged_and_isolated(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_ready()
    h.controller.reconcile_once()
    spec = h.runtime.specs[agent_container_name(run_id)]
    assert spec.user == "10001:10001"
    assert spec.cap_drop_all and spec.no_new_privileges
    assert all("docker.sock" not in m.source for m in spec.mounts)
    assert all(not m.source.startswith(str(h.settings.state_db.parent)) for m in spec.mounts)
    # The frozen agreement (brief + checks) is the only other host path, read-only.
    frozen_mount = next(m for m in spec.mounts if m.target == "/brief")
    assert frozen_mount.read_only
    assert frozen_mount.source == str(h.controller.paths(run_id).frozen_dir)
    run_root = str(h.controller.paths(run_id).root)
    for private in ("evidence", "snapshots.git", "secrets", "reviews"):
        assert all(not m.source.startswith(f"{run_root}/{private}") for m in spec.mounts)
    assert spec.networks == (h.settings.internal_network, h.settings.egress_network)
    assert not spec.gpus


def test_conversation_error_is_retried_until_the_start_timeout(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_ready()
    h.agent_healthy(run_id)
    h.conversation.fail_start = ConversationError("409 not ready")
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "launched"

    h.conversation.fail_start = None
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "running"

    run2 = _launch(h)
    h.agent_healthy(run2)
    h.conversation.fail_start = ConversationError("still broken")
    h.controller.reconcile_once()
    h.clock.advance(seconds=h.settings.agent_start_timeout_s + 1)
    h.controller.reconcile_once()
    assert _phase(h, run2) == "failed"
    op = h.state.get_operation(run2, "conversation.start")
    assert op is not None and op.status == "failed" and "still broken" in (op.error or "")


def test_inference_crash_fails_the_run_with_logs(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_loading()
    h.controller.reconcile_once()
    h.runtime.exit(h.settings.inference_name, code=137, logs="cudaMalloc failed: out of memory")
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "failed"
    op = h.state.get_operation(run_id, "inference.start")
    assert op is not None and op.status == "failed"
    assert "out of memory" in (op.error or "")
    assert h.runtime.runs.count(agent_container_name(run_id)) == 0


def test_inference_load_timeout_fails_the_run(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_loading()
    h.controller.reconcile_once()
    h.clock.advance(seconds=h.settings.inference_load_timeout_s + 1)
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "failed"


def test_agent_container_exit_fails_the_run(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    h.inference_ready()
    h.controller.reconcile_once()
    h.runtime.exit(agent_container_name(run_id), logs="PermissionError: /workspace")
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "failed"
    op = h.state.get_operation(run_id, "workspace.create")
    assert op is not None and "PermissionError" in (op.error or "")


@pytest.mark.parametrize("status", ["error", "stuck"])
def test_conversation_error_or_stuck_does_not_end_the_run(harness: Harness, status: str) -> None:
    """Only the deadline, a stop or a confirmed blocker end a run (test_rollover.py)."""
    h = harness
    run_id = _launch(h)
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    h.conversation.status = status
    h.controller.reconcile_once()
    h.controller.reconcile_once()
    assert _phase(h, run_id) == "running"
    assert h.conversation.delivered  # a nudge, or a handoff request


def test_a_second_model_does_not_silently_replace_the_first(harness: Harness) -> None:
    h = harness
    _launch(h)
    h.inference_ready()
    h.controller.reconcile_once()
    labels = dict(h.runtime.containers[h.settings.inference_name].labels)
    labels["dgx-autonomy.model"] = "some-other-model"
    h.runtime.containers[h.settings.inference_name] = replace(
        h.runtime.containers[h.settings.inference_name], labels=labels
    )
    run2 = _launch(h)
    h.controller.reconcile_once()
    assert _phase(h, run2) == "failed"
    op = h.state.get_operation(run2, "inference.start")
    assert op is not None and "some-other-model" in (op.error or "")


def test_logs_page_through_conversation_events(harness: Harness) -> None:
    h = harness
    run_id = _launch(h)
    assert h.controller.handle("logs", {"run_id": run_id})["events"] == []
    h.inference_ready()
    h.agent_healthy(run_id)
    h.controller.reconcile_once()
    h.conversation.event_log.extend(
        EventSummary(f"e{i}", f"t{i}", "MessageEvent", "agent", f"step {i}") for i in range(5)
    )
    page = h.controller.handle("logs", {"run_id": run_id, "since": 2, "limit": 2})
    assert [e["text"] for e in page["events"]] == ["step 2", "step 3"]
    assert page["next"] == 4


def test_inference_request_passthrough_is_restricted(harness: Harness) -> None:
    h = harness
    with pytest.raises(RequestError, match="not an allowed"):
        h.controller.handle("inference.request", {"method": "POST", "path": "/slots?action=erase"})
    h.inference_ready()
    res = h.controller.handle("inference.request", {"method": "GET", "path": "/health"})
    assert res == {"status": 200, "body": {"status": "ok"}, "error": None}


def test_run_directories_hand_the_workspace_to_the_agent_user(harness: Harness) -> None:
    run_id = _launch(harness)
    paths = harness.controller.paths(run_id)
    assert (str(paths.agent_dir), 10001, 10001) in harness.chowned
    assert (str(paths.project_dir), 10001, 10001) in harness.chowned
    assert (paths.secrets_dir.stat().st_mode & 0o777) == 0o700
    assert ((paths.secrets_dir / "session_api_key").stat().st_mode & 0o777) == 0o600
    assert (paths.frozen_dir.stat().st_mode & 0o777) == 0o755
    assert ((paths.frozen_dir / "brief.md").stat().st_mode & 0o777) == 0o644
