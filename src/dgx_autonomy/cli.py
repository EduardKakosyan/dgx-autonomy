"""`dgx-autonomy`: the operator's SSH interface. It never runs work itself.

Every subcommand except `controller` is a thin client of the control socket. The
unattended run lives in the controller container, so closing the SSH session or the
laptop changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import frozen
from .config import default_socket_path
from .control_api import ControlError, call

MAX_BUDGET_HOURS = 40.0
DEFAULT_SSH_HOST = "hugo-dgx1"
TERMINAL_PHASES = ("finished", "failed", "stopped")
# Root-owned copy of reservation.py; host/sudoers-autonomy lets jim run it via sudo.
RESERVATION_HELPER = "/usr/local/sbin/dgx-autonomy-reservation"
HELPER_REFUSED = 2

HelperRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=900)


class HelperError(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def reservation_helper(action: str, runner: HelperRunner = _run) -> dict[str, Any]:
    """Run the root reservation helper through sudo; its JSON answer, or HelperError."""
    helper = os.environ.get("DGX_AUTONOMY_RESERVATION_HELPER") or RESERVATION_HELPER
    proc = runner(["sudo", "-n", helper, action])
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout).strip()
        raise HelperError(
            f"cannot run the reservation helper ({detail or f'exit {proc.returncode}'})."
            " The operator installs it with `sudo host/install.sh` (README, Phase 3)."
        ) from None
    if not isinstance(out, dict):
        raise HelperError(f"reservation helper: unexpected answer {out!r}")
    if proc.returncode == HELPER_REFUSED:
        raise HelperError(str(out.get("refused", out)), HELPER_REFUSED)
    if proc.returncode != 0:
        raise HelperError(str(out.get("error", out)))
    return out


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
    """Launch from a brief file, or a directory with brief.md and checks/, or a plan."""
    if getattr(args, "plan", None):
        return _launch_plan(args)
    if not args.brief:
        print("dgx-autonomy: launch needs --brief or --plan", file=sys.stderr)
        return 2
    brief = Path(args.brief)
    # The controller runs in a container and cannot read the operator's files, so
    # the CLI sends their contents, and the digest of what it read. The controller
    # freezes its own copy and refuses the launch unless the digests agree.
    try:
        text, checks = frozen.read_bundle(brief)
        criteria = frozen.validate(text.encode(), frozen.encode(checks))
    except frozen.FrozenError as exc:
        print(f"dgx-autonomy: {exc}", file=sys.stderr)
        return 1
    result = call(
        _socket(args),
        "launch",
        {
            "brief_text": text,
            "checks": checks,
            "bundle_digest": frozen.bundle_digest(text.encode(), frozen.encode(checks)),
            "brief_source": str(brief.resolve()),
            "model_key": args.model,
            "budget_hours": args.budget_hours,
        },
    )
    if args.json:
        _print(result, True)
        return 0
    print(f"run_id         {result['run_id']}")
    print(f"deadline_at    {result['deadline_at']}")
    print(f"frozen_digest  {result['frozen_digest']}")
    if not criteria:
        print("criteria       none: completion will be the builder's claim only")
    for c in criteria:
        what = f"{c.runner} checks/{c.test}" if c.kind == "automated" else "human judgment"
        optional = "" if c.required else " (optional)"
        print(f"  {c.key:<24} {what}{optional}")
    return 0


def _launch_plan(args: argparse.Namespace) -> int:
    """Launch a plan's draft without the REPL: show it, confirm, freeze that digest."""
    from .plan_repl import PlanSession

    sock = _socket(args)

    def answer(prompt: str) -> str:
        if not args.yes:
            return input(prompt)
        if "force" in prompt:  # the draft's checks have not passed a dry run
            return "force" if args.skip_dry_run else ""
        return "y"

    session = PlanSession(
        lambda op, a=None, **kw: call(sock, op, a, **kw), args.plan, out=sys.stdout, read=answer
    )
    return 0 if session.launch(str(args.budget_hours)) else 1


