"""docker CLI adapter behind RuntimePort.

Only the controller container holds the Docker socket. Every container it creates
goes through `docker_run_argv`, which refuses a socket mount and always adds the
hardening flags. The agent container is built here directly rather than through
OpenHands `DockerWorkspace`, which adds no `--user`, `--cap-drop` or security options.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .config import Settings
from .ports import (
    ContainerSpec,
    ContainerState,
    Mount,
    RuntimeSnapshot,
    WorkspaceHandle,
    WorkspaceSpec,
)

LABEL_PREFIX = "dgx-autonomy"
LABEL_RUN = f"{LABEL_PREFIX}.run"
LABEL_OP = f"{LABEL_PREFIX}.op"
LABEL_ROLE = f"{LABEL_PREFIX}.role"

FORBIDDEN_MOUNT_SOURCES = frozenset({"/var/run/docker.sock", "/run/docker.sock"})

AGENT_WORKDIR = "/workspace"
AGENT_PROJECT_DIR = f"{AGENT_WORKDIR}/project"
AGENT_BRIEF_PATH = "/brief/brief.md"


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


def _mount_arg(m: Mount) -> str:
    arg = f"type=bind,source={m.source},target={m.target}"
    return f"{arg},readonly" if m.read_only else arg


def docker_run_argv(spec: ContainerSpec) -> list[str]:
    """The exact argv for `docker run -d`. Pure, so tests can assert on it."""
    _check_spec(spec)
    argv = ["docker", "run", "-d", "--name", spec.name]
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
    for m in spec.mounts:
        argv += ["--mount", _mount_arg(m)]
    for key, value in sorted(spec.env.items()):
        argv += ["--env", f"{key}={value}"]
    argv.append(spec.image)
    argv += list(spec.command)
    return argv


def agent_container_name(run_id: str) -> str:
    return f"{LABEL_PREFIX}-agent-{run_id}"


def agent_container_spec(settings: Settings, spec: WorkspaceSpec) -> ContainerSpec:
    """The unprivileged sandbox that runs the Agent Server and its tools.

    It sees its own workspace (project + SDK conversation storage) and a read-only
    brief. It gets the private inference network and an egress network, but no
    Docker socket, no controller state and no published ports.
    """
    return ContainerSpec(
        name=agent_container_name(spec.run_id),
        image=settings.agent_image,
        labels={LABEL_RUN: spec.run_id, LABEL_OP: spec.op_id, LABEL_ROLE: "agent"},
        networks=(settings.internal_network, settings.egress_network),
        mounts=(
            Mount(str(spec.agent_dir), AGENT_WORKDIR),
            Mount(str(spec.brief_file), AGENT_BRIEF_PATH, read_only=True),
        ),
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
