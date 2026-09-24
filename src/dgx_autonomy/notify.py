"""The operator's feed: one line per milestone, so nobody has to poll `status`.

The controller appends to `state/notifications.jsonl` as things happen: the planner
waits for the operator, a dry run ends, a run launches, the builder reports progress
or claims completion, an evaluation decides, a conversation rolls over, a recovery
runs, a run ends, a qualification ends. `dgx-autonomy notify --follow` long-polls the
`notifications` control op and prints each line as it arrives.

What the builder reports (`progress`, `claim`) is its own account and is labelled so;
what the environment established (`evaluation`, `run.ended`) is labelled apart.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_TEXT = 1500
MAX_WAIT_S = 300.0


class Notifier:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._cond = threading.Condition()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._n = self._count()

    def _count(self) -> int:
        try:
            with open(self._path, "rb") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0

    @property
    def last(self) -> int:
        return self._n

    def emit(
        self,
        kind: str,
        text: str,
        *,
        now: datetime,
        run_id: str | None = None,
        plan_id: str | None = None,
    ) -> None:
        """Append one notification. Never raises: a full disk must not stop a run."""
        with self._cond:
            entry: dict[str, Any] = {"n": self._n + 1, "at": now.isoformat(), "kind": kind}
            if run_id:
                entry["run_id"] = run_id
            if plan_id:
                entry["plan_id"] = plan_id
            entry["text"] = text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
            try:
                with open(self._path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                return
            self._n += 1
            self._cond.notify_all()

    def read(self, since: int, *, limit: int = 200, wait_s: float = 0.0) -> dict[str, Any]:
        """Notifications after `since` (a negative `since` means "from now on"),
        waiting up to `wait_s` for the first one."""
        with self._cond:
            if since < 0:
                since = self._n
            if self._n <= since and wait_s > 0:
                self._cond.wait_for(lambda: self._n > since, timeout=min(wait_s, MAX_WAIT_S))
            last = self._n
        events: list[dict[str, Any]] = []
        if last > since:
            try:
                with open(self._path) as f:
                    for i, line in enumerate(f, start=1):
                        if i <= since:
                            continue
                        if i > last or len(events) >= limit:
                            break
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
            except FileNotFoundError:
                pass
        nxt = events[-1]["n"] if events else since
        return {"events": events, "next": nxt, "last": last}


def format_line(e: dict[str, Any]) -> str:
    where = e.get("run_id") or (f"plan {e['plan_id']}" if e.get("plan_id") else "")
    head = f"{e.get('at', '')[:19]}Z #{e.get('n')} {e.get('kind')}"
    return f"{head} [{where}] {e.get('text', '')}" if where else f"{head} {e.get('text', '')}"
