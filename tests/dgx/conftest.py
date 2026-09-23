"""Target-host smoke tests. Run on hugo-dgx1 as the operator:

    cd ~/dgx-autonomy && ~/.local/bin/uv run pytest -m dgx tests/dgx

They drive the real controller through its control socket, exactly as the CLI does,
so they need the controller running (README, privileged setup) and the model files
downloaded. They are deselected in CI with `-m "not dgx"`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy.config import default_socket_path
from dgx_autonomy.control_api import ControlError, call


@pytest.fixture(scope="session")
def control() -> Callable[..., Any]:
    path = Path(os.environ.get("DGX_AUTONOMY_SOCKET") or default_socket_path())
    try:
        call(path, "ping", timeout=10)
    except ControlError as exc:
        pytest.fail(f"controller not reachable: {exc}")

    def _call(op: str, args: dict[str, Any] | None = None, timeout: float = 600.0) -> Any:
        return call(path, op, args or {}, timeout=timeout)

    return _call
