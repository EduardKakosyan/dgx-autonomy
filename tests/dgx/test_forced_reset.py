"""A forced context reset on the DGX: a fresh conversation finishes without redoing work.

The brief is six slow steps, each leaving a file. After the third file exists, the
test forces a rollover. The first conversation is asked for a handoff (write_handoff),
the controller validates it against the project, records the checkpoint, pauses the
old conversation and starts conversation #2 with the brief and the checkpoint. The
run must finish VERIFIED in the same run, before the same deadline, with files 1-3
untouched (same mtime) and the checkpoint written by the agent, not a fallback.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy import frozen
from dgx_autonomy.cli import print_report

from dgx_helpers import wait_for

pytestmark = pytest.mark.dgx

BUDGET_HOURS = 1.0

BRIEF = """\
# Brief: six steps

Do these steps in order, one at a time. For each step N from 1 to 6:

1. Run `sleep 25` in the terminal and wait for it to finish.
2. Create the file `stepN.txt` in the project directory containing exactly `step N`
   (for example `step3.txt` containing `step 3`).

Never rewrite a step file that already exists: check which files exist first.
After step 6, call the `start_demo` tool with command
`python3 -m http.server 3000 --bind 0.0.0.0` and port 3000, then finish.
"""

CHECKS = {
    "criteria.yaml": """\
criteria:
  - key: steps
    description: step1.txt to step6.txt are served, each containing its step
    test: test_steps.py
""",
    "test_steps.py": """\
import os

import httpx


def test_every_step_file_is_served() -> None:
    for n in range(1, 7):
        r = httpx.get(f"{os.environ['APP_URL']}/step{n}.txt", timeout=10)
        assert r.status_code == 200, f"step{n}.txt: HTTP {r.status_code}"
        assert r.text.strip() == f"step {n}", f"step{n}.txt contains {r.text!r}"
""",
}


def test_a_forced_reset_continues_from_the_checkpoint(control: Callable[..., Any]) -> None:
    launched = control(
        "launch",
        {
            "brief_text": BRIEF,
            "checks": CHECKS,
            "bundle_digest": frozen.bundle_digest(BRIEF.encode(), frozen.encode(CHECKS)),
            "budget_hours": BUDGET_HOURS,
        },
    )
    run_id = launched["run_id"]
    status = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: s["phase"] == "running",
        timeout_s=30 * 60,
        every_s=10,
    )
    project = Path(status["workspace_dir"])
    first = status["conversation_id"]

    wait_for(lambda: (project / "step3.txt").exists(), bool, timeout_s=30 * 60, every_s=5)
    done_before = {n: os.stat(project / f"step{n}.txt").st_mtime_ns for n in (1, 2, 3)}
    control("rollover", {"run_id": run_id, "detail": "forced reset test"})

    wait_for(
        lambda: control("status", {"run_id": run_id})["conversations"],
        lambda cs: len(cs) == 2 and cs[1]["status"] == "active",
        timeout_s=20 * 60,
        every_s=10,
    )
    status = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: s["phase"] in ("finished", "failed", "stopped"),
        timeout_s=BUDGET_HOURS * 3600 + 15 * 60,
        every_s=15,
    )
    report = control("report", {"run_id": run_id})
    chain = control("checkpoints", {"run_id": run_id})
    print_report(report)
    print(json.dumps(chain, indent=2, default=str))

    assert status["phase"] == "finished", status
    assert report["verified"] is True, report["verdict"]
    assert status["deadline_at"] == launched["deadline_at"]
    assert status["conversation_id"] != first
    [ckpt] = chain["checkpoints"]
    assert ckpt["source"] == "agent_handoff", ckpt
    assert ckpt["from_conversation"] == first
    assert ckpt["reason"] == "forced"
    assert any(r["status"] == "done" for r in ckpt["handoff"]["roadmap"])
    # The fresh conversation did not redo the steps that were done.
    for n, mtime in done_before.items():
        assert os.stat(project / f"step{n}.txt").st_mtime_ns == mtime, f"step{n}.txt was redone"
