"""The docker argv the controller produces, asserted without Docker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dgx_autonomy.config import Settings, load_models
from dgx_autonomy.inference import inference_container_spec
from dgx_autonomy.ports import ContainerSpec, Mount, WorkspaceSpec
from dgx_autonomy.runtime import (
    CommandResult,
    DockerError,
    DockerRuntime,
    UnsafeSpecError,
    agent_container_spec,
    docker_run_argv,
)

from fakes import FakeRunner


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


def _workspace(tmp_path: Path) -> WorkspaceSpec:
    return WorkspaceSpec(
        run_id="20260922-120000-abcdef",
        op_id="20260922-120000-abcdef.workspace.create",
        agent_dir=tmp_path / "runs" / "r" / "agent",
        brief_file=tmp_path / "runs" / "r" / "brief.md",
        session_api_key="session-key",
        secret_key="secret-key",
    )


def _inspect_json(name: str, running: bool, labels: dict[str, str] | None = None) -> str:
    return json.dumps(
        [
            {
                "Id": f"id-{name}",
                "Name": f"/{name}",
                "State": {"Running": running, "Status": "running" if running else "exited"},
                "Config": {"Labels": labels or {}},
            }
        ]
    )


def test_agent_argv_is_unprivileged_and_has_no_socket(settings: Settings, tmp_path: Path) -> None:
    argv = docker_run_argv(agent_container_spec(settings, _workspace(tmp_path)))
    assert argv[:3] == ["docker", "run", "-d"]
    assert _flag_values(argv, "--user") == ["10001:10001"]
    assert _flag_values(argv, "--cap-drop") == ["ALL"]
    assert _flag_values(argv, "--security-opt") == ["no-new-privileges"]
    assert _flag_values(argv, "--pids-limit") == [str(settings.agent_pids_limit)]
    assert _flag_values(argv, "--memory") == [settings.agent_memory]
    assert "--privileged" not in argv
    assert "--gpus" not in argv
    assert not any(a in ("-p", "--publish", "--network=host") for a in argv)
    assert _flag_values(argv, "--network") == [settings.internal_network, settings.egress_network]
    mounts = _flag_values(argv, "--mount")
    assert not any("docker.sock" in m for m in mounts)
    assert f"type=bind,source={tmp_path}/runs/r/agent,target=/workspace" in mounts
    assert f"type=bind,source={tmp_path}/runs/r/brief.md,target=/brief/brief.md,readonly" in mounts
    assert "OH_SESSION_API_KEYS_0=session-key" in _flag_values(argv, "--env")
    assert argv[-5:] == [settings.agent_image, "--host", "0.0.0.0", "--port", "8000"]
    labels = _flag_values(argv, "--label")
    assert "dgx-autonomy.op=20260922-120000-abcdef.workspace.create" in labels
    assert "dgx-autonomy.run=20260922-120000-abcdef" in labels


def test_inference_argv_gets_the_gpu_and_a_read_only_model_dir(settings: Settings) -> None:
    model = load_models(settings.models_file).get(None)
    argv = docker_run_argv(inference_container_spec(settings, model))
    assert _flag_values(argv, "--gpus") == ["all"]
    assert _flag_values(argv, "--cap-drop") == ["ALL"]
    assert _flag_values(argv, "--user") == ["65534:65534"]
    assert _flag_values(argv, "--network") == [settings.internal_network]
    assert _flag_values(argv, "--mount") == [
        f"type=bind,source={settings.models_dir},target=/models,readonly"
    ]
    assert "dgx-autonomy.model=qwen3.6-35b-a3b" in _flag_values(argv, "--label")
    assert _flag_values(argv, "--model") == [f"/models/{model.gguf}"]
    assert _flag_values(argv, "--ctx-size") == [str(model.ctx)]
    assert _flag_values(argv, "--alias") == [model.key]
    assert _flag_values(argv, "--host") == ["0.0.0.0"]
    assert "--jinja" in argv
    assert not any(a in ("-p", "--publish") for a in argv)


@pytest.mark.parametrize("sock", ["/var/run/docker.sock", "/run/docker.sock", "/tmp/x/docker.sock"])
def test_socket_mounts_are_refused(sock: str) -> None:
    spec = ContainerSpec(name="x", image="i", user="1000:1000", mounts=(Mount(sock, "/s"),))
    with pytest.raises(UnsafeSpecError, match="Docker socket"):
        docker_run_argv(spec)


@pytest.mark.parametrize("user", [None, "0", "root", "0:0"])
def test_root_containers_are_refused(user: str | None) -> None:
    with pytest.raises(UnsafeSpecError, match="non-root"):
        docker_run_argv(ContainerSpec(name="x", image="i", user=user))


def test_keeping_capabilities_is_refused() -> None:
    with pytest.raises(UnsafeSpecError, match="capabilities"):
        docker_run_argv(ContainerSpec(name="x", image="i", user="1:1", cap_drop_all=False))


def test_host_root_mount_is_refused() -> None:
    with pytest.raises(UnsafeSpecError, match="host root"):
        docker_run_argv(ContainerSpec(name="x", image="i", user="1:1", mounts=(Mount("/", "/h"),)))


def test_ensure_container_runs_a_missing_container(settings: Settings, tmp_path: Path) -> None:
    spec = agent_container_spec(settings, _workspace(tmp_path))
    inspected: list[int] = []

    def inspect(argv: list[str]) -> CommandResult:
        inspected.append(1)
        if len(inspected) == 1:
            return CommandResult(1, "[]", f"Error: No such container: {spec.name}")
        return CommandResult(0, _inspect_json(spec.name, True), "")

    runner = FakeRunner({"docker container inspect": inspect})
    state = DockerRuntime(settings, runner).ensure_container(spec)
    assert state.running and state.name == spec.name
    [run] = runner.commands("run")
    assert run == docker_run_argv(spec)
    assert runner.commands("start") == []


def test_ensure_container_starts_a_stopped_container(settings: Settings, tmp_path: Path) -> None:
    spec = agent_container_spec(settings, _workspace(tmp_path))
    states = iter([False, True])
    runner = FakeRunner(
        {
            "docker container inspect": lambda argv: CommandResult(
                0, _inspect_json(spec.name, next(states)), ""
            )
        }
    )
    DockerRuntime(settings, runner).ensure_container(spec)
    assert runner.commands("run") == []
    assert runner.commands("start") == [["docker", "start", spec.name]]


def test_ensure_container_leaves_a_running_container_alone(
    settings: Settings, tmp_path: Path
) -> None:
    spec = agent_container_spec(settings, _workspace(tmp_path))
    runner = FakeRunner(
        {"docker container inspect": CommandResult(0, _inspect_json(spec.name, True), "")}
    )
    DockerRuntime(settings, runner).ensure_container(spec)
    assert [c[1] for c in runner.calls] == ["container"]


def test_docker_failures_surface_stderr(settings: Settings, tmp_path: Path) -> None:
    spec = agent_container_spec(settings, _workspace(tmp_path))
    runner = FakeRunner(
        {
            "docker container inspect": CommandResult(1, "", "Error: No such container"),
            "docker run": CommandResult(125, "", "pull access denied for dgx-autonomy/agent"),
        }
    )
    with pytest.raises(DockerError, match="pull access denied"):
        DockerRuntime(settings, runner).ensure_container(spec)


def test_daemon_errors_are_not_mistaken_for_missing_containers(settings: Settings) -> None:
    runner = FakeRunner(
        {"docker container inspect": CommandResult(1, "", "permission denied on docker.sock")}
    )
    with pytest.raises(DockerError, match="permission denied"):
        DockerRuntime(settings, runner).inspect_container("anything")


def test_inspect_finds_run_containers_by_label(settings: Settings) -> None:
    runner = FakeRunner(
        {
            "docker ps": CommandResult(0, "abc\n", ""),
            "docker container inspect abc": CommandResult(
                0, _inspect_json("agent", True, {"dgx-autonomy.run": "r1"}), ""
            ),
        }
    )
    snap = DockerRuntime(settings, runner).inspect("r1")
    assert [c.name for c in snap.containers] == ["agent"]
    assert runner.calls[0] == ["docker", "ps", "-a", "-q", "--filter", "label=dgx-autonomy.run=r1"]
