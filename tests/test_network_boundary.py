"""The controller keeps agent containers inside the host's egress policy."""

from __future__ import annotations

import json
import subprocess

import pytest

from dgx_autonomy import cli
from dgx_autonomy.controller import RequestError
from dgx_autonomy.egress import check_policy, probe_container_spec, probe_targets
from dgx_autonomy.runtime import agent_container_name, docker_run_argv

from fakes import Harness, load_egress_policy


def _launch(h: Harness) -> str:
    run_id = str(h.controller.handle("launch", {"brief_text": "x\n", "budget_hours": 1})["run_id"])
    h.inference_ready()
    h.controller.reconcile_once()
    return run_id


def test_policy_in_place(harness: Harness) -> None:
    check = check_policy(harness.settings, harness.runtime)
    assert check.ok, check.problems
    assert check.bridges == {
        "dgx-autonomy-egress": "dgx-egress",
        "dgx-autonomy-internal": "dgx-internal",
    }


@pytest.mark.parametrize(
    ("breakage", "problem"),
    [
        ("no-marker", "not loaded"),
        ("old-boot", "another boot"),
        ("default-bridge", "uses bridge 'br-253366c1381b'"),
        ("no-network", "does not exist"),
    ],
)
def test_no_agent_container_without_the_policy(
    harness: Harness, breakage: str, problem: str
) -> None:
    h = harness
    if breakage == "no-marker":
        h.settings.egress_marker.unlink()
    elif breakage == "old-boot":
        load_egress_policy(h.settings, boot_id="an-earlier-boot")
    elif breakage == "default-bridge":
        h.runtime.bridges[h.settings.egress_network] = "br-253366c1381b"
    else:
        h.runtime.bridges[h.settings.internal_network] = None

    run_id = _launch(h)

    run = h.state.get_run(run_id)
    assert run is not None and run.phase == "failed"
    op = h.state.get_operation(run_id, "workspace.create")
    assert op is not None and op.status == "failed"
    assert op.error is not None and problem in op.error and "refusing" in op.error
    assert agent_container_name(run_id) not in h.runtime.runs


def test_the_policy_check_can_be_switched_off_for_development(harness: Harness) -> None:
    from dataclasses import replace

    h = harness
    h.settings.egress_marker.unlink()
    h.controller._settings = replace(h.settings, require_egress_policy=False)
    run_id = _launch(h)
    assert agent_container_name(run_id) in h.runtime.runs


def test_the_agent_sandbox_uses_public_resolvers(harness: Harness) -> None:
    run_id = _launch(harness)
    spec = harness.runtime.specs[agent_container_name(run_id)]
    argv = docker_run_argv(spec)
    assert argv[argv.index("--dns") + 1] == "1.1.1.1"
    assert "9.9.9.9" in argv


def test_the_probe_stands_where_the_agent_stands(harness: Harness) -> None:
    s = harness.settings
    spec = probe_container_spec(s, [("pypi.org", 443)], 5.0)
    argv = docker_run_argv(spec, detach=False)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert spec.networks == (s.internal_network, s.egress_network)
    assert spec.user == f"{s.agent_uid}:{s.agent_gid}"
    assert spec.dns == s.agent_dns
    assert "--cap-drop" in argv and "no-new-privileges" in argv
    assert spec.mounts == () and spec.ports == ()
    assert argv[argv.index("--entrypoint") + 1] == "/usr/local/bin/python3"
    assert json.loads(spec.command[3]) == [["pypi.org", 443]]


def test_network_probe_reports_results_and_the_policy(harness: Harness) -> None:
    h = harness
    h.runtime.completion_output = (
        0,
        "noise\n"
        + json.dumps({"gateway": "172.19.0.1", "results": [{"host": "10.0.0.1", "ok": False}]}),
    )
    out = h.controller.handle("network.probe", {"targets": [["10.0.0.1", 22]]})
    assert out["gateway"] == "172.19.0.1"
    assert out["results"] == [{"host": "10.0.0.1", "ok": False}]
    assert out["policy"]["ok"] is True
    assert len(h.runtime.completed) == 1


@pytest.mark.parametrize(
    "targets",
    [None, [], [["a b", 1]], [["h", 0]], [["h", 70000]], [["h", True]], [["h"]], "pypi.org:443"],
)
def test_probe_targets_are_validated(targets: object) -> None:
    with pytest.raises(ValueError):
        probe_targets(targets)


def test_inference_stop_is_refused_while_a_run_uses_the_model(harness: Harness) -> None:
    h = harness
    run_id = h.running_run()
    with pytest.raises(RequestError, match=run_id):
        h.controller.handle("inference.stop", {})
    assert h.runtime.removed == []


def test_inference_stop_removes_the_owned_server(harness: Harness) -> None:
    h = harness
    h.controller.handle("inference.ensure", {})
    out = h.controller.handle("inference.stop", {})
    assert out["removed"] is True and out["container"] == "missing"
    assert h.runtime.removed == [h.settings.inference_name]
    assert h.controller.handle("inference.stop", {})["removed"] is False


def test_inference_status_shows_the_reservation(harness: Harness) -> None:
    h = harness
    assert h.controller.handle("inference.status", {})["reservation"] == "none"
    h.settings.reservation_record.parent.mkdir(parents=True)
    h.settings.reservation_record.write_text(json.dumps({"state": "held"}))
    assert h.controller.handle("inference.status", {})["reservation"] == "held"


# --- the CLI's side of the sudo helper ------------------------------------------------


def _helper(code: int, stdout: str, stderr: str = "") -> cli.HelperRunner:
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["sudo", "-n"] and argv[-1] in ("reserve", "release", "status")
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    return run


def test_helper_answer_is_returned() -> None:
    out = cli.reservation_helper("status", _helper(0, '{"reservation": "none"}'))
    assert out == {"reservation": "none"}


def test_helper_refusal_carries_its_reason() -> None:
    with pytest.raises(cli.HelperError, match=r"needs 45\.0 GiB") as exc:
        cli.reservation_helper("release", _helper(2, '{"refused": "needs 45.0 GiB"}'))
    assert exc.value.code == cli.HELPER_REFUSED


def test_a_missing_helper_points_at_the_install_step() -> None:
    run = _helper(1, "", "sudo: a password is required")
    with pytest.raises(cli.HelperError, match=r"password is required.*install\.sh"):
        cli.reservation_helper("reserve", run)