def cmd_plan(args: argparse.Namespace) -> int:
    """Plan a run with the model: start a session, or attach to one."""
    from .plan_repl import PlanSession

    sock = _socket(args)
    if args.close:
        result = call(sock, "plan.close", {"plan_id": args.close})
        _print(result, args.json)
        return 0
    if args.attach is not None:
        plan_id = args.attach or call(sock, "plan.status", {})["plan_id"]
    else:
        request = args.request
        if not request:
            print("What do you want to build? (end with an empty line)")
            lines: list[str] = []
            while (line := input("> " if not lines else "  ")).strip():
                lines.append(line)
            request = "\n".join(lines)
        started = call(sock, "plan.start", {"request": request, "model_key": args.model})
        plan_id = started["plan_id"]
    session = PlanSession(lambda op, a=None, **kw: call(sock, op, a, **kw), plan_id, out=sys.stdout)
    return session.run()


def cmd_plans(args: argparse.Namespace) -> int:
    plans = call(_socket(args), "plans")
    if args.json:
        _print(plans, True)
        return 0
    for p in plans:
        run = f"  -> run {p['run_id']}" if p.get("run_id") else ""
        print(f"{p['plan_id']}  {p['state']:<9} {p['model_key']}{run}  {p['request'][:60]}")
    return 0


def _print_status(result: dict[str, Any]) -> None:
    ops = result.pop("operations", [])
    last = result.pop("last_event", None)
    demo = result.pop("demo", None)
    evidence = result.pop("stop_evidence", None)
    recoveries = result.pop("recoveries", [])
    containers = result.pop("containers", None)
    evaluation = result.pop("evaluation", None)
    criteria = result.pop("criteria", None)
    conversations = result.pop("conversations", None) or []
    blocked = result.pop("blocked", None)
    _print(result, False)
    for op in ops:
        line = f"  {op['kind']:<20} {op['status']}"
        if op.get("error"):
            line += f"  {op['error']}"
        print(line)
    for c in containers or []:
        print(f"container   {c['name']} {c['status']}")
    for rec in recoveries:
        line = f"recovery #{rec['n']} {rec['status']} ({rec['started_at']}): {rec['cause']}"
        if rec.get("steps", {}).get("resumed_from"):
            line += f"; conversation resumed from {rec['steps']['resumed_from']}"
        print(line)
        if rec.get("error"):
            print(f"            {rec['error']}")
    if last:
        print(f"last_event  [{last['kind']}/{last['source']}] {last['text']}")
    if demo:
        live = "listening" if demo.get("listening") else "not listening"
        print(f"demo        {demo['state']} ({live}) at {demo['url']} on the DGX")
        if demo.get("command"):
            print(f"            command: {demo['command']}")
        if demo.get("message") and demo["state"] != "running":
            print(f"            {demo['message']}")
    if criteria and (criteria["automated"] or criteria["human_judgment"]):
        print(
            f"criteria    {criteria['automated']} automated,"
            f" {criteria['human_judgment']} awaiting human judgment"
        )
    if evaluation:
        print(
            f"evaluation  #{evaluation['n']} ({evaluation['trigger']}) {evaluation['status']}"
            + (f": {evaluation['detail']}" if evaluation.get("detail") else "")
        )
        for key, r in evaluation["results"].items():
            print(f"            {r['status']:<8} {key}  {r.get('summary') or ''}")
    if len(conversations) > 1 or any(c["status"] != "active" for c in conversations):
        chain = ", ".join(f"#{c['n']} {c['status']} ({c['reason']})" for c in conversations)
        print(f"conversations {chain}")
    if blocked:
        b = blocked["blocker"]
        print(f"blocked     {b['missing_capability']}")
        print(f"            needed: {b['needed']}; tried: {'; '.join(b['alternatives_tried'])}")
    if evidence:
        verdict = "NOT VERIFIED" if evidence["failed"] else "verified"
        print(
            f"stop        {evidence['reason']}: agent execution ended {verdict};"
            f" {len(evidence['after_pause'])} agent processes after pause"
            f" ({evidence['tool_processes_after_pause']} from tools),"
            f" survivors {evidence['survivors']}"
        )
        for note in evidence.get("notes") or []:
            print(f"            {note}")


