"""The docker argv the controller produces, asserted without Docker."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dgx_autonomy.config import Settings, load_models
from dgx_autonomy.inference import inference_container_spec
from dgx_autonomy.ports import ContainerSpec, DemoSpec, Mount, PortBinding, WorkspaceSpec
from dgx_autonomy.runtime import (
    SANDBOX_HELPER,
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
        frozen_dir=tmp_path / "runs" / "r" / "frozen",
        session_api_key="session-key",
        secret_key="secret-key",
        control_dir=tmp_path / "runs" / "r" / "control",
        demo_host_port=43007,
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
    assert not any(a in ("-p", "--network=host") for a in argv)
    # Exactly one published port: the demo, on host loopback only.
    assert _flag_values(argv, "--publish") == [f"127.0.0.1:43007:{settings.demo_port}"]
    assert _flag_values(argv, "--network") == [settings.internal_network, settings.egress_network]
    mounts = _flag_values(argv, "--mount")
    assert not any("docker.sock" in m for m in mounts)
    assert sorted(mounts) == sorted(
        [
            f"type=bind,source={tmp_path}/runs/r/agent,target=/workspace",
            f"type=bind,source={tmp_path}/runs/r/frozen,target=/brief,readonly",
            f"type=bind,source={tmp_path}/runs/r/control,target=/dgx-control,readonly",
        ]
    )
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
    assert f"dgx-autonomy.model={model.key}" in _flag_values(argv, "--label")
    assert _flag_values(argv, "--model") == [f"/models/{model.gguf}"]
    assert _flag_values(argv, "--ctx-size") == [str(model.ctx)]
    # A response is bounded even when the client sends no max_tokens.
    assert _flag_values(argv, "--n-predict") == [str(model.max_output_tokens)]
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


@pytest.mark.parametrize("host_ip", ["0.0.0.0", "", "192.168.1.5", "100.75.80.123", "::"])
def test_ports_are_only_published_on_loopback(host_ip: str) -> None:
    spec = ContainerSpec(
        name="x", image="i", user="1:1", ports=(PortBinding(host_ip, 43000, 3000),)
    )
    with pytest.raises(UnsafeSpecError, match=r"127\.0\.0\.1"):
        docker_run_argv(spec)


def _running_agent(settings: Settings) -> dict[str, CommandResult]:
    name = "dgx-autonomy-agent-r1"
    return {"docker container inspect": CommandResult(0, _inspect_json(name, True), "")}


def _exec_argv(settings: Settings, *args: str) -> list[str]:
    return [
        "docker", "exec", "--user", f"{settings.agent_uid}:{settings.agent_gid}",
        "dgx-autonomy-agent-r1", *SANDBOX_HELPER, *args,
    ]  # fmt: skip


def test_stop_agent_runs_the_isolated_helper_as_the_sandbox_user(settings: Settings) -> None:
    answer = {
        "before": [
            {"pid": 7, "ppid": 1, "sid": 7, "state": "S", "cmd": "openhands-agent-server"},
            {"pid": 40, "ppid": 1, "sid": 40, "state": "S", "cmd": "tmux new-session"},
        ],
        "survivors": [],
    }
    runner = FakeRunner(
        {**_running_agent(settings), "docker exec": CommandResult(0, json.dumps(answer), "")}
    )
    report = DockerRuntime(settings, runner).stop_agent("r1", keep_session=55, grace_s=5)
    [call] = runner.commands("exec")
    assert call == _exec_argv(settings, "kill-agent", "--grace", "5", "--keep-sid", "55")
    # -I: the agent's uid cannot hook the helper through user site-packages or PYTHON*.
    assert "-I" in call
    assert report.container_running
    assert [p.cmd for p in report.before] == ["openhands-agent-server", "tmux new-session"]
    assert report.survivors == ()


def test_stop_agent_on_a_stopped_sandbox_kills_nothing(settings: Settings) -> None:
    runner = FakeRunner(
        {
            "docker container inspect": CommandResult(
                0, _inspect_json("dgx-autonomy-agent-r1", False), ""
            )
        }
    )
    report = DockerRuntime(settings, runner).stop_agent("r1", keep_session=None, grace_s=5)
    assert not report.container_running
    assert runner.commands("exec") == []


def test_helper_failures_become_docker_errors(settings: Settings) -> None:
    runner = FakeRunner(
        {**_running_agent(settings), "docker exec": CommandResult(1, "", "OCI runtime exec failed")}
    )
    with pytest.raises(DockerError, match="OCI runtime exec failed"):
        DockerRuntime(settings, runner).stop_agent("r1", keep_session=None, grace_s=5)


def test_ensure_demo_passes_the_command_as_one_argument(settings: Settings) -> None:
    runner = FakeRunner({"docker exec": CommandResult(0, '{"session_id": 88}', "")})
    command = "pnpm build && pnpm start; echo '$HOME'"
    handle = DockerRuntime(settings, runner).ensure_demo(
        DemoSpec("r1", command, 3000, replace_session=12)
    )
    assert handle.session_id == 88
    [call] = runner.commands("exec")
    assert call == _exec_argv(
        settings, "demo-start", "--port", "3000", "--command", command, "--replace-sid", "12"
    )


def test_sandbox_processes_parses_roles(settings: Settings) -> None:
    answer = {
        "processes": [
            {"pid": 1, "ppid": 0, "sid": 1, "state": "S", "cmd": "supervise", "role": "supervisor"},
            {"pid": 30, "ppid": 1, "sid": 30, "state": "S", "cmd": "http.server", "role": "demo"},
        ]
    }
    runner = FakeRunner(
        {**_running_agent(settings), "docker exec": CommandResult(0, json.dumps(answer), "")}
    )
    procs = DockerRuntime(settings, runner).sandbox_processes("r1", keep_session=30)
    assert procs is not None
    assert [(p.pid, p.role) for p in procs] == [(1, "supervisor"), (30, "demo")]
    assert runner.commands("exec")[0][-3:] == ["ps", "--keep-sid", "30"]


def test_restart_sandbox(settings: Settings) -> None:
    runner = FakeRunner()
    DockerRuntime(settings, runner).restart_sandbox("r1")
    assert runner.commands("restart") == [
        ["docker", "restart", "--time", "10", "dgx-autonomy-agent-r1"]
    ]


@pytest.mark.parametrize(("running", "killed"), [(True, True), (False, False)])
def test_kill_container_sigkills_only_a_running_container(
    settings: Settings, running: bool, killed: bool
) -> None:
    name = "dgx-autonomy-agent-r1"
    runner = FakeRunner(
        {"docker container inspect": CommandResult(0, _inspect_json(name, running), "")}
    )
    assert DockerRuntime(settings, runner).kill_container(name) is killed
    assert runner.commands("kill") == ([["docker", "kill", name]] if killed else [])


def test_network_bridge_reads_the_fixed_name_or_dockers_default(settings: Settings) -> None:
    named = [
        {"Id": "8cd12396a84d0000", "Options": {"com.docker.network.bridge.name": "dgx-egress"}}
    ]
    default = [{"Id": "253366c1381bffff", "Options": {}}]
    runner = FakeRunner(
        responses={
            "docker network inspect dgx-autonomy-egress": CommandResult(0, json.dumps(named), ""),
            "docker network inspect old": CommandResult(0, json.dumps(default), ""),
            "docker network inspect gone": CommandResult(1, "", "Error: network gone not found"),
        }
    )
    rt = DockerRuntime(settings, runner)
    assert rt.network_bridge("dgx-autonomy-egress") == "dgx-egress"
    assert rt.network_bridge("old") == "br-253366c1381b"
    assert rt.network_bridge("gone") is None


def test_remove_container_only_removes_what_exists(settings: Settings) -> None:
    runner = FakeRunner(
        responses={
            "docker container inspect missing": CommandResult(1, "", "Error: No such container"),
            "docker container inspect there": CommandResult(0, json.dumps([{"Id": "c1"}]), ""),
        }
    )
    rt = DockerRuntime(settings, runner)
    assert rt.remove_container("missing") is False
    assert rt.remove_container("there") is True
    assert runner.commands("rm") == [["docker", "rm", "-f", "there"]]


def test_run_to_completion_is_attached_and_removed(settings: Settings) -> None:
    runner = FakeRunner(responses={"docker run --rm": CommandResult(0, '{"ok": 1}\n', "")})
    spec = ContainerSpec(name="p", image="img", user="10001:10001", dns=("1.1.1.1",))
    code, out = DockerRuntime(settings, runner).run_to_completion(spec, 30)
    assert (code, out) == (0, '{"ok": 1}\n')
    argv = runner.commands("run")[0]
    assert argv[:3] == ["docker", "run", "--rm"] and "-d" not in argv
    assert _flag_values(argv, "--dns") == ["1.1.1.1"]


def test_a_model_is_not_loaded_without_memory_for_it(tmp_path: Path) -> None:
    from dgx_autonomy.config import PACKAGED_MODELS_FILE, load_models
    from dgx_autonomy.inference import InferenceError, InferenceManager, mem_available

    from fakes import FakeHttp, FakeRuntime

    settings = Settings.from_env({})
    runtime = FakeRuntime(settings)
    big = load_models(PACKAGED_MODELS_FILE).get("qwen3.8-flash-next")
    manager = InferenceManager(settings, runtime, FakeHttp(), available=lambda: 56 * 1024**3)
    with pytest.raises(InferenceError, match="Hold the reservation first"):
        manager.ensure(big)
    assert runtime.runs == []
    roomy = InferenceManager(settings, runtime, FakeHttp(), available=lambda: 110 * 1024**3)
    roomy.ensure(big)
    assert runtime.runs == [settings.inference_name]
    # An existing server is kept (or started again) without a memory check.
    manager.ensure(big)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 10 kB\nMemAvailable: 2048 kB\n")
    assert mem_available(meminfo) == 2048 * 1024
    assert mem_available(tmp_path / "missing") is None


def test_thinking_is_bounded_when_the_model_says_so() -> None:
    from dgx_autonomy.config import PACKAGED_MODELS_FILE
    from dgx_autonomy.inference import REASONING_BUDGET_MESSAGE, inference_command

    catalog = load_models(PACKAGED_MODELS_FILE)
    flash = catalog.get("qwen3.8-flash-next")
    argv = list(inference_command(flash, 8080))
    assert flash.reasoning_budget is not None
    assert flash.reasoning_budget < flash.max_output_tokens  # room to act after thinking
    assert argv[argv.index("--reasoning-budget") + 1] == str(flash.reasoning_budget)
    assert argv[argv.index("--reasoning-budget-message") + 1] == REASONING_BUDGET_MESSAGE
    assert argv[argv.index("--n-predict") + 1] == str(flash.max_output_tokens)

    assert "--reasoning-budget" not in inference_command(
        replace(flash, reasoning_budget=None), 8080
    )


def test_an_sglang_model_runs_offline_with_its_own_image_and_writable_dirs(
    settings: Settings,
) -> None:
    from dgx_autonomy.config import PACKAGED_MODELS_FILE

    model = load_models(PACKAGED_MODELS_FILE).get("qwen3.8-flash-next-sglang")
    spec = inference_container_spec(settings, model)
    argv = docker_run_argv(spec)
    assert spec.image == model.image
    assert _flag_values(argv, "--user") == ["65534:65534"]
    assert _flag_values(argv, "--cap-drop") == ["ALL"]
    assert _flag_values(argv, "--network") == [settings.internal_network]
    assert _flag_values(argv, "--shm-size") == ["8g"]
    root = settings.data_dir / "inference"
    assert _flag_values(argv, "--mount") == [
        f"type=bind,source={settings.models_dir},target=/models,readonly",
        f"type=bind,source={root / 'ple'},target=/ple",
        f"type=bind,source={root / 'cache'},target=/cache",
    ]
    assert "HF_HUB_OFFLINE=1" in _flag_values(argv, "--env")
    # The old PLE table file is deleted before the server starts (a rewrite is slow).
    assert _flag_values(argv, "--entrypoint") == ["/bin/sh"]
    assert 'find /ple -name "ple_table_*.bin" -delete' in argv[argv.index(spec.image) + 2]
    assert _flag_values(argv, "--model-path") == [f"/models/{model.path}"]
    assert _flag_values(argv, "--served-model-name") == [model.key]
    assert _flag_values(argv, "--context-length") == [str(model.ctx)]
    assert _flag_values(argv, "--mem-fraction-static") == [str(model.mem_fraction)]
    assert _flag_values(argv, "--tool-call-parser") == ["auto"]
    assert _flag_values(argv, "--reasoning-parser") == ["qwen3"]
    assert "--jinja" not in argv and not any(a in ("-p", "--publish") for a in argv)


def test_the_memory_check_counts_what_sglang_reserves(tmp_path: Path) -> None:
    from dgx_autonomy.config import PACKAGED_MODELS_FILE
    from dgx_autonomy.inference import InferenceError, InferenceManager

    from fakes import FakeHttp, FakeRuntime

    settings = Settings.from_env({})
    runtime = FakeRuntime(settings)
    model = load_models(PACKAGED_MODELS_FILE).get("qwen3.8-flash-next-sglang")
    made: list[Path] = []
    tight = InferenceManager(
        settings, runtime, FakeHttp(), available=lambda: 100 * 1024**3, prepare_dir=made.append
    )
    with pytest.raises(InferenceError, match="not enough memory"):
        tight.ensure(model)  # 96.9 GiB resident + 8 GiB headroom
    roomy = InferenceManager(
        settings, runtime, FakeHttp(), available=lambda: 110 * 1024**3, prepare_dir=made.append
    )
    roomy.ensure(model)
    assert made == [
        settings.data_dir / "inference" / "ple",
        settings.data_dir / "inference" / "cache",
    ]


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"backend": "vllm", "gguf": "x.gguf"}, "backend must be one of"),
        ({"backend": "sglang", "image": "i"}, "missing field 'path'"),
        ({"backend": "sglang", "path": "m"}, "names its image"),
        ({"backend": "sglang", "path": "../m", "image": "i"}, "relative to the models directory"),
    ],
)
def test_a_backend_needs_its_own_fields(raw: dict[str, str], error: str) -> None:
    from dgx_autonomy.config import ConfigError, _model_from

    base = {"repo": "r", "revision": "v", "size_bytes": 1, "ctx": 1024}
    with pytest.raises(ConfigError, match=error):
        _model_from("m", {**base, **raw})
