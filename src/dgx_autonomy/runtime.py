"""docker CLI adapter behind RuntimePort.

Only the controller container holds the Docker socket. Every container it creates
goes through `docker_run_argv`, which refuses a socket mount, refuses to publish a
port anywhere but host loopback, and always adds the hardening flags. The agent
container is built here directly rather than through OpenHands `DockerWorkspace`,
which adds no `--user`, `--cap-drop` or security options.

Inside the agent sandbox, PID 1 is `containers/sandbox/dgx_sandbox.py`. The
controller reaches it with `docker exec` as the sandbox user: to list and classify
processes, to end agent execution, and to start or check the demo session.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings
from .ports import (
    ContainerSpec,
    ContainerState,
    DemoHandle,
    DemoSpec,
    DemoStatus,
    KillReport,
    Mount,
    PortBinding,
    RuntimeSnapshot,
    SandboxProcess,
    WorkspaceHandle,
    WorkspaceSpec,
)

LABEL_PREFIX = "dgx-autonomy"
LABEL_RUN = f"{LABEL_PREFIX}.run"
LABEL_OP = f"{LABEL_PREFIX}.op"
LABEL_ROLE = f"{LABEL_PREFIX}.role"
LABEL_PLAN = f"{LABEL_PREFIX}.plan"

FORBIDDEN_MOUNT_SOURCES = frozenset({"/var/run/docker.sock", "/run/docker.sock"})
BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"

AGENT_WORKDIR = "/workspace"
AGENT_PROJECT_DIR = f"{AGENT_WORKDIR}/project"
# The frozen agreement (frozen.py), read-only: brief.md, checks/, manifest.json.
AGENT_FROZEN_DIR = "/brief"
AGENT_BRIEF_PATH = f"{AGENT_FROZEN_DIR}/brief.md"
AGENT_CHECKS_DIR = f"{AGENT_FROZEN_DIR}/checks"
# Controller-owned, mounted read-only: `mode` (agent | demo-only) and `demo.json`.
AGENT_CONTROL_DIR = "/dgx-control"
# The agent writes its start_demo request here (see demo_tool.py).
AGENT_STATE_DIR = f"{AGENT_WORKDIR}/.dgx"

# The in-sandbox helper, run isolated (-I) so the agent's uid cannot hook its imports.
SANDBOX_HELPER = ("/usr/local/bin/python3", "-I", "/opt/dgx-autonomy/dgx_sandbox.py")
LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult: ...


def subprocess_runner(argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class DockerError(RuntimeError):
    pass


class UnsafeSpecError(ValueError):
    """A container spec would hand the container host authority."""


def _check_spec(spec: ContainerSpec) -> None:
    for m in spec.mounts:
        src = m.source.rstrip("/")
        if src in FORBIDDEN_MOUNT_SOURCES or src.endswith("/docker.sock"):
            raise UnsafeSpecError(f"{spec.name}: refusing to mount the Docker socket ({m.source})")
        if src in ("", "/"):
            raise UnsafeSpecError(f"{spec.name}: refusing to mount the host root")
    if spec.user is None or spec.user.split(":")[0] in ("0", "root"):
        raise UnsafeSpecError(f"{spec.name}: containers must run as a non-root user")
    if not spec.cap_drop_all:
        raise UnsafeSpecError(f"{spec.name}: containers must drop all capabilities")
    for p in spec.ports:
        if p.host_ip != LOOPBACK:
            raise UnsafeSpecError(
                f"{spec.name}: ports may only be published on {LOOPBACK}, not {p.host_ip!r}"
            )


def _mount_arg(m: Mount) -> str:
    arg = f"type=bind,source={m.source},target={m.target}"
    return f"{arg},readonly" if m.read_only else arg


def docker_run_argv(spec: ContainerSpec, *, detach: bool = True) -> list[str]:
    """The exact argv for `docker run -d` (or `--rm`, attached). Pure, so tests can
    assert on it."""
    _check_spec(spec)
    argv = ["docker", "run", "-d" if detach else "--rm", "--name", spec.name]
    for key, value in sorted(spec.labels.items()):
        argv += ["--label", f"{key}={value}"]
    argv += ["--user", str(spec.user), "--cap-drop", "ALL"]
    if spec.no_new_privileges:
        argv += ["--security-opt", "no-new-privileges"]
    if spec.pids_limit is not None:
        argv += ["--pids-limit", str(spec.pids_limit)]
    if spec.memory is not None:
        argv += ["--memory", spec.memory]
    if spec.cpus is not None:
        argv += ["--cpus", spec.cpus]
    if spec.gpus:
        argv += ["--gpus", "all"]
    if spec.restart is not None:
        argv += ["--restart", spec.restart]
    for net in spec.networks:
        argv += ["--network", net]
    for server in spec.dns:
        argv += ["--dns", server]
    for p in spec.ports:
        argv += ["--publish", f"{p.host_ip}:{p.host_port}:{p.container_port}"]
    for m in spec.mounts:
        argv += ["--mount", _mount_arg(m)]
    for key, value in sorted(spec.env.items()):
        argv += ["--env", f"{key}={value}"]
    if spec.entrypoint is not None:
        argv += ["--entrypoint", spec.entrypoint]
    if spec.workdir is not None:
        argv += ["--workdir", spec.workdir]
    argv.append(spec.image)
    argv += list(spec.command)
    return argv


def agent_container_name(run_id: str) -> str:
    return f"{LABEL_PREFIX}-agent-{run_id}"


def agent_container_spec(settings: Settings, spec: WorkspaceSpec) -> ContainerSpec:
    """The unprivileged sandbox that runs the Agent Server and its tools.

    It sees its own workspace (project + SDK conversation storage), the frozen
    agreement (brief and checks) read-only, and a read-only control directory. It
    gets the private inference network and an egress network, but no Docker socket,
    no controller state, no snapshots and no evaluation evidence. The only
    published port is the demo's, on host loopback. The host's egress rules
    (host/nftables-autonomy.nft) keep it off the DGX, the LAN and the tailnet, so it
    resolves names through public resolvers rather than the host's (the LAN router).
    """
    return ContainerSpec(
        name=agent_container_name(spec.run_id),
        image=settings.agent_image,
        labels={LABEL_RUN: spec.run_id, LABEL_OP: spec.op_id, LABEL_ROLE: "agent"},
        networks=(settings.internal_network, settings.egress_network),
        mounts=(
            Mount(str(spec.agent_dir), AGENT_WORKDIR),
            Mount(str(spec.frozen_dir), AGENT_FROZEN_DIR, read_only=True),
            Mount(str(spec.control_dir), AGENT_CONTROL_DIR, read_only=True),
        ),
        ports=(PortBinding(LOOPBACK, spec.demo_host_port, settings.demo_port),),
        dns=settings.agent_dns,
        env={
            # The Agent Server listens on the private network; require the key.
            "OH_SESSION_API_KEYS_0": spec.session_api_key,
            # Stable per run so persisted secrets survive an Agent Server restart.
            "OH_SECRET_KEY": spec.secret_key,
        },
        command=("--host", "0.0.0.0", "--port", str(settings.agent_port)),
        user=f"{settings.agent_uid}:{settings.agent_gid}",
        memory=settings.agent_memory,
        cpus=settings.agent_cpus,
        pids_limit=settings.agent_pids_limit,
    )


def planner_container_name(plan_id: str) -> str:
    return f"{LABEL_PREFIX}-plan-{plan_id}"


@dataclass(frozen=True)
class PlannerSpec:
    plan_id: str
    # Mounted at /workspace: the planner's scratch space, its draft and its conversation.
    agent_dir: str
    # Controller-owned, read-only at /dgx-control: the supervisor mode.
    control_dir: str
    session_api_key: str
    secret_key: str


def planner_container_spec(settings: Settings, spec: PlannerSpec) -> ContainerSpec:
    """The planning sandbox: the agent image and hardening, without a project.

    It sees only its own workspace (where the draft is) and the control directory.
    No frozen agreement (there is none yet), no published port (no demo), no Docker
    socket, no controller state. Research needs the internet, so it gets the same
    two networks and the same host egress policy as a run's sandbox.
    """
    return ContainerSpec(
        name=planner_container_name(spec.plan_id),
        image=settings.agent_image,
        labels={LABEL_PLAN: spec.plan_id, LABEL_ROLE: "planner"},
        networks=(settings.internal_network, settings.egress_network),
        mounts=(
            Mount(spec.agent_dir, AGENT_WORKDIR),
            Mount(spec.control_dir, AGENT_CONTROL_DIR, read_only=True),
        ),
        dns=settings.agent_dns,
        env={"OH_SESSION_API_KEYS_0": spec.session_api_key, "OH_SECRET_KEY": spec.secret_key},
        command=("--host", "0.0.0.0", "--port", str(settings.agent_port)),
        user=f"{settings.agent_uid}:{settings.agent_gid}",
        memory=settings.planner_memory,
        cpus=settings.planner_cpus,
        pids_limit=settings.agent_pids_limit,
    )


def _parse_inspect(raw: Mapping[str, object]) -> ContainerState:
    state = raw.get("State")
    config = raw.get("Config")
    state_d = state if isinstance(state, dict) else {}
    config_d = config if isinstance(config, dict) else {}
    labels = config_d.get("Labels") or {}
    return ContainerState(
        id=str(raw.get("Id", "")),
        name=str(raw.get("Name", "")).lstrip("/"),
        running=bool(state_d.get("Running", False)),
        status=str(state_d.get("Status", "unknown")),
        exit_code=int(state_d.get("ExitCode", 0) or 0),
        labels={str(k): str(v) for k, v in dict(labels).items()},
    )


class DockerRuntime:
    """RuntimePort over the docker CLI. The runner is injected so tests need no Docker."""

    def __init__(self, settings: Settings, runner: Runner = subprocess_runner) -> None:
        self._settings = settings
        self._run = runner

    def _docker(self, *args: str, timeout: float | None = 60.0) -> CommandResult:
        return self._run(["docker", *args], timeout=timeout)

    def inspect_container(self, name: str) -> ContainerState | None:
        res = self._docker("container", "inspect", name)
        if res.returncode != 0:
            if "no such" in (res.stderr + res.stdout).lower():
                return None
            raise DockerError(f"docker inspect {name}: {res.stderr.strip()}")
        items = json.loads(res.stdout or "[]")
        return _parse_inspect(items[0]) if items else None

    def ensure_container(self, spec: ContainerSpec) -> ContainerState:
        """Running container named `spec.name`: reuse, restart, or create it."""
        argv = docker_run_argv(spec)  # validate even when the container already exists
        current = self.inspect_container(spec.name)
        if current is not None and current.running:
            return current
        if current is not None:
            res = self._docker("start", spec.name)
            if res.returncode != 0:
                raise DockerError(f"docker start {spec.name}: {res.stderr.strip()}")
        else:
            res = self._run(argv, timeout=300.0)
            if res.returncode != 0:
                raise DockerError(f"docker run {spec.name}: {res.stderr.strip()}")
        started = self.inspect_container(spec.name)
        if started is None:
            raise DockerError(f"{spec.name} vanished right after start")
        return started

    def remove_container(self, name: str) -> bool:
        if self.inspect_container(name) is None:
            return False
        res = self._docker("rm", "-f", name, timeout=120.0)
        if res.returncode != 0:
            raise DockerError(f"docker rm {name}: {res.stderr.strip()}")
        return True

    def kill_container(self, name: str) -> bool:
        current = self.inspect_container(name)
        if current is None or not current.running:
            return False
        res = self._docker("kill", name)
        if res.returncode != 0:
            raise DockerError(f"docker kill {name}: {res.stderr.strip()}")
        return True

    def network_bridge(self, network: str) -> str | None:
        res = self._docker("network", "inspect", network)
        if res.returncode != 0:
            if "not found" in (res.stderr + res.stdout).lower():
                return None
            raise DockerError(f"docker network inspect {network}: {res.stderr.strip()}")
        items = json.loads(res.stdout or "[]")
        if not items:
            return None
        options = items[0].get("Options") or {}
        # Docker names the bridge br-<id prefix> unless the network sets a name.
        return str(options.get(BRIDGE_NAME_OPTION) or f"br-{str(items[0].get('Id', ''))[:12]}")

    def run_to_completion(self, spec: ContainerSpec, timeout_s: float) -> tuple[int, str]:
        argv = docker_run_argv(spec, detach=False)
        try:
            res = self._run(argv, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._docker("rm", "-f", spec.name)
            raise DockerError(f"{spec.name}: no result in {timeout_s:.0f}s") from None
        if res.returncode != 0 and not res.stdout.strip():
            raise DockerError(f"docker run {spec.name}: {res.stderr.strip()[-1500:]}")
        return res.returncode, res.stdout

    def container_logs(self, name: str, tail: int = 40) -> str:
        res = self._docker("logs", "--tail", str(tail), name)
        return (res.stdout + res.stderr).strip()

    def inspect(self, run_id: str) -> RuntimeSnapshot:
        res = self._docker("ps", "-a", "-q", "--filter", f"label={LABEL_RUN}={run_id}")
        if res.returncode != 0:
            raise DockerError(f"docker ps: {res.stderr.strip()}")
        states = []
        for cid in res.stdout.split():
            state = self.inspect_container(cid)
            if state is not None:
                states.append(state)
        return RuntimeSnapshot(run_id=run_id, containers=tuple(states))

    def ensure_workspace(self, spec: WorkspaceSpec) -> WorkspaceHandle:
        container = self.ensure_container(agent_container_spec(self._settings, spec))
        return WorkspaceHandle(
            container_id=container.id,
            running=container.running,
            url=f"http://{agent_container_name(spec.run_id)}:{self._settings.agent_port}",
        )

    # --- inside the sandbox --------------------------------------------------------

    def _sandbox(self, run_id: str, *args: str, timeout: float = 60.0) -> dict[str, Any]:
        """Run the in-sandbox helper as the sandbox user and parse its JSON answer."""
        uid, gid = self._settings.agent_uid, self._settings.agent_gid
        argv = [
            "docker", "exec", "--user", f"{uid}:{gid}",
            agent_container_name(run_id), *SANDBOX_HELPER, *args,
        ]  # fmt: skip
        try:
            res = self._run(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DockerError(f"sandbox {args[0]}: no answer in {timeout:.0f}s") from None
        if res.returncode != 0:
            detail = (res.stderr.strip() or res.stdout.strip())[-1500:]
            raise DockerError(f"sandbox {args[0]}: exit {res.returncode}: {detail}")
        try:
            out = json.loads(res.stdout)
        except ValueError:
            raise DockerError(f"sandbox {args[0]}: not JSON: {res.stdout[-500:]!r}") from None
        if not isinstance(out, dict):
            raise DockerError(f"sandbox {args[0]}: unexpected answer {out!r}")
        return out

    def _running(self, run_id: str) -> bool:
        state = self.inspect_container(agent_container_name(run_id))
        return state is not None and state.running

    def sandbox_processes(
        self, run_id: str, keep_session: int | None
    ) -> tuple[SandboxProcess, ...] | None:
        if not self._running(run_id):
            return None
        args = ["ps"] + (["--keep-sid", str(keep_session)] if keep_session else [])
        return _procs(self._sandbox(run_id, *args).get("processes"))

    def stop_agent(self, run_id: str, keep_session: int | None, grace_s: float) -> KillReport:
        if not self._running(run_id):
            return KillReport(container_running=False)
        args = ["kill-agent", "--grace", f"{grace_s:g}"]
        if keep_session:
            args += ["--keep-sid", str(keep_session)]
        out = self._sandbox(run_id, *args, timeout=grace_s + 60.0)
        return KillReport(
            container_running=True,
            before=_procs(out.get("before")),
            survivors=_procs(out.get("survivors")),
        )

    def restart_sandbox(self, run_id: str) -> None:
        name = agent_container_name(run_id)
        res = self._docker("restart", "--time", "10", name, timeout=120.0)
        if res.returncode != 0:
            raise DockerError(f"docker restart {name}: {res.stderr.strip()}")

    def ensure_demo(self, spec: DemoSpec) -> DemoHandle:
        args = ["demo-start", "--port", str(spec.port), "--command", spec.command]
        if spec.replace_session:
            args += ["--replace-sid", str(spec.replace_session)]
        out = self._sandbox(spec.run_id, *args, timeout=60.0)
        try:
            return DemoHandle(session_id=int(out["session_id"]))
        except (KeyError, TypeError, ValueError):
            raise DockerError(f"sandbox demo-start: unexpected answer {out!r}") from None

    def demo_status(self, run_id: str, session_id: int, port: int) -> DemoStatus:
        if not self._running(run_id):
            return DemoStatus(alive=False, listening=False)
        out = self._sandbox(
            run_id, "demo-status", "--sid", str(session_id), "--port", str(port), timeout=30.0
        )
        return DemoStatus(
            alive=bool(out.get("alive")),
            listening=bool(out.get("listening")),
            log_tail=str(out.get("log_tail") or ""),
        )


def _procs(raw: object) -> tuple[SandboxProcess, ...]:
    if not isinstance(raw, list):
        return ()
    out = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        out.append(
            SandboxProcess(
                pid=int(p.get("pid", 0)),
                ppid=int(p.get("ppid", 0)),
                sid=int(p.get("sid", 0)),
                state=str(p.get("state", "?")),
                cmd=str(p.get("cmd", "")),
                role=str(p.get("role", "agent")),
            )
        )
    return tuple(out)
