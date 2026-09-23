"""From the agent's network position: the internet and the owned model, nothing else.

A probe container with the agent's image, networks, DNS and hardening connects to
each target. The DGX's own addresses are read on the DGX when the test runs, so
every address the host has (LAN, Tailscale, Docker gateways) is tried.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.dgx

INFERENCE = ("dgx-autonomy-inference", 8080)
# Ports that listen on every address of the DGX (sshd, open-webui, a python service).
HOST_PORTS = (22, 8080, 8710)


def _ip_json(*args: str) -> Any:
    return json.loads(subprocess.run(["ip", "-j", *args], check=True, capture_output=True).stdout)


def _host_addresses() -> list[str]:
    out = []
    for link in _ip_json("-4", "addr"):
        for addr in link.get("addr_info", []):
            if not addr["local"].startswith("127."):
                out.append(addr["local"])
    return out


def _lan_router() -> str:
    return str(next(r["gateway"] for r in _ip_json("route", "show", "default")))


def _probe(control: Callable[..., Any], targets: list[tuple[str, int]]) -> dict[str, Any]:
    out = control("network.probe", {"targets": [list(t) for t in targets], "timeout_s": 4})
    return {f"{r['host']}:{r['port']}": r for r in out["results"]} | {"_": out}


def test_the_policy_is_in_place(control: Callable[..., Any]) -> None:
    policy = control("network.policy")
    assert policy["ok"], policy["problems"]


def test_the_agent_reaches_the_internet_and_its_model(control: Callable[..., Any]) -> None:
    control("inference.ensure", {}, timeout=300)  # listening while it loads is enough
    results = _probe(control, [("pypi.org", 443), ("registry.npmjs.org", 443), INFERENCE])
    for key in ("pypi.org:443", "registry.npmjs.org:443", "dgx-autonomy-inference:8080"):
        assert results[key]["ok"], results[key]


def test_the_agent_cannot_reach_the_dgx_the_lan_or_the_tailnet(
    control: Callable[..., Any],
) -> None:
    gateway = control("network.probe", {"targets": [["pypi.org", 443]]})["gateway"]
    assert gateway, "the probe found no default gateway"
    hosts = sorted({*_host_addresses(), gateway})
    targets = [(h, p) for h in hosts for p in HOST_PORTS]
    targets += [
        (_lan_router(), 80),
        (_lan_router(), 53),
        ("100.100.100.100", 53),  # Tailscale MagicDNS
        ("169.254.169.254", 80),
    ]
    results = _probe(control, targets)
    reached = {k: r for k, r in results.items() if k != "_" and r["ok"]}
    assert not reached, f"reachable from the agent: {sorted(reached)}"
    # Rejected, not silently dropped: the agent learns at once that it is not allowed.
    slow = {k: r["seconds"] for k, r in results.items() if k != "_" and r["seconds"] > 3.5}
    assert not slow, f"timed out instead of being rejected: {slow}"
