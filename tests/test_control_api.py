from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy.cli import main
from dgx_autonomy.control_api import ControlError, ControlServer, call
from dgx_autonomy.controller import RequestError


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    # Unix socket paths are limited to ~104 bytes on macOS; pytest's tmp_path is longer.
    d = Path(tempfile.mkdtemp(prefix="dgxa-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _handler(op: str, args: Mapping[str, Any]) -> Any:
    if op == "echo":
        return {"args": dict(args)}
    if op == "status":
        return {"run_id": "r1", "phase": "running", "operations": [], "last_event": None}
    if op == "boom":
        raise RuntimeError("kaput")
    raise RequestError(f"unknown operation {op!r}")


@pytest.fixture
def server(sock_dir: Path) -> Iterator[ControlServer]:
    srv = ControlServer(sock_dir / "control" / "control.sock", _handler)
    srv.start()
    yield srv
    srv.shutdown()


def test_round_trip(server: ControlServer) -> None:
    assert call(server.path, "echo", {"a": 1}) == {"args": {"a": 1}}


def test_socket_is_private_to_its_owner(server: ControlServer) -> None:
    mode = stat.S_IMODE(server.path.stat().st_mode)
    assert mode == 0o600
    assert stat.S_ISSOCK(server.path.stat().st_mode)


def test_request_errors_come_back_as_messages(server: ControlServer) -> None:
    with pytest.raises(ControlError, match="unknown operation 'nope'"):
        call(server.path, "nope")


def test_internal_errors_do_not_kill_the_server(server: ControlServer) -> None:
    with pytest.raises(ControlError, match="internal error: RuntimeError: kaput"):
        call(server.path, "boom")
    assert call(server.path, "echo") == {"args": {}}


def test_missing_socket_is_explained(sock_dir: Path) -> None:
    with pytest.raises(ControlError, match="is the controller running"):
        call(sock_dir / "absent.sock", "status")


def test_cli_status_talks_to_the_socket(
    server: ControlServer, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--socket", str(server.path), "status"]) == 0
    out = capsys.readouterr().out
    assert "run_id" in out and "running" in out


def test_cli_reports_controller_errors(sock_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--socket", str(sock_dir / "absent.sock"), "status"]) == 1
    assert "is the controller running" in capsys.readouterr().err


def test_cli_rejects_budgets_over_forty_hours(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("x")
    with pytest.raises(SystemExit):
        main(["launch", "--brief", str(brief), "--budget-hours", "41"])
