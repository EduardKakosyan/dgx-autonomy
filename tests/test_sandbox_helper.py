"""The in-sandbox helper's classification rules, against a fake /proc."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

HELPER = Path(__file__).resolve().parents[1] / "containers" / "sandbox" / "dgx_sandbox.py"


@pytest.fixture(scope="module")
def sb() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dgx_sandbox", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dgx_sandbox"] = module
    spec.loader.exec_module(module)
    return module


def _proc(root: Path, pid: int, ppid: int, sid: int, cmd: str, state: str = "S") -> None:
    d = root / str(pid)
    d.mkdir()
    comm = cmd.split()[0][-15:]
    # 52 fields; only state, ppid and session matter here. comm may hold ") (".
    fields = [state, str(ppid), str(pid), str(sid)] + ["0"] * 48
    (d / "stat").write_text(f"{pid} ({comm} ) (x) {' '.join(fields)}\n")
    (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")


def test_parse_stat_survives_parentheses_in_the_command_name(sb: ModuleType) -> None:
    assert sb.parse_stat("42 (a) b) (c) S 7 42 9 0 0\n") == ("S", 7, 9)


def test_roles(sb: ModuleType, tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 1, 0, 1, "python3 -I dgx_sandbox.py supervise")
    _proc(proc, 7, 1, 7, "/usr/local/bin/openhands-agent-server --port 8000")
    _proc(proc, 40, 1, 40, "tmux new-session -d")  # daemonized: reparented to PID 1
    _proc(proc, 41, 40, 41, "bash")
    _proc(proc, 42, 41, 41, "sleep 100000")
    _proc(proc, 50, 1, 50, "/bin/sh -c python3 -m http.server 3000")  # demo leader
    _proc(proc, 51, 50, 50, "python3 -m http.server 3000")
    _proc(proc, 60, 1, 60, "setsid sleep 1")  # the agent's own setsid: still agent
    _proc(proc, 70, 0, 70, "ps")  # another docker exec from the controller
    _proc(proc, 80, 0, 80, "python3 -I dgx_sandbox.py kill-agent")  # this helper
    _proc(proc, 81, 7, 7, "defunct", state="Z")

    procs = sb.list_procs(str(proc))
    roles = {p.pid: r for p, r in sb.classify(procs, me=80, keep_sid=50)}
    assert roles == {
        1: "supervisor",
        7: "agent",
        40: "agent",
        41: "agent",
        42: "agent",
        50: "demo",
        51: "demo",
        60: "agent",
        70: "controller",
        80: "controller",
        81: "zombie",
    }
    # Without a demo session everything but PID 1 and controller execs is agent.
    roles = {p.pid: r for p, r in sb.classify(procs, me=80, keep_sid=None)}
    assert roles[50] == roles[51] == "agent"


def test_listening_ports(sb: ModuleType, tmp_path: Path) -> None:
    net = tmp_path / "net"
    net.mkdir()
    header = "  sl  local_address rem_address   st tx_queue rx_queue\n"
    (net / "tcp").write_text(
        header
        + "   0: 00000000:0BB8 00000000:0000 0A 00000000:00000000\n"  # 3000 LISTEN
        + "   1: 0100007F:1F40 0100007F:D2F0 01 00000000:00000000\n"  # 8000 ESTABLISHED
    )
    (net / "tcp6").write_text(
        header + "   0: 00000000000000000000000000000000:1F40 000:0000 0A 0:0\n"  # 8000
    )
    assert sb.listening_ports(str(tmp_path)) == {3000, 8000}


def test_demo_env_drops_the_agent_server_keys(
    sb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OH_SESSION_API_KEYS_0", "secret")
    monkeypatch.setenv("OH_SECRET_KEY", "secret")
    monkeypatch.setenv("PATH", "/opt/node22/bin:/usr/bin")
    env = sb.demo_env(3000)
    assert not any(k.startswith("OH_") for k in env)
    assert env["PORT"] == "3000" and env["HOST"] == "0.0.0.0"
    assert env["PATH"] == "/opt/node22/bin:/usr/bin"


def _tcp(root: Path, rows: list[tuple[str, int, int]], table: str = "tcp") -> None:
    """rows: (local address hex, port, inode), all LISTEN."""
    net = root / "net"
    net.mkdir(exist_ok=True)
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when uid inode\n"
    lines = [
        f"   {i}: {addr}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
        f"  10001        0 {inode} 1 0000000000000000 100 0 0 10 0\n"
        for i, (addr, port, inode) in enumerate(rows)
    ]
    (net / table).write_text(header + "".join(lines))


def _fd(root: Path, pid: int, inode: int) -> None:
    fd = root / str(pid) / "fd"
    fd.mkdir(exist_ok=True)
    (fd / str(len(list(fd.iterdir())) + 3)).symlink_to(f"socket:[{inode}]")


def test_loopback_addresses(sb: ModuleType) -> None:
    assert sb._loopback("0100007F")  # 127.0.0.1
    assert not sb._loopback("00000000")  # 0.0.0.0
    assert not sb._loopback("0200A8C0")  # 192.168.0.2
    assert sb._loopback("00000000000000000000000001000000")  # ::1
    assert sb._loopback("0000000000000000FFFF00000100007F")  # ::ffff:127.0.0.1
    assert not sb._loopback("00000000000000000000000000000000")  # ::


def test_a_stray_loopback_server_on_the_demo_port_is_not_the_demo(
    sb: ModuleType, tmp_path: Path
) -> None:
    """hugo-dgx1: the agent's `http.server 3000 --bind 127.0.0.1` held the port."""
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 1, 0, 1, "python3 -I dgx_sandbox.py supervise")
    _proc(proc, 40, 1, 40, "python3 -m http.server 3000 --bind 127.0.0.1")  # agent's
    _proc(proc, 50, 1, 50, "/bin/sh -c python3 -m http.server 3000 --bind 0.0.0.0")
    _fd(proc, 40, 111)
    _tcp(proc, [("0100007F", 3000, 111)])

    status = sb.demo_status(50, 3000, str(proc))
    assert status["alive"] is True and status["listening"] is False
    assert "not the demo" in status["problem"] and "pid 40" in status["problem"]


