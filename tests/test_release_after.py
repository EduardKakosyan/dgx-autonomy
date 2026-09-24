"""`release --after RUN_ID`: claude-qwen comes back when the run ends, not when
someone remembers (hugo-dgx1: it stayed displaced for 12 hours after a run)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy import cli
from dgx_autonomy.control_api import ControlError


def _args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(socket=None, json=False, after="r1", detach=False, **kw)


def test_it_waits_for_the_run_to_end_then_releases_and_retries_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phases = iter(["running", "running", "stopping", "finished", "finished"])
    calls: list[str] = []

    def fake_call(_socket: Path, op: str, args: dict[str, Any] | None = None, **_: Any) -> Any:
        calls.append(op)
        if op == "status":
            phase = next(phases)
            if phase == "stopping" and "boom" not in calls:
                calls.append("boom")
                raise ControlError("controller restarting")
            return {"phase": phase}
        return {"removed": True}

    helper_answers = iter([cli.HelperError("not enough memory", cli.HELPER_REFUSED), None])

    def fake_helper(action: str, runner: Any = None) -> dict[str, Any]:
        answer = next(helper_answers)
        if isinstance(answer, Exception):
            raise answer
        return {"configuration_restored": True, "service_active": "active", "health": "ok"}

    slept: list[float] = []
    monkeypatch.setattr(cli, "call", fake_call)
    monkeypatch.setattr(cli, "reservation_helper", fake_helper)
    assert cli._release_after(_args(), sleep=slept.append) == 0
    # Nothing is released while the run is active or unreadable.
    assert calls[:4] == ["status", "status", "status", "boom"]
    assert calls.count("inference.stop") == 2  # refused once, then released
    assert slept == [cli.RELEASE_POLL_S] * 3 + [cli.RELEASE_RETRY_S]


def test_detach_installs_a_lingering_user_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    ran: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert cli._release_detached("r1", runner) == 0
    unit = (tmp_path / ".config/systemd/user" / cli.RELEASE_UNIT).read_text()
    assert "ExecStart=%h/.local/bin/dgx-autonomy release --after %i" in unit
    assert "WantedBy=default.target" in unit  # a DGX restart starts the wait again
    assert ran[-1] == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "dgx-autonomy-release-after@r1.service",
    ]
