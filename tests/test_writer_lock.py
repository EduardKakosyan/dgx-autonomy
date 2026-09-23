"""Only one controller operates the state store.

The lock is an flock next to the database (released by the kernel however the
holder dies) plus a lock row that records the holder and fences every write.
"""

from __future__ import annotations

import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dgx_autonomy.state import StateStore, WriterLockError

from fakes import BOOT_ID, Harness

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
DB = Path("state") / "controller.sqlite3"


def _acquire(store: StateStore, pid: int = 7, boot_id: str = BOOT_ID, at: datetime = NOW) -> None:
    store.acquire_writer(pid=pid, host="controller-1", boot_id=boot_id, now=at)


def _launch(store: StateStore, run_id: str = "r1") -> None:
    store.create_run(
        run_id=run_id,
        model_key="m",
        launched_at=NOW,
        deadline_at=NOW + timedelta(hours=1),
        brief_path="b",
    )


def test_a_second_controller_refuses_to_operate(tmp_path: Path) -> None:
    first = StateStore(tmp_path / DB)
    _acquire(first, pid=7)
    second = StateStore(tmp_path / DB)
    with pytest.raises(WriterLockError, match=r"another controller is running .*pid 7"):
        _acquire(second, pid=8)
    # The refused controller changed nothing: the first one still holds the lock.
    holder = first.writer_holder()
    assert holder is not None and holder.pid == 7
    _launch(first)


def test_the_lock_file_is_private(tmp_path: Path) -> None:
    store = StateStore(tmp_path / DB)
    _acquire(store)
    assert store.lock_path == tmp_path / "state" / "controller.lock"
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_a_restarted_controller_takes_over_at_once(tmp_path: Path) -> None:
    dead = StateStore(tmp_path / DB)
    _acquire(dead, pid=7)
    _launch(dead)
    dead.close()  # the process died: its descriptor, and so the flock, is gone

    restarted = StateStore(tmp_path / DB)
    previous = restarted.acquire_writer(
        pid=7, host="controller-1", boot_id=BOOT_ID, now=NOW + timedelta(minutes=1)
    )
    assert previous is not None and previous.pid == 7 and previous.acquired_at == NOW
    holder = restarted.writer_holder()
    assert holder is not None and holder.token != previous.token
    restarted.set_phase("r1", "running")


def test_a_controller_that_lost_the_lock_cannot_write(tmp_path: Path) -> None:
    stale = StateStore(tmp_path / DB)
    _acquire(stale, pid=7)
    _launch(stale)
    # Its flock is gone (as if its lock file had been swapped under it) and another
    # controller took over. The stale one must not change lifecycle state any more.
    stale._release_file_lock()
    current = StateStore(tmp_path / DB)
    _acquire(current, pid=9)

    with pytest.raises(WriterLockError, match=r"lost the writer lock .*pid 9"):
        stale.set_phase("r1", "running")
    with pytest.raises(WriterLockError):
        stale.request_stop("r1", "stopped")
    run = current.get_run("r1")
    assert run is not None and run.phase == "launched" and not run.stop_requested
    # Reading is still fine.
    assert stale.get_run("r1") is not None
    current.set_phase("r1", "running")


def test_a_store_that_never_acquired_the_lock_is_not_fenced(tmp_path: Path) -> None:
    store = StateStore(tmp_path / DB)
    _launch(store)
    assert store.writer_holder() is None


def test_the_controller_records_itself_and_says_why_it_took_over(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h = harness
    h.state.acquire_writer(pid=1, host="old", boot_id="an-earlier-boot", now=NOW)
    with caplog.at_level(logging.WARNING):
        h.controller.acquire_writer()
    assert "taken over from pid 1 on old" in caplog.text
    assert "the DGX restarted since" in caplog.text

    ping = h.controller.handle("ping", {})
    assert ping["pong"] is True
    assert ping["controller"]["boot_id"] == BOOT_ID

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        h.restart_controller().acquire_writer()
    assert "the controller restarted" in caplog.text
    assert h.controller.handle("ping", {})["controller"]["token"] != ping["controller"]["token"]