def test_the_demo_on_all_addresses_is_listening(sb: ModuleType, tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 50, 1, 50, "/bin/sh -c pnpm start")
    _proc(proc, 51, 50, 50, "node server.js")
    _fd(proc, 51, 222)
    _tcp(proc, [], "tcp")
    _tcp(proc, [("00000000000000000000000000000000", 3000, 222)], "tcp6")

    status = sb.demo_status(50, 3000, str(proc))
    assert status == {"alive": True, "listening": True, "problem": "", "log_tail": ""}


def test_a_demo_on_loopback_only_is_not_reachable(sb: ModuleType, tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 50, 1, 50, "/bin/sh -c vite preview --port 3000")
    _fd(proc, 50, 333)
    _tcp(proc, [("0100007F", 3000, 333)])

    status = sb.demo_status(50, 3000, str(proc))
    assert status["listening"] is False and "loopback only" in status["problem"]


def test_free_port_ends_only_the_listeners(
    sb: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 1, 0, 1, "python3 -I dgx_sandbox.py supervise")
    _proc(proc, 40, 1, 40, "bash")
    _proc(proc, 41, 40, 40, "python3 -m http.server 3000")
    _proc(proc, 42, 40, 40, "node other.js")
    _fd(proc, 41, 111)
    _fd(proc, 42, 444)
    _tcp(proc, [("00000000", 3000, 111), ("00000000", 5173, 444)])
    sent: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if sig in (sb.signal.SIGTERM, sb.signal.SIGKILL):
            for p in (proc / str(pid)).rglob("*"):
                if p.is_symlink() or p.is_file():
                    p.unlink()

    monkeypatch.setattr(sb.os, "kill", kill)
    freed = sb.free_port(3000, grace_s=0.5, root=str(proc))
    assert [p["pid"] for p in freed] == [41]
    assert sent == [(41, sb.signal.SIGTERM)]
