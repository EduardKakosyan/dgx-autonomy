"""PID 1 of the agent sandbox, and the controller's process tool inside it.

Stdlib only. The agent image copies this file root-owned and runs it with
`python3 -I` (isolated mode: no user site-packages, no PYTHON* variables), so the
agent, which runs as the same uid, can neither edit it nor hook its imports.

    supervise -- CMD...    PID 1. Starts the Agent Server (CMD) in its own session,
                           unless /dgx-control/mode says `demo-only`. Reaps orphans.
    ps                     JSON list of processes, each with a role.
    kill-agent             End every `agent` process; print what was there and what
                           survived.
    demo-start             Start the demo command in a new session (the `demo` group).
    demo-status            Is the demo session alive, and is its port listening?
    demo-stop              End one demo session.

Roles. The controller reaches the sandbox only through `docker exec`, and a process
created that way has parent pid 0 inside the container. That cannot be forged from
inside, and neither can a session id: setsid() only creates a new session, it
cannot join an existing one. So:

    pid 1                               supervisor
    ppid 0, or this helper's own chain  controller
    session == the recorded demo sid    demo
    state Z                             zombie (already dead, waiting to be reaped)
    anything else                       agent

"Anything else" includes processes that daemonized away from the Agent Server's
tree (tmux reparents itself to PID 1), which a process-tree kill would miss.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

CONTROL_DIR = "/dgx-control"
MODE_FILE = f"{CONTROL_DIR}/mode"
PROJECT_DIR = "/workspace/project"
STATE_DIR = "/workspace/.dgx"
DEMO_LOG = f"{STATE_DIR}/demo.log"
CMD_LIMIT = 300


def log(msg: str) -> None:
    print(f"dgx-sandbox: {msg}", file=sys.stderr, flush=True)


# --- /proc -------------------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    sid: int
    state: str
    cmd: str


def parse_stat(text: str) -> tuple[str, int, int]:
    """(state, ppid, sid) from /proc/<pid>/stat. The comm field may hold spaces or ')'."""
    rest = text[text.rindex(")") + 2 :].split()
    return rest[0], int(rest[1]), int(rest[3])


def read_proc(pid: int, root: str = "/proc") -> Proc | None:
    try:
        with open(f"{root}/{pid}/stat") as f:
            state, ppid, sid = parse_stat(f.read())
    except (OSError, ValueError, IndexError):
        return None  # gone, or not ours to read
    try:
        with open(f"{root}/{pid}/cmdline", "rb") as f:
            raw = f.read()
        cmd = raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        cmd = ""
    return Proc(pid, ppid, sid, state, cmd[:CMD_LIMIT])


def list_procs(root: str = "/proc") -> list[Proc]:
    out = []
    for name in os.listdir(root):
        if name.isdigit():
            p = read_proc(int(name), root)
            if p is not None:
                out.append(p)
    return sorted(out, key=lambda p: p.pid)


def own_chain(procs: Iterable[Proc], me: int) -> set[int]:
    """This process and its ancestors inside the container."""
    parents = {p.pid: p.ppid for p in procs}
    chain = set()
    pid = me
    while pid > 0 and pid not in chain:
        chain.add(pid)
        pid = parents.get(pid, 0)
    return chain


def role(p: Proc, *, chain: set[int], keep_sid: int | None) -> str:
    if p.pid == 1:
        return "supervisor"
    if p.pid in chain or p.ppid == 0:
        return "controller"
    if p.state == "Z":
        return "zombie"
    if keep_sid and p.sid == keep_sid:
        return "demo"
    return "agent"


def classify(procs: Sequence[Proc], *, me: int, keep_sid: int | None) -> list[tuple[Proc, str]]:
    chain = own_chain(procs, me)
    return [(p, role(p, chain=chain, keep_sid=keep_sid)) for p in procs]


def agent_procs(keep_sid: int | None, root: str = "/proc") -> list[Proc]:
    procs = list_procs(root)
    return [p for p, r in classify(procs, me=os.getpid(), keep_sid=keep_sid) if r == "agent"]


def _signal(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            log(f"cannot signal {pid}: {exc}")


def _view(p: Proc, r: str | None = None) -> dict[str, object]:
    d: dict[str, object] = asdict(p)
    if r is not None:
        d["role"] = r
    return d


# --- kill-agent ----------------------------------------------------------------------


def kill_agent(keep_sid: int | None, grace_s: float) -> dict[str, object]:
    """SIGTERM every agent process, wait up to `grace_s`, then freeze and SIGKILL.

    SIGSTOP before SIGKILL stops a forking process from spawning replacements
    between the listing and the kill. Several rounds catch late children.
    """
    before = agent_procs(keep_sid)
    _signal((p.pid for p in before), signal.SIGTERM)
    end = time.monotonic() + grace_s
    remaining = agent_procs(keep_sid)
    while remaining and time.monotonic() < end:
        time.sleep(0.2)
        remaining = agent_procs(keep_sid)
    rounds = 0
    while remaining and rounds < 10:
        rounds += 1
        pids = [p.pid for p in remaining]
        _signal(pids, signal.SIGSTOP)
        _signal(pids, signal.SIGKILL)
        time.sleep(0.3)
        remaining = agent_procs(keep_sid)
    return {
        "before": [_view(p) for p in before],
        "survivors": [_view(p) for p in remaining],
        "kill_rounds": rounds,
    }


# --- demo ----------------------------------------------------------------------------


def session_members(sid: int, root: str = "/proc") -> list[Proc]:
    return [p for p in list_procs(root) if p.sid == sid and p.state != "Z" and p.pid != 1]


def demo_stop(sid: int, grace_s: float = 5.0) -> int:
    members = session_members(sid)
    _signal((p.pid for p in members), signal.SIGTERM)
    end = time.monotonic() + grace_s
    while session_members(sid) and time.monotonic() < end:
        time.sleep(0.2)
    left = session_members(sid)
    _signal((p.pid for p in left), signal.SIGKILL)
    return len(members)


def demo_env(port: int) -> dict[str, str]:
    """The container environment minus the Agent Server's keys, plus PORT and HOST."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OH_")}
    env.update(PORT=str(port), HOST="0.0.0.0")
    return env


