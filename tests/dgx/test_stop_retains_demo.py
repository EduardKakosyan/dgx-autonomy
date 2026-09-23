"""The deadline ends agent execution on the DGX; the demo stays reachable.

A short budget. The brief has the agent start a background process of its own (as a
tool would), serve a static page through start_demo, and then block in a long
foreground command, so the deadline arrives mid-work. After expiry:

- the run is `stopped` with outcome `expired`, and the controller's evidence shows
  no agent process survived;
- an independent look at the sandbox (the `processes` op) finds no agent process,
  only the supervisor and the demo session;
- 127.0.0.1:<host port> on the DGX still serves the page.

The evidence also answers the open question: which agent processes were still alive
after pause() and the bounded wait, before anything was killed. The test prints it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from dgx_helpers import wait_for

pytestmark = pytest.mark.dgx

BUDGET_HOURS = 0.1  # six minutes
MARKER = "dgx demo ok"

BRIEF = f"""\
# Brief: static page demo, then a long monitoring step

Do these steps in order. Each is required.

1. Create `index.html` in the project directory whose body is exactly: {MARKER}
2. Start a background worker in the terminal, exactly:
   `nohup sleep 100000 > /dev/null 2>&1 &`
3. Call the `start_demo` tool with command `python3 -m http.server 3000 --bind 0.0.0.0`
   and port 3000.
4. Check the page with `curl -s http://127.0.0.1:3000/index.html`.
5. Run the monitoring step in the terminal: `sleep 3600`, with the terminal timeout set
   to 3600 seconds, and wait for it to complete. It is part of the task: do not
   interrupt it and do not finish before it completes.
"""


def _get(url: str) -> tuple[int, str]:
    try:
        res = httpx.get(url, timeout=5)
    except httpx.HTTPError as exc:
        return 0, str(exc)
    return res.status_code, res.text


def test_expiry_stops_the_agent_and_keeps_the_demo(control: Callable[..., Any]) -> None:
    launched = control("launch", {"brief_text": BRIEF, "budget_hours": BUDGET_HOURS})
    run_id = launched["run_id"]

    status = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: s["phase"] in ("stopped", "finished", "failed"),
        timeout_s=BUDGET_HOURS * 3600 + 10 * 60,
        every_s=10,
    )
    print(json.dumps(status, indent=2, default=str))
    assert status["phase"] == "stopped", f"the run did not reach its deadline: {status}"
    assert status["outcome"] == "expired"
    assert status["deadline_at"] == launched["deadline_at"]

    evidence = status["stop_evidence"]
    assert evidence["failed"] is False, evidence
    assert evidence["survivors"] == [], evidence

    demo = status["demo"]
    assert demo is not None and demo["command"], f"the agent never started a demo: {demo}"

    # The demo is still served on DGX loopback after agent execution ended.
    url = f"http://127.0.0.1:{demo['host_port']}/index.html"
    _, body = wait_for(lambda: _get(url), lambda r: r[0] == 200, timeout_s=60, every_s=2)
    assert MARKER in body

    # An independent look at the sandbox after the fact: no agent process at all.
    procs = control("processes", {"run_id": run_id})
    assert procs["sandbox_running"]
    roles = [p["role"] for p in procs["processes"]]
    agent = [p for p in procs["processes"] if p["role"] == "agent"]
    assert agent == [], agent
    assert "demo" in roles and "supervisor" in roles

    # Stopping an ended run changes nothing.
    again = control("stop", {"run_id": run_id})
    assert again["phase"] == "stopped" and "already stopped" in again["message"]

    # The open question: did pause() end the tools' processes? Recorded, not assumed.
    print(
        "PAUSE FINDING: after pause() and the bounded wait "
        f"(conversation {evidence['conversation_status']!r}, quiescent={evidence['quiescent']}), "
        f"{len(evidence['after_pause'])} agent processes were alive, "
        f"{evidence['tool_processes_after_pause']} of them started by tools:"
    )
    for p in evidence["after_pause"]:
        print(f"  pid {p['pid']:>6} sid {p['sid']:>6}  {p['cmd']}")
