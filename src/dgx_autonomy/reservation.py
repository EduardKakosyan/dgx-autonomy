#!/usr/bin/python3 -I
"""The inference reservation: displace `claude-qwen` for the environment, then give it back.

This file is two things. In the package it is importable (and unit-tested) as
`dgx_autonomy.reservation`. On the DGX the operator installs a root-owned copy as
`/usr/local/sbin/dgx-autonomy-reservation`, and a sudoers entry lets jim run exactly
`reserve`, `release` and `status` (host/sudoers-autonomy). So it imports nothing
but the standard library, takes no options, and runs isolated (`-I`).

The competing service's files are never edited. `reserve` records them, adds one
drop-in that keeps the unit from starting while the reservation record exists, and
stops the service. A small listener (dgx-autonomy-busy-notice.service) then answers
the service's client port with "in use by the autonomous coding environment". The
drop-in outlives a reboot, so a restarted DGX does not bring claude-qwen back.

`release` refuses while the memory claude-qwen needs is not available (retained
demos may hold it). Otherwise it stops the notice, removes the drop-in, checks that
the unit's effective configuration is byte-for-byte what `reserve` recorded,
restores the prior enabled state, and starts the service if it was running before.

Output is one JSON object on stdout. Exit 0: done (or nothing to do); 2: refused,
nothing changed; 1: failed part-way (the record stays, run `release` again).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVICE = "claude-qwen.service"
NOTICE_SERVICE = "dgx-autonomy-busy-notice.service"
UNIT_DIR = Path("/etc/systemd/system")
DROPIN_NAME = "50-dgx-autonomy-reservation.conf"
RECORD_DIR = Path("/var/lib/dgx-autonomy/reservation")
RECORD_NAME = "record.json"
NOTICE_ENV_NAME = "notice.env"
# Kept free on top of what claude-qwen used, so restoring it does not push the DGX
# into swap next to retained demos.
HEADROOM_BYTES = 8 * 1024**3
# The DGX's unified memory takes a moment to show a stopped llama-server's pages.
SETTLE_S = 5.0
START_TIMEOUT_S = 60.0
HEALTH_TIMEOUT_S = 300.0

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
GIB = 1024**3


class ReservationError(RuntimeError):
    """A step failed after something changed; the record says where things stand."""


class Refused(RuntimeError):
    """Nothing was changed; the message says why."""


# --- the host, as far as this helper touches it ------------------------------------


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], Result]


def _subprocess_runner(argv: Sequence[str]) -> Result:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=120)
    return Result(proc.returncode, proc.stdout, proc.stderr)


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * (1024 if parts[1:] == ["kB"] else 1)
    return out


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return 0


@dataclass
class Host:
    """Everything that touches the machine. Tests swap in a tmp root and a fake systemd."""

    root: Path = Path("/")
    run: Runner = _subprocess_runner
    meminfo: Callable[[], Mapping[str, int]] = _meminfo
    http_status: Callable[[str], int] = _http_status
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def path(self, p: Path) -> Path:
        return self.root / p.relative_to("/")

    def systemctl(self, *args: str, check: bool = True) -> Result:
        res = self.run(["systemctl", *args])
        if check and res.returncode != 0:
            raise ReservationError(f"systemctl {' '.join(args)}: {res.stderr.strip()}")
        return res

    def mem_available(self) -> int:
        return int(self.meminfo().get("MemAvailable", 0))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# --- what `reserve` records ----------------------------------------------------------


@dataclass
class Record:
    """The reservation, and everything release needs to put claude-qwen back."""

    state: str  # reserving | held | releasing
    held_since: str
    service: str
    prior_unit_path: str
    prior_unit_sha: str
    prior_dropins: dict[str, str]  # path -> sha256, the unit's drop-ins before reserve
    prior_dropin_dir_existed: bool
    prior_effective_sha: str  # sha256 of `systemctl cat`: unit plus drop-ins
    prior_enabled: str  # enabled | disabled | ...
    prior_active: bool
    notice_host: str
    notice_port: int
    # MemAvailable gained by stopping the service (or its weights, if larger), plus
    # headroom: what release needs before starting it again.
    freed_bytes: int = 0
    required_available_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str) -> Record:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ReservationError("reservation record is not a JSON object")
        return cls(**raw)


@dataclass(frozen=True)
class UnitSnapshot:
    unit_path: str
    unit_text: str
    dropins: dict[str, str]  # path -> text
    dropin_dir_existed: bool
    effective: str  # `systemctl cat` output
    enabled: str
    active: bool
    exec_start: str


class Reservation:
    def __init__(self, host: Host | None = None, *, service: str = SERVICE) -> None:
        self.host = host or Host()
        self.service = service

    # --- paths -----------------------------------------------------------------------

    @property
    def record_dir(self) -> Path:
        return self.host.path(RECORD_DIR)

    @property
    def record_path(self) -> Path:
        return self.record_dir / RECORD_NAME

    @property
    def dropin_dir(self) -> Path:
        return self.host.path(UNIT_DIR / f"{self.service}.d")

    @property
    def dropin_path(self) -> Path:
        return self.dropin_dir / DROPIN_NAME

    def read_record(self) -> Record | None:
        try:
            return Record.from_json(self.record_path.read_text())
        except FileNotFoundError:
            return None

    def _save(self, record: Record) -> None:
        self.record_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write(self.record_path, json.dumps(asdict(record), indent=2) + "\n")

    # --- inspection --------------------------------------------------------------------

    def _show(self) -> dict[str, str]:
        props = "FragmentPath,DropInPaths,UnitFileState,ActiveState,ExecStart,LoadState"
        res = self.host.systemctl("show", self.service, "-p", props)
        out = {}
        for line in res.stdout.splitlines():
            key, _, value = line.partition("=")
            out[key] = value
        return out

    def snapshot(self) -> UnitSnapshot:
        show = self._show()
        if show.get("LoadState") != "loaded" or not show.get("FragmentPath"):
            state = show.get("LoadState")
            raise Refused(f"{self.service} is not a loaded unit (LoadState={state})")
        unit_path = show["FragmentPath"]
        # Our own drop-in is never part of the "prior" configuration.
        dropins = {
            p: self.host.path(Path(p)).read_text()
            for p in show.get("DropInPaths", "").split()
            if not p.endswith(f"/{DROPIN_NAME}")
        }
        return UnitSnapshot(
            unit_path=unit_path,
            unit_text=self.host.path(Path(unit_path)).read_text(),
            dropins=dropins,
            dropin_dir_existed=self.dropin_dir.is_dir(),
            effective=self.host.systemctl("cat", self.service).stdout,
            enabled=show.get("UnitFileState", ""),
            active=show.get("ActiveState") in ("active", "activating", "reloading"),
            exec_start=show.get("ExecStart", ""),
        )

    # --- reserve -------------------------------------------------------------------------

    def reserve(self) -> dict[str, Any]:
        existing = self.read_record()
        if existing is not None and existing.state == "held":
            return {"reservation": "held", "already": True, "record": asdict(existing)}
        if existing is not None and existing.state == "releasing":
            raise Refused("a release is half done; run `release` again first")

        snap = self.snapshot()
        host, port = client_address(snap.unit_text)
        weights = _weights_bytes(self.host, snap.unit_text)
        before = self.host.mem_available()
        now = self.host.now().isoformat(timespec="seconds")
        record = existing or Record(
            state="reserving",
            held_since=now,
            service=self.service,
            prior_unit_path=snap.unit_path,
            prior_unit_sha=_sha(snap.unit_text),
            prior_dropins={p: _sha(t) for p, t in snap.dropins.items()},
            prior_dropin_dir_existed=snap.dropin_dir_existed,
            prior_effective_sha=_sha(snap.effective),
            prior_enabled=snap.enabled,
            prior_active=snap.active,
            notice_host=host,
            notice_port=port,
        )
        # The record is written before anything changes: the drop-in's condition and
        # the notice's condition both point at it, and a crash can be released. A
        # half-done reserve keeps its first record; the unit may already carry our
        # drop-in or be stopped, so it is not "prior" any more.
        self._save(record)
        if existing is None:
            snapshot_dir = self.record_dir / "prior"
            snapshot_dir.mkdir(mode=0o755, exist_ok=True)
            _write(snapshot_dir / "unit", snap.unit_text)
            _write(snapshot_dir / "systemctl-cat", snap.effective)
        _write(
            self.record_dir / NOTICE_ENV_NAME,
            f"NOTICE_HOST={record.notice_host}\nNOTICE_PORT={record.notice_port}\n",
        )

        self.dropin_dir.mkdir(mode=0o755, exist_ok=True)
        _write(self.dropin_path, dropin_text(self.service))
        self.host.systemctl("daemon-reload")
        if snap.active:
            self.host.systemctl("stop", self.service)
            self.host.sleep(SETTLE_S)
        after = self.host.mem_available()
        freed = max(after - before, 0)
        if record.required_available_bytes == 0:
            record.freed_bytes = max(freed, weights)
            record.required_available_bytes = record.freed_bytes + HEADROOM_BYTES
            if freed < weights:
                record.notes.append(
                    f"stopping freed {freed / GIB:.1f} GiB; using the weights' "
                    f"{weights / GIB:.1f} GiB as the service's footprint"
                )
            self._save(record)  # before anything else can fail: the measurement is gone
        self._wait_inactive()
        self.host.systemctl("start", NOTICE_SERVICE)
        record.state = "held"
        self._save(record)
        return {
            "reservation": "held",
            "already": False,
            "notice": f"http://{record.notice_host}:{record.notice_port}/",
            "record": asdict(record),
        }

    def _wait_inactive(self) -> None:
        end = self.host.monotonic() + START_TIMEOUT_S
        while self._show().get("ActiveState") not in ("inactive", "failed"):
            if self.host.monotonic() > end:
                raise ReservationError(f"{self.service} did not stop")
            self.host.sleep(1.0)

    # --- release -------------------------------------------------------------------------

    def release(self) -> dict[str, Any]:
        record = self.read_record()
        if record is None:
            return {"reservation": "none", "already": True}
        available = self.host.mem_available()
        if available < record.required_available_bytes:
            raise Refused(
                f"{self.service} needs {record.required_available_bytes / GIB:.1f} GiB available"
                f" ({record.freed_bytes / GIB:.1f} GiB it used plus"
                f" {HEADROOM_BYTES / GIB:.0f} GiB headroom); only {available / GIB:.1f} GiB is."
                " Retained demos and the environment's llama-server hold memory; stop what you"
                " can spare and run release again. The reservation is still held."
            )
        record.state = "releasing"
        self._save(record)

        self.host.systemctl("stop", NOTICE_SERVICE)
        if self.dropin_path.exists():
            self.dropin_path.unlink()
        if (
            not record.prior_dropin_dir_existed
            and self.dropin_dir.is_dir()
            and not any(self.dropin_dir.iterdir())
        ):
            self.dropin_dir.rmdir()
        self.host.systemctl("daemon-reload")

        snap = self.snapshot()
        mismatches = self._compare(record, snap)
        if snap.enabled != record.prior_enabled and record.prior_enabled in ("enabled", "disabled"):
            verb = "enable" if record.prior_enabled == "enabled" else "disable"
            self.host.systemctl(verb, self.service)
        health = None
        if record.prior_active:
            self.host.systemctl("start", self.service)
            health = self._wait_healthy(record)

        self._archive(record)
        return {
            "reservation": "released",
            "already": False,
            "service_active": self._show().get("ActiveState"),
            "health": health,
            "configuration_restored": not mismatches,
            "mismatches": mismatches,
        }

    def _compare(self, record: Record, snap: UnitSnapshot) -> list[str]:
        """What differs from the recorded prior configuration. Never overwritten here:
        someone else changed it during the reservation, and that is theirs to keep."""
        out = []
        if snap.unit_path != record.prior_unit_path:
            out.append(f"unit moved: {record.prior_unit_path} -> {snap.unit_path}")
        if _sha(snap.unit_text) != record.prior_unit_sha:
            out.append(f"{snap.unit_path} changed during the reservation")
        now_dropins = {p: _sha(t) for p, t in snap.dropins.items()}
        if now_dropins != record.prior_dropins:
            out.append(f"drop-ins differ: {sorted(now_dropins)} vs {sorted(record.prior_dropins)}")
        if _sha(snap.effective) != record.prior_effective_sha:
            out.append("`systemctl cat` differs from the recorded output")
        return out

    def _wait_healthy(self, record: Record) -> str:
        host = "127.0.0.1" if record.notice_host in ("0.0.0.0", "") else record.notice_host
        url = f"http://{host}:{record.notice_port}/health"
        end = self.host.monotonic() + HEALTH_TIMEOUT_S
        while True:
            status = self.host.http_status(url)
            if status == 200:
                return "ready"
            if self.host.monotonic() > end:
                return f"not ready after {HEALTH_TIMEOUT_S:.0f}s (last HTTP {status})"
            self.host.sleep(3.0)

    def _archive(self, record: Record) -> None:
        released = self.record_dir / "released"
        released.mkdir(mode=0o755, exist_ok=True)
        stamp = self.host.now().strftime("%Y%m%dT%H%M%SZ")
        _write(released / f"{stamp}.json", json.dumps(asdict(record), indent=2) + "\n")
        self.record_path.unlink()
        (self.record_dir / NOTICE_ENV_NAME).unlink(missing_ok=True)

    # --- status --------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        record = self.read_record()
        show = self._show()
        notice = self.host.systemctl("is-active", NOTICE_SERVICE, check=False).stdout.strip()
        return {
            "reservation": record.state if record else "none",
            "service": self.service,
            "service_active": show.get("ActiveState"),
            "notice_active": notice,
            "mem_available_gib": round(self.host.mem_available() / GIB, 1),
            "record": asdict(record) if record else None,
        }


def dropin_text(service: str) -> str:
    return (
        "# Written by dgx-autonomy-reservation. The DGX inference is reserved for the\n"
        f"# autonomous coding environment; while {RECORD_DIR / RECORD_NAME} exists,\n"
        f"# {service} does not start (not by hand, not at boot).\n"
        "# `dgx-autonomy release` removes this file and starts the service again.\n"
        "[Unit]\n"
        f"ConditionPathExists=!{RECORD_DIR / RECORD_NAME}\n"
    )


def _exec_args(unit_text: str) -> list[str]:
    """ExecStart= of a unit file, joined across backslash continuations."""
    joined = unit_text.replace("\\\n", " ")
    for line in joined.splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "ExecStart" and value.strip():
            return shlex.split(value.strip().lstrip("-@:+!"))
    return []


def _flag(args: Sequence[str], name: str) -> str | None:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return None


def client_address(unit_text: str) -> tuple[str, int]:
    """Where the service's clients connect: llama-server's --host and --port."""
    args = _exec_args(unit_text)
    port = _flag(args, "--port")
    if port is None or not port.isdigit():
        raise Refused("cannot find --port in the service's ExecStart; not reserving blind")
    host = _flag(args, "--host") or "127.0.0.1"
    return host, int(port)


def _weights_bytes(host: Host, unit_text: str) -> int:
    args = _exec_args(unit_text)
    total = 0
    for flag in ("--model", "-m", "--mmproj"):
        value = _flag(args, flag)
        if value:
            with contextlib.suppress(OSError, ValueError):
                total += host.path(Path(value)).stat().st_size
    return total


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("reserve", "release", "status"):
        print("usage: dgx-autonomy-reservation reserve|release|status", file=sys.stderr)
        return EXIT_REFUSED
    reservation = Reservation()
    try:
        out = getattr(reservation, args[0])()
        code = EXIT_OK
    except Refused as exc:
        out, code = {"refused": str(exc)}, EXIT_REFUSED
    except (ReservationError, OSError, ValueError) as exc:
        out, code = {"error": f"{type(exc).__name__}: {exc}"}, EXIT_FAILED
    print(json.dumps(out, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
