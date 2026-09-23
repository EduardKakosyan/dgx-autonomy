"""The planning REPL behind `dgx-autonomy plan`. A client of the control socket only.

The planning session lives in the controller: closing this REPL, the SSH session or
the laptop changes nothing, and `dgx-autonomy plan --attach` picks it up again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TextIO

Call = Callable[..., Any]
MAX_BUDGET_HOURS = 40.0
_BLOCK = '"""'
_CHECK_TIMEOUT_S = 1800.0
_TAKES_ARGUMENT = frozenset({"/launch"})
# After a message, how long a planner that shows no sign of work is waited for.
_START_WAIT_S = 90.0
HELP = """\
    TEXT               a message to the planner; its reply is streamed
    /draft             the draft brief and checks, and whether they can be launched
    /checks            dry-run the draft checks against an empty target
    /launch [HOURS]    freeze exactly the draft shown, and start the run (budget ≤ 40)
    /status            the planning session
    /interrupt         stop the planner's current turn
    /close             abandon the plan (its sandbox goes; the draft stays on disk)
    /detach            leave; the plan keeps going (Ctrl-D too)

A line ending in a backslash continues on the next line; a line with only three
double quotes starts or ends a block of lines."""


def finish_message(text: str) -> str | None:
    """The message of a `finish` action as summarize_event wrote it, or None."""
    at = text.find("finish {")
    if at < 0 or (at > 0 and not text[:at].endswith("-> ")):
        return None
    try:
        args = json.loads(text[at + len("finish ") :])
    except ValueError:
        return None
    message = args.get("message") if isinstance(args, dict) else None
    return str(message) if message else None


