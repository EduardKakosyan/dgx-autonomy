"""The host pieces agree with each other and with the controller's settings.

They are installed by hand (host/install.sh), so nothing else would notice a bridge
renamed in one place, a range missing from the rules, or a sudoers entry that
grants more than the three helper actions.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from dgx_autonomy import reservation
from dgx_autonomy.config import Settings

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_names_agree_between_compose_rules_and_settings() -> None:
    settings = Settings.from_env({})
    compose = yaml.safe_load((ROOT / "containers" / "compose.yaml").read_text())
    nets = compose["networks"]
    named = {n["name"]: n["driver_opts"]["com.docker.network.bridge.name"] for n in nets.values()}
    assert named == {
        settings.egress_network: settings.egress_bridge,
        settings.internal_network: settings.internal_bridge,
    }
    rules = (HOST / "nftables-autonomy.nft").read_text()
    assert f'"{settings.egress_bridge}", "{settings.internal_bridge}"' in rules


@pytest.mark.parametrize(
    "cidr",
    [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",  # the DGX's LAN, 192.168.50.0/24
        "100.64.0.0/10",  # Tailscale, including 100.100.100.100
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",  # the LAN's fd21:… and Tailscale's fd7a:…
        "fe80::/10",
        "::ffff:0.0.0.0/96",
    ],
)
def test_the_rules_block_private_and_tailnet_ranges(cidr: str) -> None:
    rules = (HOST / "nftables-autonomy.nft").read_text()
    assert re.search(rf"^\s*{re.escape(cidr)}\s*,?\s*(#.*)?$", rules, re.M) or f"{cidr}," in rules


def test_the_rules_reject_everything_to_the_host_itself() -> None:
    rules = (HOST / "nftables-autonomy.nft").read_text()
    chain = rules.split("chain input", 1)[1].split("}", 1)[0]
    assert "iifname != @agent_bridges accept" in chain
    assert "ct state established,related accept" in chain
    assert chain.strip().splitlines()[-1].strip() == "counter reject with icmpx admin-prohibited"


def test_sudoers_grants_exactly_the_three_helper_actions() -> None:
    lines = [
        line
        for line in (HOST / "sudoers-autonomy").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 1
    user, grant = lines[0].split(" ALL=(root) NOPASSWD: ")
    assert user == "jim"
    commands = [c.strip() for c in grant.split(",")]
    assert commands == [
        f"/usr/local/sbin/dgx-autonomy-reservation {a}" for a in ("reserve", "release", "status")
    ]


def test_units_and_helper_point_at_the_same_record() -> None:
    record = str(reservation.RECORD_DIR / reservation.RECORD_NAME)
    notice = (HOST / "dgx-autonomy-busy-notice.service").read_text()
    assert f"ConditionPathExists={record}" in notice
    assert f"EnvironmentFile={reservation.RECORD_DIR / reservation.NOTICE_ENV_NAME}" in notice
    assert f"ConditionPathExists=!{record}" in reservation.dropin_text("claude-qwen.service")
    assert Settings.from_env({}).reservation_record == Path(record)
    egress = (HOST / "dgx-autonomy-egress").read_text()
    assert 'MARKER="$MARKER_DIR/egress.json"' in egress
    assert Settings.from_env({}).egress_marker == Path("/var/lib/dgx-autonomy/policy/egress.json")


@pytest.fixture
def notice_url() -> str:
    mod = _load("busy_notice", HOST / "busy_notice.py")
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[misc]
    server.shutdown()


def _fetch(url: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    raise AssertionError("expected an error status")


def test_the_busy_notice_answers_openai_clients(notice_url: str) -> None:
    status, body = _fetch(f"{notice_url}/v1/chat/completions", {"model": "qwen3.8-opus"})
    assert status == 503
    error = body["error"]
    assert isinstance(error, dict)
    assert "in use by the autonomous coding environment" in str(error["message"])


def test_the_busy_notice_answers_anthropic_clients(notice_url: str) -> None:
    status, body = _fetch(f"{notice_url}/v1/messages", {"model": "qwen3.8-opus"})
    assert status == 503
    assert body["type"] == "error"
    error = body["error"]
    assert isinstance(error, dict) and error["type"] == "overloaded_error"
    assert "dgx-autonomy release" in str(error["message"])


def test_the_busy_notice_fails_health_checks(notice_url: str) -> None:
    status, _ = _fetch(f"{notice_url}/health")
    assert status == 503
