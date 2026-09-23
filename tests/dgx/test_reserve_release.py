"""Reserve displaces claude-qwen and says so on its port; release puts it back exactly.

This stops a live service that other people may be using, so it only runs when
asked to:

    DGX_AUTONOMY_RESERVATION_TEST=1 ~/.local/bin/uv run pytest -m dgx \
        tests/dgx/test_reserve_release.py -s

It needs host/install.sh installed (sudo helper, notice unit) and no active run.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from dgx_autonomy.cli import reservation_helper

from dgx_helpers import wait_for

pytestmark = [
    pytest.mark.dgx,
    pytest.mark.skipif(
        os.environ.get("DGX_AUTONOMY_RESERVATION_TEST") != "1",
        reason="stops claude-qwen; set DGX_AUTONOMY_RESERVATION_TEST=1 to run",
    ),
]

SERVICE = "claude-qwen.service"
CLIENT = "http://127.0.0.1:8090"


def _systemctl(*args: str) -> str:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True).stdout


def _active() -> str:
    return _systemctl("show", SERVICE, "-p", "ActiveState", "--value").strip()


def _get(url: str, body: dict[str, Any] | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(resp.status), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode()
    except OSError as exc:
        return 0, str(exc)


def test_reserve_then_release(control: Callable[..., Any]) -> None:
    before_cat = _systemctl("cat", SERVICE)
    assert _active() == "active", "claude-qwen should be running before the test"
    assert reservation_helper("status")["reservation"] == "none"

    held = reservation_helper("reserve")
    try:
        assert held["reservation"] == "held"
        assert _active() == "inactive"
        assert control("inference.status")["reservation"] == "held"

        status, body = _get(f"{CLIENT}/v1/chat/completions", {"model": "qwen3.8-opus"})
        assert status == 503 and "autonomous coding environment" in body, (status, body)
        status, body = _get(f"{CLIENT}/v1/messages", {"model": "qwen3.8-opus"})
        assert status == 503 and "overloaded_error" in body, (status, body)
        assert "50-dgx-autonomy-reservation.conf" in _systemctl("cat", SERVICE)
    finally:
        control("inference.stop", timeout=300)
        released = reservation_helper("release")

    assert released["reservation"] == "released", released
    assert released["configuration_restored"] is True, released["mismatches"]
    assert _active() == "active"
    assert _systemctl("cat", SERVICE) == before_cat
    wait_for(lambda: _get(f"{CLIENT}/health")[0], lambda s: s == 200, timeout_s=600)
    assert reservation_helper("status")["reservation"] == "none"