def cmd_status(args: argparse.Namespace) -> int:
    result = call(_socket(args), "status", {"run_id": args.run_id} if args.run_id else {})
    if args.json:
        _print(result, True)
    else:
        _print_status(result)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """End agent execution now. The demo, if the agent started one, stays up."""
    result = call(_socket(args), "stop", {"run_id": args.run_id}, timeout=600)
    if args.json:
        _print(result, True)
        return 0
    message = result.pop("message", None)
    if message:
        print(message)
    evidence = result.get("stop_evidence") or {}
    _print_status(result)
    return 1 if evidence.get("failed") else 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    """Print the SSH local-forward command that reaches the run's demo from a laptop."""
    status = call(_socket(args), "status", {"run_id": args.run_id} if args.run_id else {})
    demo = status.get("demo")
    if not demo:
        print(f"dgx-autonomy: run {status['run_id']} has no demo port yet", file=sys.stderr)
        return 1
    host_port = int(demo["host_port"])
    local = args.local_port or host_port
    host = args.ssh_host or os.environ.get("DGX_AUTONOMY_SSH_HOST") or DEFAULT_SSH_HOST
    print(f"ssh -N -L {local}:127.0.0.1:{host_port} {host}")
    live = "listening" if demo.get("listening") else f"{demo['state']}, not listening yet"
    print(
        f"# run on the laptop, then open http://127.0.0.1:{local}/  (demo: {live})",
        file=sys.stderr,
    )
    return 0


def _indent(text: str, prefix: str = "      ") -> str:
    return prefix + str(text).replace("\n", "\n" + prefix)


def print_report(r: dict[str, Any]) -> None:
    """The report, one section per kind of evidence. Nothing is merged across them."""
    print(f"run       {r['run_id']}  {r['phase']} ({r['outcome'] or '-'})")
    print(f"deadline  {r['deadline_at']}")
    print(f"result    {'VERIFIED' if r['verified'] else 'NOT VERIFIED'}: {r['verdict']}")
    fz = r["frozen"]
    if fz["digest"]:
        print(f"frozen    {fz['digest']} ({'intact' if fz['intact'] else fz['problem']})")

    print("\nBuilder claims (what the builder said; not evidence of anything)")
    if not r["claims"]:
        print("  none")
    for c in r["claims"]:
        results = ", ".join(f"#{e['n']} {e['status']}" for e in c["evaluations"]) or "-"
        print(f"  claimed: {c['text']}")
        print(f"      evaluations: {results}")

    print("\nAutomated acceptance checks (frozen at launch, run by the evaluator)")
    if not r["automated"]:
        print("  none agreed")
    for c in r["automated"]:
        optional = "" if c["required"] else " (optional)"
        print(f"  {str(c['status']).upper():<14} {c['key']}{optional}: {c['description']}")
        if c["evaluation"] is not None:
            history = " -> ".join(f"#{h['evaluation']} {h['status']}" for h in c["history"])
            print(f"      evaluation #{c['evaluation']}, snapshot {str(c['snapshot'])[:12]};"
                  f" history {history}")  # fmt: skip
        if c["status"] != "passed" and c.get("summary"):
            print(f"      {c['summary']}")
            if c.get("excerpt"):
                print(_indent(c["excerpt"][:800], "        "))

    print("\nAwaiting human judgment (never counted as passes)")
    if not r["human_judgment"]:
        print("  none")
    for c in r["human_judgment"]:
        print(f"  {c['key']}: {c['description']}")

    print("\nEvaluations")
    if not r["evaluations"]:
        print("  none")
    for e in r["evaluations"]:
        relaunched = ", demo relaunched" if e["demo_relaunched"] else ""
        delivered = ", failures sent to the builder" if e["delivered_at"] else ""
        print(
            f"  #{e['n']} {e['trigger']:<9} {e['status']:<12} snapshot"
            f" {str(e['snapshot'])[:12]}{relaunched}{delivered}"
        )
        if e.get("detail"):
            print(f"      {e['detail']}")
        changes = e.get("changes_since_previous")
        if changes:
            print(f"      changed since the previous evaluation: {', '.join(changes[:10])}")
        print(f"      evidence: {e['evidence_dir']}")

    if r.get("blocked"):
        b = r["blocked"]["blocker"]
        print("\nBlocked (declared, then confirmed by a fresh conversation)")
        print(f"  missing: {b['missing_capability']}")
        for alt in b["alternatives_tried"]:
            print(f"  tried:   {alt}")
        print(f"  needed:  {b['needed']}")

    conversations = r.get("conversations") or []
    if len(conversations) > 1:
        print("\nConversations (fresh ones continue from checkpoints)")
        for c in conversations:
            print(f"  #{c['n']} {c['status']:<9} {c['reason']:<15} from {c['started_at']}")
        for k in r.get("checkpoints") or []:
            problems = f"; {'; '.join(k['problems'])}" if k["problems"] else ""
            print(f"  checkpoint #{k['n']} {k['source']} ({k['reason']}){problems}")

    print("\nStronger-model reviews (requested manually; separate from the checks)")
    if not r["reviews"]:
        print("  none requested")
    for rv in r["reviews"]:
        who = f" by {rv['reviewer']}" if rv["reviewer"] else ""
        print(f"  #{rv['n']} {rv['status']}{who} ({rv['requested_at']}): {rv['bundle_dir']}")
        if rv.get("result_excerpt"):
            print(_indent(rv["result_excerpt"][:800]))


