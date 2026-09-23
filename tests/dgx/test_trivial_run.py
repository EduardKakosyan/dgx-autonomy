"""Walking skeleton: a trivial brief goes through every layer and finishes unattended.

CLI contract -> control socket -> controller -> SQLite -> owned llama-server ->
Agent Server in the unprivileged sandbox -> OpenHands conversation -> a file in the
run's workspace on the host.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dgx_helpers import wait_for

pytestmark = pytest.mark.dgx

BRIEF = """\
# Brief: hello file

Create a file named `hello.txt` in the project directory. Its only content must be
today's date in ISO format (YYYY-MM-DD), as reported by the `date` command in your
sandbox. Check the file's content, then finish. Do nothing else.
"""


def test_trivial_brief_finishes_unattended(control: Callable[..., Any]) -> None:
    launched = control("launch", {"brief_text": BRIEF, "budget_hours": 1})
    run_id = launched["run_id"]

    status = wait_for(
        lambda: control("status", {"run_id": run_id}),
        lambda s: s["phase"] in ("finished", "failed"),
        timeout_s=60 * 60,
        every_s=15,
    )
    assert status["phase"] == "finished", status
    assert status["conversation_status"] == "finished"
    assert {op["kind"]: op["status"] for op in status["operations"]} == {
        "inference.start": "done",
        "workspace.create": "done",
        "conversation.start": "done",
    }
    assert status["deadline_at"] == launched["deadline_at"]

    hello = Path(status["workspace_dir"]) / "hello.txt"
    assert hello.is_file(), f"{hello} missing"
    content = hello.read_text().strip()
    assert len(content) == 10 and content[4] == "-" and content[7] == "-", content

    logs = control("logs", {"run_id": run_id})
    kinds = {e["kind"] for e in logs["events"]}
    assert "ActionEvent" in kinds and "ObservationEvent" in kinds, kinds
