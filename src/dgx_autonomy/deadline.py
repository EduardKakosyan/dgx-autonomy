"""The deadline watchdog: ends agent execution at the fixed deadline.

It runs on a thread of its own and reads only SQLite and the injected clock. It never
waits for the reconcile loop, so an SDK call that hangs there (a stalled model
request, an Agent Server that stopped answering) cannot delay the stop. The expiry
callback persists the stop intent first and then ends agent execution; every call
it makes has a timeout.

Runs whose deadline passed while the controller was down are caught by the first
check after startup, before the reconcile loop could resume them.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .ports import Clock
from .state import Run, StateStore

log = logging.getLogger("dgx_autonomy.deadline")

# Phases in which the agent may still be executing. `stopping` is already ending.
_WATCHED_PHASES = frozenset({"launched", "running"})


class DeadlineWatchdog:
    def __init__(
        self,
        *,
        state: StateStore,
        clock: Clock,
        on_expired: Callable[[str], object],
        interval_s: float = 1.0,
    ) -> None:
        self._state = state
        self._clock = clock
        self._on_expired = on_expired
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def due(self) -> list[Run]:
        now = self._clock.now()
        return [
            r
            for r in self._state.list_runs()
            if r.phase in _WATCHED_PHASES and now >= r.deadline_at
        ]

    def check_once(self) -> list[str]:
        """Expire every run whose deadline has passed. Returns their ids."""
        expired = []
        for run in self.due():
            log.warning("%s: deadline %s reached; ending agent execution", run.id, run.deadline_at)
            try:
                self._on_expired(run.id)
            except Exception:
                # The stop intent is durable before anything else happens, so the
                # reconcile loop keeps retrying the stop itself.
                log.exception("%s: expiry handling failed", run.id)
            expired.append(run.id)
        return expired

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="deadline", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                log.exception("deadline check failed")
            self._stop.wait(self._interval_s)
