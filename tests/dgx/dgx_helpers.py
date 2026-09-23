"""Polling helper shared by the target-host tests."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def wait_for(
    probe: Callable[[], Any], done: Callable[[Any], bool], *, timeout_s: float, every_s: float = 10
) -> Any:
    deadline = time.monotonic() + timeout_s
    last = probe()
    while not done(last):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s:.0f}s; last observation: {last}")
        time.sleep(every_s)
        last = probe()
    return last
