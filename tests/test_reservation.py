"""The reservation helper against a fake systemd and a tmp root filesystem."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dgx_autonomy import reservation as rs
from dgx_autonomy.reservation import (
    DROPIN_NAME,
    HEADROOM_BYTES,
    NOTICE_SERVICE,
    SERVICE,
    Host,
    Refused,
    Reservation,
    Result,
)

GIB = 1024**3

# claude-qwen.service as found on hugo-dgx1 (read-only inspection, Phase 3).
UNIT = """\
[Unit]
Description=Claude Qwen llama.cpp inference server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=jim
WorkingDirectory=/home/jim

ExecStart=/home/jim/src/llama.cpp-claude/build/bin/llama-server \\
  --model /home/jim/models/qwen3.8-27b/Qwen3.8-27B-Q8_0.gguf \\
  --mmproj /home/jim/models/qwen3.8-27b/mmproj-Qwen3.8-27B-Q8_0.gguf \\
  --alias qwen3.8-opus \\
  --host 127.0.0.1 \\
  --port 8090 \\
  --ctx-size 262144 \\
  --parallel 1 \\
  --n-gpu-layers all \\
  --jinja \\
  --api-key-file /home/jim/.config/llama-claude/api-key \\
  --no-ui

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
UNIT_PATH = "/etc/systemd/system/claude-qwen.service"
DROPIN_DIR = "/etc/systemd/system/claude-qwen.service.d"


class FakeSystemd:
    """systemctl for one service and the notice, with conditions and memory."""

    def __init__(self, root: Path, *, active: bool = True, footprint: int = 37 * GIB) -> None:
        self.root = root
        self.active = active
        self.enabled = "enabled"
        self.notice_active = False
        self.footprint = footprint
        self.mem = 40 * GIB  # MemAvailable while claude-qwen runs
        self.calls: list[list[str]] = []
        self.skipped_starts = 0

    def path(self, p: str) -> Path:
        return self.root / p.lstrip("/")

    def dropins(self) -> list[str]:
        d = self.path(DROPIN_DIR)
        return sorted(f"{DROPIN_DIR}/{f.name}" for f in d.glob("*.conf")) if d.is_dir() else []

    def condition_blocks(self) -> bool:
        for p in self.dropins():
            for line in self.path(p).read_text().splitlines():
                if (
                    line.startswith("ConditionPathExists=!")
                    and self.path(line.split("!", 1)[1]).exists()
                ):
                    return True
        return False

    def cat(self) -> str:
        out = f"# {UNIT_PATH}\n{self.path(UNIT_PATH).read_text()}"
        for p in self.dropins():
            out += f"\n# {p}\n{self.path(p).read_text()}"
        return out

    def __call__(self, argv: Sequence[str]) -> Result:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == "systemctl"
        verb, unit = argv[1], argv[2] if len(argv) > 2 else None
        if verb == "show":
            state = "active" if self.active else "inactive"
            return Result(
                0,
                f"FragmentPath={UNIT_PATH}\nDropInPaths={' '.join(self.dropins())}\n"
                f"UnitFileState={self.enabled}\nActiveState={state}\nLoadState=loaded\n"
                "ExecStart={ path=/home/jim/... }\n",
                "",
            )
        if verb == "cat":
            return Result(0, self.cat(), "")
        if verb == "daemon-reload":
            return Result(0, "", "")
        if verb == "is-active":
            return Result(0, "active\n" if self.notice_active else "inactive\n", "")
        if unit == NOTICE_SERVICE:
            self.notice_active = verb == "start"
            return Result(0, "", "")
        if verb == "stop":
            if self.active:
                self.active = False
                self.mem += self.footprint
            return Result(0, "", "")
        if verb == "start":
            if self.condition_blocks():
                self.skipped_starts += 1  # systemd: "condition check resulted in … skipped"
            elif not self.active:
                self.active = True
                self.mem -= self.footprint
            return Result(0, "", "")
        if verb in ("enable", "disable"):
            self.enabled = f"{verb}d"
            return Result(0, "", "")
        raise AssertionError(f"unexpected systemctl call {argv}")


@pytest.fixture
def systemd(tmp_path: Path) -> FakeSystemd:
    root = tmp_path / "root"
    unit = root / UNIT_PATH.lstrip("/")
    unit.parent.mkdir(parents=True)
    unit.write_text(UNIT)
    weights = root / "home/jim/models/qwen3.8-27b"
    weights.mkdir(parents=True)
    (weights / "Qwen3.8-27B-Q8_0.gguf").write_bytes(b"x" * 1000)
    return FakeSystemd(root)