def cmd_report(args: argparse.Namespace) -> int:
    result = call(_socket(args), "report", {"run_id": args.run_id} if args.run_id else {})
    if args.json:
        _print(result, True)
    else:
        print_report(result)
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Run the frozen checks again on a run that has ended."""
    result = call(_socket(args), "evaluate", {"run_id": args.run_id})
    if args.json:
        _print(result, True)
    else:
        print(f"evaluation #{result['n']} started; `dgx-autonomy report {args.run_id}` shows it")
    return 0


def cmd_review_request(args: argparse.Namespace) -> int:
    """Bundle a run for a stronger-model review that the operator runs by hand."""
    request = {"run_id": args.run_id} | ({"note": args.note} if args.note else {})
    result = call(_socket(args), "review.request", request, timeout=600)
    if args.json:
        _print(result, True)
    else:
        print(f"review #{result['n']}: bundle in {result['bundle_dir']} (see its README.md)")
    return 0


def cmd_review_record(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text()
    result = call(
        _socket(args),
        "review.record",
        {"run_id": args.run_id, "n": args.n, "reviewer": args.reviewer, "text": text},
    )
    _print(result, args.json)
    return 0


def cmd_rollover(args: argparse.Namespace) -> int:
    """Replace the run's conversation with a fresh one that continues from a checkpoint."""
    request = {"run_id": args.run_id} | ({"detail": args.detail} if args.detail else {})
    result = call(_socket(args), "rollover", request)
    if args.json:
        _print(result, True)
    else:
        print(
            f"conversation #{result['n']} requested; the current one is asked for a handoff"
            f" (`dgx-autonomy checkpoints {args.run_id}` shows the chain)"
        )
    return 0


def cmd_checkpoints(args: argparse.Namespace) -> int:
    result = call(_socket(args), "checkpoints", {"run_id": args.run_id} if args.run_id else {})
    if args.json:
        _print(result, True)
        return 0
    print(f"run {result['run_id']}")
    for c in result["conversations"]:
        detail = f": {c['detail']}" if c.get("detail") else ""
        print(f"conversation #{c['n']} {c['status']} ({c['reason']}{detail[:160]})")
        print(f"    {c['conversation_id']}, from checkpoint {c['from_checkpoint'] or '-'}")
    for k in result["checkpoints"]:
        print(
            f"\ncheckpoint #{k['n']} {k['source']} ({k['reason']}) from conversation"
            f" {k['from_conversation'][:8]} at event {k['event_position']}, snapshot"
            f" {str(k['workspace_sha'])[:12]}, supersedes {k['supersedes'] or '-'}"
        )
        for problem in k["problems"]:
            print(f"    problem: {problem}")
        print(_indent(json.dumps(k["handoff"], indent=2)[:4000], "    "))
        print(f"    verified by the environment: {json.dumps(k['verified'])[:600]}")
    return 0


