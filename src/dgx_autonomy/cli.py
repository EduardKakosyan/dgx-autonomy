"""`dgx-autonomy`: the operator's SSH interface. It never runs work itself.

Every subcommand except `controller` is a thin client of the control socket. The
unattended run lives in the controller container, so closing the SSH session or the
laptop changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import default_socket_path
from .control_api import ControlError, call

MAX_BUDGET_HOURS = 40.0


def _socket(args: argparse.Namespace) -> Path:
    return Path(args.socket or os.environ.get("DGX_AUTONOMY_SOCKET") or default_socket_path())


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
        return
    if isinstance(obj, dict):
        width = max((len(k) for k in obj), default=0)
        for key, value in obj.items():
            if isinstance(value, dict | list):
                value = json.dumps(value, default=str)
            print(f"{key.ljust(width)}  {value}")
    else:
        print(obj)


def _budget(value: str) -> float:
    hours = float(value)
    if not 0 < hours <= MAX_BUDGET_HOURS:
        raise argparse.ArgumentTypeError(f"budget must be in (0, {MAX_BUDGET_HOURS:g}] hours")
    return hours


def cmd_launch(args: argparse.Namespace) -> int:
    brief = Path(args.brief)
    # The controller runs in a container and cannot read the operator's files, so
    # the CLI sends the brief's contents; the controller stores its own copy.
    text = brief.read_text()
    result = call(
        _socket(args),
        "launch",
        {
            "brief_text": text,
            "brief_source": str(brief.resolve()),
            "model_key": args.model,
            "budget_hours": args.budget_hours,
        },
    )
    _print(result, args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = call(_socket(args), "status", {"run_id": args.run_id} if args.run_id else {})
    if args.json:
        _print(result, True)
        return 0
    ops = result.pop("operations", [])
    last = result.pop("last_event", None)
    _print(result, False)
    for op in ops:
        line = f"  {op['kind']:<20} {op['status']}"
        if op.get("error"):
            line += f"  {op['error']}"
        print(line)
    if last:
        print(f"last_event  [{last['kind']}/{last['source']}] {last['text']}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    runs = call(_socket(args), "runs")
    if args.json:
        _print(runs, True)
        return 0
    for r in runs:
        print(f"{r['run_id']}  {r['phase']:<9} {r['model_key']}  deadline {r['deadline_at']}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    since = args.since
    while True:
        page = call(_socket(args), "logs", {"run_id": args.run_id, "since": since})
        for ev in page["events"]:
            if args.json:
                print(json.dumps(ev))
            else:
                print(f"{ev['timestamp']}  {ev['kind']:<22} {ev['source']:<11} {ev['text']}")
        since = page["next"]
        if not args.follow:
            return 0
        if not page["events"] and page["phase"] in ("finished", "failed"):
            return 0
        sys.stdout.flush()
        time.sleep(args.interval)


def cmd_inference(args: argparse.Namespace) -> int:
    if args.action == "up":
        result = call(_socket(args), "inference.ensure", {"model_key": args.model}, timeout=300)
    else:
        result = call(_socket(args), "inference.status")
    _print(result, args.json)
    return 0


def cmd_controller(_args: argparse.Namespace) -> int:
    from .controller import serve

    serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dgx-autonomy", description=__doc__)
    p.add_argument("--socket", help="control socket (default: $DGX_AUTONOMY_SOCKET)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("launch", help="start an unattended run from a brief file")
    s.add_argument("--brief", required=True, help="markdown file with the agreed brief")
    s.add_argument("--model", default=None, help="model key from models.yaml (default: default)")
    s.add_argument(
        "--budget-hours", type=_budget, default=MAX_BUDGET_HOURS, help="wall-clock budget (≤40)"
    )
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("status", help="phase, deadline and last event of a run")
    s.add_argument("run_id", nargs="?", help="run id (default: the latest run)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("runs", help="list runs")
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("logs", help="summarized OpenHands events of a run")
    s.add_argument("run_id")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--since", type=int, default=0, help="skip the first N events")
    s.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("inference", help="owned llama-server: status, or start it")
    s.add_argument("action", choices=["status", "up"], nargs="?", default="status")
    s.add_argument("--model", default=None)
    s.set_defaults(func=cmd_inference)

    s = sub.add_parser("controller", help="run the controller daemon (inside its container)")
    s.set_defaults(func=cmd_controller)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ControlError as exc:
        print(f"dgx-autonomy: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"dgx-autonomy: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