def demo_start(command: str, port: int, replace_sid: int | None) -> dict[str, object]:
    if replace_sid:
        demo_stop(replace_sid)
    os.makedirs(STATE_DIR, exist_ok=True)
    pid = os.fork()
    if pid == 0:  # the demo session leader
        try:
            os.setsid()
            fd = os.open(DEMO_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            os.write(fd, f"=== {stamp} demo on port {port}: {command}\n".encode())
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.chdir(PROJECT_DIR if os.path.isdir(PROJECT_DIR) else "/workspace")
            os.execve("/bin/sh", ["/bin/sh", "-c", command], demo_env(port))
        finally:
            os._exit(127)
    return {"session_id": pid}


def listening_ports(root: str = "/proc") -> set[int]:
    ports: set[int] = set()
    for table in ("net/tcp", "net/tcp6"):
        try:
            with open(f"{root}/{table}") as f:
                lines = f.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 3 and fields[3] == "0A":  # TCP_LISTEN
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def tail(path: str, limit: int = 2000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def demo_status(sid: int, port: int) -> dict[str, object]:
    return {
        "alive": bool(session_members(sid)),
        "listening": port in listening_ports(),
        "log_tail": tail(DEMO_LOG),
    }


# --- supervise -----------------------------------------------------------------------


def read_mode() -> str:
    try:
        with open(MODE_FILE) as f:
            return f.read().strip() or "agent"
    except OSError:
        return "agent"


def supervise(cmd: Sequence[str]) -> int:
    """PID 1: run the Agent Server in its own session and reap every orphan.

    The Agent Server exiting on its own ends the sandbox (the controller sees the
    container stop). After the controller switches the mode to `demo-only` and
    ends agent execution, the sandbox stays up for the demo, and a restarted
    sandbox never starts the Agent Server again.
    """
    terminating = False

    def on_term(signum: int, _frame: object) -> None:
        nonlocal terminating
        terminating = True
        # From PID 1, kill(-1) reaches every other process in the namespace.
        _signal([-1], signal.SIGTERM)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_term)

    agent: int | None = None
    # Keep the Popen referenced: its finalizer would otherwise try to reap the pid
    # that the loop below reaps with waitpid(-1).
    server: subprocess.Popen[bytes] | None = None
    mode = read_mode()
    if mode == "agent":
        server = subprocess.Popen(list(cmd), start_new_session=True)
        agent = server.pid
        log(f"started the Agent Server (pid {agent})")
    else:
        log(f"mode is {mode!r}: not starting the Agent Server")

    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            if terminating:
                return 0
            time.sleep(1.0)  # no children right now; orphans may arrive later
            continue
        if pid != agent:
            continue
        agent = None
        code = os.waitstatus_to_exitcode(status)
        if terminating:
            continue
        if read_mode() != "agent":
            log(f"Agent Server ended ({code}) after the controller stopped it; keeping the demo")
            continue
        log(f"Agent Server exited with {code}; ending the sandbox")
        on_term(signal.SIGTERM, None)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            time.sleep(0.1)
        return code if code > 0 else 1


# --- entry point ---------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dgx-sandbox")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("supervise")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("ps")
    s.add_argument("--keep-sid", type=int, default=None)
    s = sub.add_parser("kill-agent")
    s.add_argument("--keep-sid", type=int, default=None)
    s.add_argument("--grace", type=float, default=5.0)
    s = sub.add_parser("demo-start")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--command", required=True)
    s.add_argument("--replace-sid", type=int, default=None)
    s = sub.add_parser("demo-status")
    s.add_argument("--sid", type=int, required=True)
    s.add_argument("--port", type=int, required=True)
    s = sub.add_parser("demo-stop")
    s.add_argument("--sid", type=int, required=True)
    args = p.parse_args(argv)

    if args.cmd == "supervise":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            p.error("supervise needs a command")
        return supervise(command)
    if args.cmd == "ps":
        procs = list_procs()
        rows = classify(procs, me=os.getpid(), keep_sid=args.keep_sid)
        result: dict[str, object] = {"processes": [_view(pr, r) for pr, r in rows]}
    elif args.cmd == "kill-agent":
        result = kill_agent(args.keep_sid, args.grace)
    elif args.cmd == "demo-start":
        result = demo_start(args.command, args.port, args.replace_sid)
    elif args.cmd == "demo-status":
        result = demo_status(args.sid, args.port)
    else:
        result = {"signalled": demo_stop(args.sid)}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