def _print_qualification(q: dict[str, Any]) -> None:
    print(f"qualification of {q['model_key']}: {q['status']} (step {q['step']})")
    for name, step in q.get("steps", {}).items():
        mark = "ok  " if step.get("ok") else ("…   " if step.get("ok") is None else "FAIL")
        facts = {k: v for k, v in step.items() if k not in ("ok", "probes", "error")}
        print(f"  {mark} {name:<12} {json.dumps(facts, default=str)[:180]}")
        for probe in step.get("probes") or []:
            mark = "ok  " if probe["ok"] else "FAIL"
            print(f"         {mark} {probe['probe']}: {probe['detail'][:120]}")
        if step.get("error"):
            print(f"         {step['error']}")
    if q.get("memory"):
        print(f"  memory {json.dumps(q['memory'])}")
    for failure in q.get("failures") or []:
        print(f"  failure: {failure}")
    print(f"  record: {q.get('path')}")


def cmd_qualify(args: argparse.Namespace) -> int:
    """Qualify a model with the whole workload running; follow it to the verdict."""
    sock = _socket(args)
    job = call(sock, "qualify.start", {"model_key": args.model})
    if args.json and not args.follow:
        _print(job, True)
        return 0
    last_step = None
    while job["status"] == "running":
        if job["step"] != last_step:
            print(f"{time.strftime('%H:%M:%S')} {job['step']}…", flush=True)
            last_step = job["step"]
        time.sleep(args.interval)
        job = call(sock, "qualify.status", {"model_key": job["model_key"]})
    if args.json:
        _print(job, True)
    else:
        _print_qualification(job)
    return 0 if job["status"] == "qualified" else 1


def cmd_qualification(args: argparse.Namespace) -> int:
    request = {"model_key": args.model} if args.model else {}
    job = call(_socket(args), "qualify.status", request)
    if args.json:
        _print(job, True)
    else:
        _print_qualification(job)
    return 0


def cmd_readiness(args: argparse.Namespace) -> int:
    """Run every target-host smoke test in sequence and record one report."""
    from .readiness import PACKAGE_ROOT, run_readiness

    status = call(_socket(args), "inference.status")
    out = Path(args.out or PACKAGE_ROOT / "readiness" / time.strftime("%Y%m%dT%H%M%S"))
    report = run_readiness(
        out,
        model_key=status.get("model_key"),
        with_reservation=args.with_reservation,
        only=args.only or (),
    )
    print(
        f"readiness {'PASSED' if report['passed'] else 'NOT PASSED'}; report: {out / 'report.md'}"
    )
    return 0 if report["passed"] else 1


