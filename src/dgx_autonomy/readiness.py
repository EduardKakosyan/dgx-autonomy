"""`dgx-autonomy readiness`: every target-host smoke test, in sequence, in one report.

It runs as the operator on the DGX, from the deployed checkout, and drives the real
controller through the tests in tests/dgx (which use the control socket, like the
CLI). Each suite runs in its own pytest process with a JUnit report; the readiness
report (JSON and Markdown, under `readiness/<time>/`) lists every suite with its
result, duration and failure text, and the model that served them.

The DGX restart checks (resume after a reboot, expiry while offline) cannot be driven
from here: tests/dgx/test_reboot.md is their checklist, and the report lists them as
manual. `test_reserve_release.py` stops claude-qwen for a few seconds, so it runs only
with --with-reservation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

SUITES: tuple[tuple[str, str], ...] = (
    ("tool calls through llama.cpp", "tests/dgx/test_tool_calls.py"),
    ("a trivial brief end to end", "tests/dgx/test_trivial_run.py"),
    ("the agent's network boundary", "tests/dgx/test_egress.py"),
    ("deadline stop keeps the demo", "tests/dgx/test_stop_retains_demo.py"),
    ("controller and whole-stack crashes", "tests/dgx/test_controller_kill.py"),
    ("protected acceptance checks", "tests/dgx/test_protected_eval.py"),
    ("planning to launch", "tests/dgx/test_plan_to_launch.py"),
    ("forced context reset", "tests/dgx/test_forced_reset.py"),
)
RESERVATION_SUITE = ("inference reservation and release", "tests/dgx/test_reserve_release.py")
MANUAL = (
    "resume after a DGX reboot (tests/dgx/test_reboot.md)",
    "deadline passing while the DGX is down (tests/dgx/test_reboot.md)",
)

Runner = Callable[[Sequence[str], dict[str, str]], tuple[int, str]]


def run_pytest(argv: Sequence[str], env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(
        list(argv), cwd=PACKAGE_ROOT, env=env, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


@dataclass(frozen=True)
class SuiteResult:
    name: str
    path: str
    status: str  # passed | failed | error
    tests: int
    failures: int
    errors: int
    skipped: int
    seconds: float
    detail: str = ""


def read_junit(path: Path) -> tuple[int, int, int, int, str]:
    """tests, failures, errors, skipped, and the first failure's text."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return 0, 0, 1, 0, f"no JUnit report at {path}"
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = [sum(int(s.get(k, "0") or 0) for s in suites)
              for k in ("tests", "failures", "errors", "skipped")]  # fmt: skip
    first = ""
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is not None:
            first = f"{case.get('name')}: {bad.get('message') or ''}\n{(bad.text or '')[-1500:]}"
            break
    return counts[0], counts[1], counts[2], counts[3], first.strip()


def run_readiness(
    out_dir: Path,
    *,
    model_key: str | None,
    with_reservation: bool = False,
    only: Sequence[str] = (),
    runner: Runner = run_pytest,
    now: Callable[[], datetime] = datetime.now,
    echo: Callable[[str], None] = print,
) -> dict[str, Any]:
    suites = list(SUITES) + ([RESERVATION_SUITE] if with_reservation else [])
    if only:
        suites = [s for s in suites if any(o in s[1] or o in s[0] for o in only)]
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if with_reservation:
        env["DGX_AUTONOMY_RESERVATION_TEST"] = "1"
    started = now()
    results: list[SuiteResult] = []
    for name, path in suites:
        junit = out_dir / f"{Path(path).stem}.xml"
        echo(f"-> {name} ({path})")
        t0 = time.monotonic()
        code, output = runner(
            [sys.executable, "-m", "pytest", "-m", "dgx", path, "-q", "-s",
             f"--junitxml={junit}"],
            env,
        )  # fmt: skip
        seconds = round(time.monotonic() - t0, 1)
        (out_dir / f"{Path(path).stem}.log").write_text(output)
        tests, failures, errors, skipped, first = read_junit(junit)
        if tests and not failures and not errors and code == 0:
            status = "passed"
        elif failures:
            status = "failed"
        else:
            status = "error"
        detail = first or (output[-1500:] if status != "passed" else "")
        results.append(
            SuiteResult(name, path, status, tests, failures, errors, skipped, seconds, detail)
        )
        echo(f"   {status} ({tests} tests, {seconds:.0f}s)")
    report = {
        "started_at": started.isoformat(),
        "finished_at": now().isoformat(),
        "model_key": model_key,
        "passed": all(r.status == "passed" for r in results) and bool(results),
        "suites": [asdict(r) for r in results],
        "not_run": [] if with_reservation else [RESERVATION_SUITE[1] + " (--with-reservation)"],
        "manual": list(MANUAL),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    (out_dir / "report.md").write_text(render(report))
    return report


def render(report: dict[str, Any]) -> str:
    lines = [
        "# Readiness report",
        "",
        f"- model: `{report['model_key']}`",
        f"- from {report['started_at']} to {report['finished_at']}",
        f"- result: **{'PASSED' if report['passed'] else 'NOT PASSED'}**",
        "",
        "| suite | result | tests | time |",
        "|---|---|---|---|",
    ]
    for s in report["suites"]:
        lines.append(
            f"| {s['name']} (`{s['path']}`) | {s['status']} | {s['tests']} | {s['seconds']:.0f}s |"
        )
    for s in report["suites"]:
        if s["status"] != "passed":
            lines += ["", f"## {s['name']}: {s['status']}", "", "```", s["detail"], "```"]
    if report["not_run"]:
        lines += ["", "Not run: " + "; ".join(report["not_run"])]
    lines += ["", "Manual (not driven by this command):"]
    lines += [f"- {m}" for m in report["manual"]]
    return "\n".join(lines) + "\n"
