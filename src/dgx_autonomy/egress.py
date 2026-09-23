"""The agent's network boundary, as the controller can see and test it.

The rules themselves live on the host (host/nftables-autonomy.nft) and need root;
the controller has neither root nor the host's network namespace. It relies on two
things it can check:

- the marker the host's loader writes (`policy/egress.json` in the data directory),
  which must name the current boot, so rules from a previous boot do not count;
- the host interface names of the agent's Docker networks, which must be the names
  the rules match.

Until both hold, no agent container is created. `probe` then shows the policy from
the inside: a throwaway container with the agent's image, networks, DNS and
hardening tries to connect to a list of targets.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .ports import ContainerSpec, RuntimePort
from .runtime import LABEL_PREFIX, LABEL_ROLE, DockerError

MAX_PROBE_TARGETS = 64
_HOST = re.compile(r"^[A-Za-z0-9.:_-]{1,253}$")

# Runs with `python3 -I -c` in the probe container: resolve and connect to each
# target, report the result and the container's default gateway (the DGX's address
# on the egress bridge).
PROBE_SCRIPT = r"""
import json, socket, sys, time
targets, timeout = json.loads(sys.argv[1]), float(sys.argv[2])
def gateway():
    for line in open("/proc/net/route").read().splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
out = []
for host, port in targets:
    r = {"host": host, "port": port, "ok": False, "addresses": [], "error": None}
    t = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        r["addresses"] = sorted({i[4][0] for i in infos})
        with socket.create_connection((r["addresses"][0], port), timeout=timeout):
            r["ok"] = True
    except OSError as e:
        r["error"] = f"{type(e).__name__}: {e}"
    r["seconds"] = round(time.monotonic() - t, 2)
    out.append(r)
print(json.dumps({"gateway": gateway(), "results": out}))
"""


class EgressPolicyError(RuntimeError):
    """The agent's network boundary is not in place; no agent container may start."""


@dataclass(frozen=True)
class PolicyCheck:
    ok: bool
    problems: tuple[str, ...]
    marker: dict[str, Any] | None
    bridges: dict[str, str | None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "marker": self.marker,
            "bridges": self.bridges,
        }


def check_policy(settings: Settings, runtime: RuntimePort) -> PolicyCheck:
    problems: list[str] = []
    marker: dict[str, Any] | None = None
    try:
        raw = json.loads(settings.egress_marker.read_text())
        marker = raw if isinstance(raw, dict) else None
    except FileNotFoundError:
        problems.append(
            f"the host egress policy is not loaded (no {settings.egress_marker}); the"
            " operator installs it with `sudo host/install.sh` (README, Phase 3)"
        )
    except (OSError, ValueError) as exc:
        problems.append(f"cannot read {settings.egress_marker}: {exc}")
    if marker is not None:
        try:
            boot_id = settings.boot_id_file.read_text().strip()
        except OSError as exc:
            boot_id = f"unreadable ({exc})"
        if marker.get("boot_id") != boot_id:
            problems.append(
                "the egress policy marker is from another boot; dgx-autonomy-egress.service"
                " has not loaded the rules since the DGX started"
            )
    bridges: dict[str, str | None] = {}
    for network, expected in (
        (settings.egress_network, settings.egress_bridge),
        (settings.internal_network, settings.internal_bridge),
    ):
        try:
            actual = runtime.network_bridge(network)
        except DockerError as exc:
            problems.append(str(exc))
            continue
        bridges[network] = actual
        if actual is None:
            problems.append(f"Docker network {network} does not exist")
        elif actual != expected:
            problems.append(
                f"Docker network {network} uses bridge {actual!r}, not {expected!r}, so the"
                " egress rules do not apply to it; recreate the compose networks (README)"
            )
    return PolicyCheck(not problems, tuple(problems), marker, bridges)


def probe_targets(raw: object) -> list[tuple[str, int]]:
    """Validate `[[host, port], ...]` from a control request."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PROBE_TARGETS:
        raise ValueError(f"targets must be a list of 1..{MAX_PROBE_TARGETS} [host, port] pairs")
    out = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise ValueError(f"bad target {item!r}; expected [host, port]")
        host, port = item
        if not isinstance(host, str) or not _HOST.match(host):
            raise ValueError(f"bad host {host!r}")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"bad port {port!r}")
        out.append((host, port))
    return out


def probe_container_spec(
    settings: Settings, targets: Sequence[tuple[str, int]], timeout_s: float
) -> ContainerSpec:
    """The agent's network position, image and hardening, without its mounts or ports."""
    return ContainerSpec(
        name=f"{LABEL_PREFIX}-probe-{secrets.token_hex(4)}",
        image=settings.agent_image,
        labels={LABEL_ROLE: "probe"},
        networks=(settings.internal_network, settings.egress_network),
        dns=settings.agent_dns,
        entrypoint="/usr/local/bin/python3",
        command=("-I", "-c", PROBE_SCRIPT, json.dumps(list(targets)), f"{timeout_s:g}"),
        user=f"{settings.agent_uid}:{settings.agent_gid}",
        memory="256m",
        cpus="1",
        pids_limit=64,
    )


def probe(
    settings: Settings,
    runtime: RuntimePort,
    targets: Sequence[tuple[str, int]],
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    spec = probe_container_spec(settings, targets, timeout_s)
    code, stdout = runtime.run_to_completion(spec, timeout_s * len(targets) + 60.0)
    try:
        out = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise DockerError(f"probe exited {code} without a result: {stdout[-500:]!r}") from None
    if not isinstance(out, dict):
        raise DockerError(f"probe: unexpected result {out!r}")
    return out