def cmd_retire(args: argparse.Namespace) -> int:
    """Take ended runs' retained demos down (their sandboxes); the records stay."""
    sock = _socket(args)
    ids = list(args.run_ids)
    if args.all_ended:
        ids += [r["run_id"] for r in call(sock, "runs") if r["phase"] in TERMINAL_PHASES]
    if not ids:
        print("dgx-autonomy: name runs to retire, or --all-ended", file=sys.stderr)
        return 2
    for run_id in ids:
        result = call(sock, "retire", {"run_id": run_id})
        print(f"{run_id}  {'sandbox removed' if result['sandbox_removed'] else 'no sandbox'}")
    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    result = call(_socket(args), "processes", {"run_id": args.run_id} if args.run_id else {})
    if args.json:
        _print(result, True)
        return 0
    if not result["sandbox_running"]:
        print(f"the sandbox of {result['run_id']} is not running")
        return 0
    for p in result["processes"]:
        print(f"{p['pid']:>7} {p['sid']:>7} {p['role']:<10} {p['state']} {p['cmd']}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    runs = call(_socket(args), "runs")
    if args.json:
        _print(runs, True)
        return 0
    for r in runs:
        outcome = r.get("outcome") or "-"
        print(
            f"{r['run_id']}  {r['phase']:<9} {outcome:<8} {r['model_key']}"
            f"  deadline {r['deadline_at']}"
        )
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
        if not page["events"] and page["phase"] in TERMINAL_PHASES:
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


def cmd_reserve(args: argparse.Namespace) -> int:
    """Displace claude-qwen, then start the environment's own llama-server."""
    out = reservation_helper("reserve")
    record = out.get("record") or {}
    if args.json:
        _print(out, True)
    elif out.get("already"):
        print(f"the reservation is already held (since {record.get('held_since')})")
    else:
        print(
            f"reserved: {record.get('service')} stopped; its clients now get a busy notice"
            f" at {out.get('notice')}"
        )
        gib = (record.get("required_available_bytes") or 0) / 1024**3
        print(f"release will need {gib:.1f} GiB available to start it again")
    if args.no_inference:
        return 0
    result = call(_socket(args), "inference.ensure", {"model_key": args.model}, timeout=300)
    if not args.json:
        print(f"owned llama-server: {result['container']}, {result['health']}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """Stop the environment's llama-server and give the DGX back to claude-qwen."""
    stopped = call(_socket(args), "inference.stop", timeout=300)
    if not args.json:
        print(f"owned llama-server {'removed' if stopped['removed'] else 'was not running'}")
    try:
        out = reservation_helper("release")
    except HelperError as exc:
        if exc.code != HELPER_REFUSED:
            raise
        print(f"dgx-autonomy: release refused: {exc}", file=sys.stderr)
        return HELPER_REFUSED
    if args.json:
        _print(out, True)
    elif out.get("already"):
        print("no reservation was held")
    else:
        print(f"released: claude-qwen is {out.get('service_active')} ({out.get('health')})")
        for m in out.get("mismatches") or []:
            print(f"  warning: {m}")
    return 0 if out.get("already") or out.get("configuration_restored") else 1


def cmd_reservation(args: argparse.Namespace) -> int:
    _print(reservation_helper("status"), args.json)
    return 0


def _target(value: str) -> list[Any]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return [host, int(port)]


def cmd_egress(args: argparse.Namespace) -> int:
    """The agent's network boundary: is it in place, and what can the agent reach."""
    if not args.targets:
        policy = call(_socket(args), "network.policy")
        if args.json:
            _print(policy, True)
        else:
            print("egress policy " + ("in place" if policy["ok"] else "NOT in place"))
            for problem in policy["problems"]:
                print(f"  {problem}")
        return 0 if policy["ok"] else 1
    result = call(_socket(args), "network.probe", {"targets": args.targets}, timeout=900)
    if args.json:
        _print(result, True)
        return 0
    print(f"from the agent's network (gateway {result.get('gateway')}):")
    for r in result["results"]:
        verdict = "reachable" if r["ok"] else f"blocked  {r['error']}"
        print(f"  {r['host']}:{r['port']:<6} {verdict}  ({r['seconds']}s)")
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

    s = sub.add_parser("launch", help="start an unattended run from a brief (and its checks)")
    source = s.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--brief",
        help="a brief.md file, or a directory with brief.md and checks/ (criteria.yaml + checks)",
    )
    source.add_argument("--plan", help="a plan id: freeze its draft (shown first)")
    s.add_argument("--yes", action="store_true", help="with --plan: do not ask to confirm")
    s.add_argument(
        "--skip-dry-run",
        action="store_true",
        help="with --plan --yes: launch even if the draft checks have not passed a dry run",
    )
    s.add_argument("--model", default=None, help="model key from models.yaml (default: default)")
    s.add_argument(
        "--budget-hours", type=_budget, default=MAX_BUDGET_HOURS, help="wall-clock budget (≤40)"
    )
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("plan", help="plan a run with the model (interactive)")
    s.add_argument("--request", help="what to build (asked for when missing)")
    s.add_argument("--model", default=None, help="model key from models.yaml (default: default)")
    s.add_argument(
        "--attach", nargs="?", const="", default=None, metavar="PLAN_ID",
        help="continue a plan (default: the latest open one)",
    )  # fmt: skip
    s.add_argument("--close", metavar="PLAN_ID", help="abandon a plan")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("plans", help="list planning sessions")
    s.set_defaults(func=cmd_plans)

    s = sub.add_parser("status", help="phase, deadline, demo and last event of a run")
    s.add_argument("run_id", nargs="?", help="run id (default: the latest run)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stop", help="end agent execution now; the demo stays up")
    s.add_argument("run_id", help="run id")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("report", help="claims, check results, human judgment, reviews")
    s.add_argument("run_id", nargs="?", help="run id (default: the latest run)")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("evaluate", help="run the frozen checks again on an ended run")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_evaluate)

    s = sub.add_parser("review-request", help="bundle a run for a manual stronger-model review")
    s.add_argument("run_id")
    s.add_argument("--note", help="what the reviewer should look at")
    s.set_defaults(func=cmd_review_request)

    s = sub.add_parser("review-record", help="attach the result of a manual review")
    s.add_argument("run_id")
    s.add_argument("n", type=int, help="review number")
    s.add_argument("--reviewer", required=True, help="the model or person that reviewed")
    s.add_argument("--file", required=True, help="the review, as text")
    s.set_defaults(func=cmd_review_record)

    s = sub.add_parser("tunnel", help="print the ssh command that forwards the demo port")
    s.add_argument("run_id", nargs="?", help="run id (default: the latest run)")
    s.add_argument("--ssh-host", help=f"ssh host of the DGX (default: {DEFAULT_SSH_HOST})")
    s.add_argument("--local-port", type=int, help="laptop port (default: the DGX port)")
    s.set_defaults(func=cmd_tunnel)

    s = sub.add_parser("rollover", help="continue a run in a fresh conversation (checkpoint)")
    s.add_argument("run_id")
    s.add_argument("--detail", help="why, for the record and the next conversation")
    s.set_defaults(func=cmd_rollover)

    s = sub.add_parser("checkpoints", help="a run's conversations and checkpoint chain")
    s.add_argument("run_id", nargs="?", help="run id (default: the latest run)")
    s.set_defaults(func=cmd_checkpoints)

    s = sub.add_parser("qualify", help="qualify a model with the whole workload running")
    s.add_argument("model", help="model key from models.yaml")
    s.add_argument("--no-follow", dest="follow", action="store_false", help="start and return")
    s.add_argument("--interval", type=float, default=15.0, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_qualify)

    s = sub.add_parser("qualification", help="the latest qualification result")
    s.add_argument("model", nargs="?", help="model key (default: the latest of any)")
    s.set_defaults(func=cmd_qualification)

    s = sub.add_parser("readiness", help="run every target-host smoke test; record a report")
    s.add_argument("--only", nargs="*", help="run only suites whose path or name contains this")
    s.add_argument(
        "--with-reservation",
        action="store_true",
        help="also test reserve/release (stops claude-qwen for a few seconds)",
    )
    s.add_argument("--out", help="report directory (default: readiness/<time>/ in the checkout)")
    s.set_defaults(func=cmd_readiness)

    s = sub.add_parser("retire", help="take ended runs' demos down (removes their sandboxes)")
    s.add_argument("run_ids", nargs="*")
    s.add_argument("--all-ended", action="store_true", help="every run that has ended")
    s.set_defaults(func=cmd_retire)

    s = sub.add_parser("ps", help="processes in a run's sandbox, by role")
    s.add_argument("run_id", nargs="?", help="run id (default: the latest run)")
    s.set_defaults(func=cmd_ps)

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

    s = sub.add_parser("reserve", help="displace claude-qwen and start the owned llama-server")
    s.add_argument("--model", default=None, help="model key for the owned llama-server")
    s.add_argument(
        "--no-inference", action="store_true", help="only displace claude-qwen; start nothing"
    )
    s.set_defaults(func=cmd_reserve)

    s = sub.add_parser("release", help="stop the owned llama-server and restore claude-qwen")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("reservation", help="is the DGX inference reserved, and for how long")
    s.set_defaults(func=cmd_reservation)

    s = sub.add_parser("egress", help="check the agent's network boundary, or probe through it")
    s.add_argument("targets", nargs="*", type=_target, help="HOST:PORT to try from the agent")
    s.set_defaults(func=cmd_egress)

    s = sub.add_parser("controller", help="run the controller daemon (inside its container)")
    s.set_defaults(func=cmd_controller)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ControlError, HelperError) as exc:
        print(f"dgx-autonomy: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"dgx-autonomy: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