def _reservation(systemd: FakeSystemd, *, health: int = 200) -> Reservation:
    host = Host(
        root=systemd.root,
        run=systemd,
        meminfo=lambda: {"MemAvailable": systemd.mem},
        http_status=lambda url: health,
        sleep=lambda s: None,
        now=lambda: datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    return Reservation(host)


def test_reserve_records_the_prior_service_then_displaces_it(systemd: FakeSystemd) -> None:
    before = systemd.cat()
    out = _reservation(systemd).reserve()

    assert out["reservation"] == "held" and out["already"] is False
    assert out["notice"] == "http://127.0.0.1:8090/"
    rec = out["record"]
    assert rec["state"] == "held"
    assert rec["prior_unit_path"] == UNIT_PATH
    assert rec["prior_active"] is True and rec["prior_enabled"] == "enabled"
    assert rec["prior_dropins"] == {} and rec["prior_dropin_dir_existed"] is False
    assert (rec["notice_host"], rec["notice_port"]) == ("127.0.0.1", 8090)
    assert rec["freed_bytes"] == 37 * GIB
    assert rec["required_available_bytes"] == 37 * GIB + HEADROOM_BYTES

    assert not systemd.active and systemd.notice_active
    # The unit file itself is untouched; the reservation is one drop-in.
    assert systemd.path(UNIT_PATH).read_text() == UNIT
    assert systemd.dropins() == [f"{DROPIN_DIR}/{DROPIN_NAME}"]
    # The prior configuration is kept verbatim for the operator.
    record_dir = systemd.path("/var/lib/dgx-autonomy/reservation")
    assert (record_dir / "prior" / "unit").read_text() == UNIT
    assert (record_dir / "prior" / "systemctl-cat").read_text() == before
    assert (record_dir / "notice.env").read_text() == "NOTICE_HOST=127.0.0.1\nNOTICE_PORT=8090\n"


def test_the_service_cannot_start_while_the_reservation_is_held(systemd: FakeSystemd) -> None:
    _reservation(systemd).reserve()
    # By hand, by Restart=always, or at boot: the drop-in's condition skips the start.
    systemd(["systemctl", "start", SERVICE])
    assert not systemd.active and systemd.skipped_starts == 1


def test_reserve_twice_is_a_no_op(systemd: FakeSystemd) -> None:
    r = _reservation(systemd)
    first = r.reserve()
    calls = len(systemd.calls)
    again = r.reserve()
    assert again["already"] is True
    assert again["record"] == first["record"]
    assert not any(c[1] in ("stop", "start") for c in systemd.calls[calls:])


def test_release_restores_the_exact_prior_configuration(systemd: FakeSystemd) -> None:
    before = systemd.cat()
    r = _reservation(systemd)
    r.reserve()
    out = r.release()

    assert out["reservation"] == "released"
    assert out["configuration_restored"] is True and out["mismatches"] == []
    assert out["health"] == "ready"
    assert systemd.active and not systemd.notice_active
    assert systemd.cat() == before
    assert systemd.path(UNIT_PATH).read_text() == UNIT
    # The drop-in directory did not exist before, so it does not exist after.
    assert not systemd.path(DROPIN_DIR).exists()
    assert r.read_record() is None
    archived = list(systemd.path("/var/lib/dgx-autonomy/reservation/released").iterdir())
    assert len(archived) == 1 and json.loads(archived[0].read_text())["state"] == "releasing"
    # The notice gives the port back before claude-qwen needs it.
    verbs = [(c[1], c[2] if len(c) > 2 else "") for c in systemd.calls]
    assert verbs.index(("stop", NOTICE_SERVICE)) < verbs.index(("start", SERVICE))


def test_release_keeps_the_operators_own_dropins(systemd: FakeSystemd) -> None:
    own = systemd.path(f"{DROPIN_DIR}/10-limits.conf")
    own.parent.mkdir(parents=True)
    own.write_text("[Service]\nMemoryHigh=60G\n")
    before = systemd.cat()
    r = _reservation(systemd)
    rec = r.reserve()["record"]
    assert list(rec["prior_dropins"]) == [f"{DROPIN_DIR}/10-limits.conf"]
    assert rec["prior_dropin_dir_existed"] is True

    out = r.release()
    assert out["configuration_restored"] is True
    assert systemd.cat() == before
    assert own.read_text() == "[Service]\nMemoryHigh=60G\n"


def test_release_is_refused_while_the_memory_is_not_there(systemd: FakeSystemd) -> None:
    r = _reservation(systemd)
    r.reserve()
    systemd.mem -= 40 * GIB  # a retained demo and the owned llama-server hold memory
    calls = len(systemd.calls)

    with pytest.raises(Refused, match="still held"):
        r.release()

    # Nothing changed: still displaced, still announced, still recorded.
    assert not any(c[1] in ("stop", "start", "daemon-reload") for c in systemd.calls[calls:])
    assert not systemd.active and systemd.notice_active
    assert systemd.dropins() == [f"{DROPIN_DIR}/{DROPIN_NAME}"]
    record = r.read_record()
    assert record is not None and record.state == "held"

    systemd.mem += 40 * GIB  # the operator stopped the demo
    assert r.release()["reservation"] == "released"
    assert systemd.active


def test_release_without_a_reservation_does_nothing(systemd: FakeSystemd) -> None:
    assert _reservation(systemd).release() == {"reservation": "none", "already": True}
    assert systemd.calls == []


def test_a_stopped_service_stays_stopped_after_release(systemd: FakeSystemd) -> None:
    systemd.active = False
    r = _reservation(systemd)
    rec = r.reserve()["record"]
    assert rec["prior_active"] is False
    # Nothing was freed, so the weights on disk stand in for its footprint.
    assert rec["freed_bytes"] == 1000
    out = r.release()
    assert not systemd.active and out["health"] is None


def test_a_unit_edited_during_the_reservation_is_reported_not_overwritten(
    systemd: FakeSystemd,
) -> None:
    r = _reservation(systemd)
    r.reserve()
    edited = UNIT.replace("--ctx-size 262144", "--ctx-size 131072")
    systemd.path(UNIT_PATH).write_text(edited)

    out = r.release()
    assert out["configuration_restored"] is False
    assert any("changed during the reservation" in m for m in out["mismatches"])
    assert systemd.path(UNIT_PATH).read_text() == edited


def test_a_half_done_reserve_resumes_with_the_first_snapshot(
    systemd: FakeSystemd, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _reservation(systemd)

    def crash(*_args: object, **_kwargs: object) -> None:
        raise rs.ReservationError("controller of the universe crashed")

    monkeypatch.setattr(r, "_wait_inactive", crash)
    with pytest.raises(rs.ReservationError):
        r.reserve()
    record = r.read_record()
    assert record is not None and record.state == "reserving"
    assert not systemd.active  # stopped before the crash
    monkeypatch.undo()

    rec = r.reserve()["record"]
    # What the service was before the first attempt, not the half-reserved state.
    assert rec["state"] == "held" and rec["prior_active"] is True
    assert rec["freed_bytes"] == 37 * GIB
    r.release()
    assert systemd.active


def test_client_address_reads_host_and_port_across_continuations() -> None:
    assert rs.client_address(UNIT) == ("127.0.0.1", 8090)
    assert rs.client_address("[Service]\nExecStart=/bin/llama-server --port=9000\n") == (
        "127.0.0.1",
        9000,
    )


def test_a_unit_without_a_port_is_not_reserved(systemd: FakeSystemd) -> None:
    systemd.path(UNIT_PATH).write_text(UNIT.replace("--port 8090", ""))
    with pytest.raises(Refused, match="--port"):
        _reservation(systemd).reserve()
    assert not systemd.path("/var/lib/dgx-autonomy/reservation/record.json").exists()
    assert systemd.active


def test_the_helper_takes_exactly_one_known_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert rs.main([]) == rs.EXIT_REFUSED
    assert rs.main(["reserve", "--force"]) == rs.EXIT_REFUSED
    assert rs.main(["rm"]) == rs.EXIT_REFUSED
    assert "usage" in capsys.readouterr().err


def test_the_helper_imports_only_the_standard_library() -> None:
    """It runs as root from /usr/local/sbin with `python3 -I`, outside any venv."""
    source = Path(rs.__file__).read_text()
    assert source.startswith("#!/usr/bin/python3 -I\n")
    imports = [
        line.split()[1]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]
    assert imports and all(not m.startswith((".", "dgx_autonomy")) for m in imports)
    assert "yaml" not in imports and "httpx" not in imports