class PlanSession:
    def __init__(
        self,
        call: Call,
        plan_id: str,
        *,
        out: TextIO,
        read: Callable[[str], str] = input,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = 2.0,
    ) -> None:
        self._call = call
        self.plan_id = plan_id
        self._out = out
        self._read = read
        self._sleep = sleep
        self._poll_s = poll_s
        self.since = 0
        self.launched: dict[str, Any] | None = None

    def say(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    # --- events ---------------------------------------------------------------------

    def _show(self, ev: dict[str, Any], *, history: bool) -> bool:
        """Print one event. True when it is the planner speaking."""
        kind, source, text = ev.get("kind"), ev.get("source"), str(ev.get("text") or "")
        if kind == "MessageEvent" and source == "agent":
            self.say(f"\nplanner> {text}\n")
            return True
        if kind == "MessageEvent" and source == "user":
            if history:
                self.say(f"you> {text if len(text) <= 400 else text[:399] + '…'}")
            return False
        if kind == "ActionEvent":
            message = finish_message(text)
            if message is not None:
                self.say(f"\nplanner> {message}\n")
                return True
            self.say(f"  · {text if len(text) <= 160 else text[:159] + '…'}")
            return False
        if kind in ("AgentErrorEvent", "ConversationErrorEvent"):
            self.say(f"  ! {text[:600]}")
        return False

    def history(self) -> None:
        while True:
            page = self._call("plan.events", {"plan_id": self.plan_id, "since": self.since})
            for ev in page["events"]:
                self._show(ev, history=True)
            self.since = page["next"]
            if not page["events"]:
                return

    def wait_until_open(self) -> bool:
        announced = False
        while True:
            status = self._call("plan.status", {"plan_id": self.plan_id})
            if status["state"] == "open":
                return True
            if status["state"] != "starting":
                self.say(f"plan {self.plan_id} is {status['state']}: {status.get('error') or ''}")
                return False
            if not announced:
                self.say("starting the planner (the model may take a few minutes to load)…")
                announced = True
            self._sleep(self._poll_s * 2)

    def follow(self, *, after_send: bool) -> str | None:
        """Print the planner's events until its turn is over. Returns its status."""
        spoke = False
        saw_running = False
        quiet_polls = 0
        while True:
            page = self._call("plan.events", {"plan_id": self.plan_id, "since": self.since})
            for ev in page["events"]:
                spoke = self._show(ev, history=False) or spoke
            self.since = page["next"]
            status = page.get("conversation_status")
            saw_running = saw_running or status == "running"
            settled = status not in ("running", None)
            quiet_polls = 0 if page["events"] or status == "running" else quiet_polls + 1
            if settled and not page["events"]:
                if spoke or saw_running or not after_send:
                    if status in ("error", "stuck"):
                        self.say(f"  ! the planner's conversation is {status}; send a message")
                    return str(status)
                if quiet_polls * self._poll_s >= _START_WAIT_S:
                    self.say("  ! the planner has not started on the message; /status shows why")
                    return str(status)
            self._sleep(self._poll_s)

    # --- commands -------------------------------------------------------------------

    def draft(self) -> dict[str, Any]:
        d: dict[str, Any] = self._call("plan.draft", {"plan_id": self.plan_id})
        if d.get("brief") is None:
            self.say(f"no draft yet: {d.get('problem')}")
            return d
        self.say("----- draft/brief.md " + "-" * 40)
        self.say(str(d["brief"]).rstrip())
        self.say("-" * 61)
        for c in d["criteria"]:
            what = f"{c['runner']} checks/{c['test']}" if c["kind"] == "automated" else "human"
            optional = "" if c["required"] else " (optional)"
            self.say(f"  {c['key']:<24} {what}{optional}: {c['description']}")
        others = [f for f in d["files"] if f not in {c.get("test") for c in d["criteria"]}]
        if others:
            self.say(f"  other files in checks/: {', '.join(others)}")
        self.say(f"digest  {d['digest']}")
        if d.get("problem"):
            self.say(f"cannot launch: {d['problem']}")
        dry = d.get("dry_run")
        if dry and d.get("dry_run_current"):
            self.say(f"dry run #{dry['n']}: {'ok' if dry['ok'] else 'HAS PROBLEMS'} (this draft)")
        elif dry:
            self.say(f"dry run #{dry['n']} was of an earlier draft; /checks again")
        else:
            self.say("not dry-run yet; /checks runs the checks against an empty target")
        return d

    def checks(self) -> None:
        self.say("running each automated check against an empty target…")
        r = self._call("plan.checks", {"plan_id": self.plan_id}, timeout=_CHECK_TIMEOUT_S)
        for c in r["checks"]:
            self.say(f"  {'ok     ' if c['ok'] else 'PROBLEM'} {c['key']:<24} {c['verdict']}")
            self.say(f"          {'':<24} {c['reference_verdict']}")
            if c["status"] != "failed" and c.get("excerpt"):
                self.say("      " + str(c["excerpt"])[:800].replace("\n", "\n      "))
            if c.get("reference_ok") is False and c.get("reference_excerpt"):
                self.say("      " + str(c["reference_excerpt"])[:800].replace("\n", "\n      "))
        if not r["ok"]:
            self.say("the planner has the results; ask it to fix what has a problem")
        elif r.get("satisfiable"):
            self.say("every check fails with no app and passes against the reference: good")
        else:
            self.say(
                "every check runs and fails with no app, but no reference app shows they"
                " can pass; ask the planner for one"
            )
        self.say(f"evidence: {r.get('evidence_dir')}")

    def launch(self, arg: str) -> bool:
        try:
            hours = float(arg) if arg else MAX_BUDGET_HOURS
        except ValueError:
            self.say("usage: /launch [HOURS]")
            return False
        if not 0 < hours <= MAX_BUDGET_HOURS:
            self.say(f"the budget must be in (0, {MAX_BUDGET_HOURS:g}] hours")
            return False
        d = self.draft()
        if d.get("problem") or d.get("brief") is None:
            return False
        dry = d.get("dry_run")
        if not (dry and d.get("dry_run_current") and dry.get("ok")):
            self.say("the checks of this draft have not passed a dry run (/checks).")
            answer = self._read("launch anyway? type 'force' to launch, anything else to go back: ")
            if answer.strip() != "force":
                return False
        answer = self._read(f"freeze this draft and launch with a {hours:g} h budget? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return False
        result = self._call(
            "plan.launch",
            {"plan_id": self.plan_id, "digest": d["digest"], "budget_hours": hours},
        )
        self.launched = result
        self.say(f"launched run {result['run_id']}; deadline {result['deadline_at']}")
        self.say(f"frozen {result['frozen_digest']}")
        self.say(
            f"follow it with `dgx-autonomy status {result['run_id']}`,"
            f" `dgx-autonomy logs -f {result['run_id']}` and `dgx-autonomy report`."
        )
        return True

    def status(self) -> None:
        s = self._call("plan.status", {"plan_id": self.plan_id})
        self.say(f"plan {s['plan_id']}  {s['state']}  model {s['model_key']}")
        self.say(f"  planner {s.get('conversation_status')}, sandbox {s.get('sandbox')}")
        draft = s.get("draft") or {}
        self.say(
            f"  draft {draft.get('digest')}: {draft.get('automated')} automated,"
            f" {draft.get('human_judgment')} human-judgment criteria"
        )
        if draft.get("problem"):
            self.say(f"  {draft['problem']}")

    def read_message(self) -> str:
        """One message: continued lines (trailing backslash) and triple-quote blocks."""
        line = self._read("you> ")
        if line.strip() == _BLOCK:
            lines = []
            while (more := self._read("... ")).strip() != _BLOCK:
                lines.append(more)
            return "\n".join(lines)
        parts = [line]
        while parts[-1].endswith("\\"):
            parts[-1] = parts[-1][:-1]
            parts.append(self._read("... "))
        return "\n".join(parts)

    def handle(self, line: str) -> bool:
        """Act on one input. False when the REPL should end."""
        if not line.strip():
            return True
        if not line.startswith("/"):
            self._call("plan.send", {"plan_id": self.plan_id, "text": line.strip()})
            self.follow(after_send=True)
            return True
        command, _, arg = line.strip().partition(" ")
        if arg.strip() and command not in _TAKES_ARGUMENT:
            # "/draft says ..." is most likely a message, not the command.
            self.say(
                f"{command} takes no argument; nothing was sent. To send a message that"
                " starts with '/', begin it with a space."
            )
            return True
        if command in ("/detach", "/quit", "/exit"):
            self.say(f"detached. `dgx-autonomy plan --attach {self.plan_id}` continues.")
            return False
        if command == "/help":
            self.say(HELP)
        elif command == "/draft":
            self.draft()
        elif command == "/checks":
            self.checks()
        elif command == "/launch":
            return not self.launch(arg.strip())
        elif command == "/status":
            self.status()
        elif command == "/interrupt":
            self._call("plan.interrupt", {"plan_id": self.plan_id})
            self.say("interrupted the planner's turn")
        elif command == "/close":
            if self._read("abandon this plan? [y/N] ").strip().lower() in ("y", "yes"):
                self._call("plan.close", {"plan_id": self.plan_id})
                self.say(f"plan {self.plan_id} closed; its draft stays on the DGX")
                return False
        else:
            self.say(f"unknown command {command}; /help lists them")
        return True

    def run(self) -> int:
        from .control_api import ControlError

        self.say(f"plan {self.plan_id}. /help lists the commands; Ctrl-D detaches.")
        self.history()
        if not self.wait_until_open():
            return 1
        try:
            self.follow(after_send=False)
        except KeyboardInterrupt:
            self.say("\n(stopped following; the planner goes on)")
        while True:
            try:
                line = self.read_message()
            except EOFError:
                self.say(f"\ndetached. `dgx-autonomy plan --attach {self.plan_id}` continues.")
                return 0
            except KeyboardInterrupt:
                self.say("")
                continue
            try:
                if not self.handle(line):
                    return 0
            except KeyboardInterrupt:
                self.say("\n(stopped following; the planner goes on. /interrupt stops it)")
            except ControlError as exc:
                self.say(f"  ! {exc}")
