"""Planning on the DGX: scripted turns -> draft -> dry run -> launch -> verified run.

The planner is the real model in its own sandbox. The turns are scripted (they
dictate the draft) so the test does not depend on the model's product sense:

1. The request tells the planner to write a given brief and two checks.
2. The controller crashes (fault.inject) mid-plan; the plan, its conversation and
   the draft survive it, as they survive a dropped SSH session.
3. A second turn changes the heading. The draft follows.
4. The dry run runs both checks against an empty target (both run and fail) and
   against the planner's reference page (both pass).
5. Launch freezes exactly the reviewed draft. The run gets its deadline then, builds
   the page and finishes VERIFIED. One of the frozen checks asserts, from inside the
   evaluator, that /checks is mounted read-only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from dgx_autonomy.cli import print_report
from dgx_autonomy.control_api import ControlError

from dgx_helpers import wait_for

pytestmark = pytest.mark.dgx

BUDGET_HOURS = 0.75

BRIEF = """\
# Brief: greeting page

1. Create `index.html` in the project directory containing exactly: <h1>hello</h1>
2. Call the `start_demo` tool with command `python3 -m http.server 3000 --bind 0.0.0.0`
   and port 3000.
3. Finish. Do not create any other file.
"""

CRITERIA = """\
criteria:
  - key: home
    description: The home page shows the heading hello
    test: home.spec.ts
  - key: checks-read-only
    description: The app answers, and the evaluator cannot change the frozen checks
    test: test_read_only.py
"""

HOME_SPEC = """\
import { test, expect } from '@playwright/test'

test('the home page shows the heading', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('hello')
})
"""

READ_ONLY_TEST = """\
import os

import httpx
import pytest


def test_app_answers_and_checks_are_read_only() -> None:
    r = httpx.get(os.environ["APP_URL"], timeout=10)
    assert r.status_code == 200
    with pytest.raises(OSError):
        open("/checks/tampered.txt", "w")
"""

REQUEST = f"""\
This is a scripted test of the planning environment; no research or questions are
needed. Write these five files exactly as given, with your file editor, then reply
with the single line: draft written

File /workspace/draft/brief.md:
<<<
{BRIEF}>>>

File /workspace/draft/checks/criteria.yaml:
<<<
{CRITERIA}>>>

File /workspace/draft/checks/home.spec.ts:
<<<
{HOME_SPEC}>>>

File /workspace/draft/checks/test_read_only.py:
<<<
{READ_ONLY_TEST}>>>

File /workspace/draft/reference/index.html:
<<<
<h1>hello</h1>
>>>
"""

SECOND_TURN = (
    "Change the heading from hello to 'hello planner' everywhere it appears: in"
    " /workspace/draft/brief.md (the <h1> line), in"
    " /workspace/draft/checks/home.spec.ts (the toHaveText value) and in"
    " /workspace/draft/reference/index.html. Change nothing else, then reply with the"
    " single line: draft updated"
)


def _turn_over(control: Callable[..., Any], plan_id: str, since: int) -> dict[str, Any]:
    """Wait until the planner has spoken after event `since` and is no longer running."""

    def probe() -> dict[str, Any]:
        page: dict[str, Any] = control("plan.events", {"plan_id": plan_id, "since": since})
        return page

    def done(page: dict[str, Any]) -> bool:
        spoke = any(e["source"] == "agent" for e in page["events"])
        return spoke and page["conversation_status"] not in ("running", None)

    page = wait_for(probe, done, timeout_s=20 * 60, every_s=10)
    assert page["conversation_status"] not in ("error", "stuck"), page
    return page


def _event_count(control: Callable[..., Any], plan_id: str) -> int:
    since = 0
    while True:
        page = control("plan.events", {"plan_id": plan_id, "since": since, "limit": 500})
        if not page["events"]:
            return since
        since = page["next"]


def test_plan_to_launch(control: Callable[..., Any]) -> None:
    started = control("plan.start", {"request": REQUEST})
    plan_id = started["plan_id"]
    wait_for(
        lambda: control("plan.status", {"plan_id": plan_id}),
        lambda s: s["state"] != "starting",
        timeout_s=30 * 60,
        every_s=10,
    )
    _turn_over(control, plan_id, 0)
    draft = control("plan.draft", {"plan_id": plan_id})
    assert draft["problem"] is None, draft
    assert [c["key"] for c in draft["criteria"]] == ["home", "checks-read-only"]
    status = control("plan.status", {"plan_id": plan_id})
    cid = status["conversation_id"]

    # The controller crashes mid-plan. The plan is the controller's, not the CLI's.
    control("fault.inject", {"target": "controller", "confirm": "controller"})
    time.sleep(5)
    wait_for(lambda: _ping(control), bool, timeout_s=300, every_s=5)
    status = control("plan.status", {"plan_id": plan_id})
    assert (status["state"], status["conversation_id"]) == ("open", cid)
    assert control("plan.draft", {"plan_id": plan_id})["digest"] == draft["digest"]

    since = _event_count(control, plan_id)
    control("plan.send", {"plan_id": plan_id, "text": SECOND_TURN})
    _turn_over(control, plan_id, since)
    draft = control("plan.draft", {"plan_id": plan_id})
    assert draft["problem"] is None, draft
    assert "hello planner" in draft["brief"], draft["brief"]

    dry = control("plan.checks", {"plan_id": plan_id}, timeout=1800)
    print(dry)
    assert dry["digest"] == draft["digest"]
    assert [(c["key"], c["status"], c["reference_status"]) for c in dry["checks"]] == [
        ("home", "failed", "passed"),
        ("checks-read-only", "failed", "passed"),
    ], dry
    assert dry["ok"] is True and dry["satisfiable"] is True

    launched = control(
        "plan.launch",
        {"plan_id": plan_id, "digest": draft["digest"], "budget_hours": BUDGET_HOURS},
    )
    run_id = launched["run_id"]
    assert launched["frozen_digest"] == draft["digest"]
    status = control("plan.status", {"plan_id": plan_id})
    assert (status["state"], status["run_id"], status["sandbox"]) == ("launched", run_id, "removed")

    run = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: s["phase"] in ("finished", "failed", "stopped"),
        timeout_s=BUDGET_HOURS * 3600 + 15 * 60,
        every_s=15,
    )
    report = control("report", {"run_id": run_id})
    print_report(report)
    assert run["plan_id"] == plan_id
    assert report["frozen"]["digest"] == draft["digest"] and report["frozen"]["intact"]
    assert run["phase"] == "finished", run
    assert report["verified"] is True, report["verdict"]
    last = report["evaluations"][-1]
    assert last["results"]["checks-read-only"]["status"] == "passed", last


def _ping(control: Callable[..., Any]) -> bool:
    try:
        control("ping", timeout=10)
    except ControlError:
        return False
    return True
