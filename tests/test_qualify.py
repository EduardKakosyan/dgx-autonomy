"""Model qualification and the readiness report, without a DGX."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy.config import PACKAGED_MODELS_FILE, Settings, load_models
from dgx_autonomy.controller import RequestError
from dgx_autonomy.qualify import (
    GIB,
    TOOL_PROBES,
    Hooks,
    Qualifier,
    judge_probe,
    long_prompt,
    read_meminfo,
)
from dgx_autonomy.readiness import SUITES, read_junit, run_readiness

from fakes import FakeClock, Harness


def _tool_answer(name: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = {"name": name, "arguments": json.dumps(args)}
    call = {"id": "c", "type": "function", "function": fn}
    return {"choices": [{"message": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}


GOOD_ARGS: dict[str, tuple[str, dict[str, Any]]] = {
    "single argument": ("get_weather", {"city": "Halifax"}),
    "choose between tools": ("add_numbers", {"a": 17, "b": 25}),
    "nested array of objects": (
        "record_plan",
        {
            "items": [
                {"title": "write the brief", "status": "done"},
                {"title": "build the page", "status": "todo"},
            ]
        },
    ),
    "shell command": ("run_command", {"command": "ls -la /tmp"}),
}


def _good_chat(body: dict[str, Any], timeout: float) -> tuple[int, Any]:
    content = str(body["messages"][-1].get("content") or "")
    if "secret word" in content:
        return 200, {
            "choices": [{"message": {"content": "quartzlight"}}],
            "usage": {"prompt_tokens": 49000},
            "timings": {"prompt_per_second": 812.4, "predicted_per_second": 48.2},
        }
    if body["messages"][-1]["role"] == "tool":
        return 200, {"choices": [{"message": {"content": "It is 14 C and foggy."}}]}
    for probe in TOOL_PROBES:
        if probe.messages == body["messages"]:
            name, args = GOOD_ARGS[probe.name]
            return 200, _tool_answer(name, args)
    return 500, "unexpected"


class FakeHost:
    def __init__(self, available_gib: float = 100) -> None:
        self.available = int(available_gib * GIB)
        self.swap_used = 0
        self.serving: str | None = "qwen3.6-35b-a3b"
        self.removed = 0
        self.ensured: list[str] = []
        self.launched: list[dict[str, Any]] = []
        self.active: list[str] = []
        self.verified = True
        self.chat = _good_chat

    def meminfo(self) -> dict[str, int]:
        return {"MemAvailable": self.available, "SwapTotal": 8 * GIB,
                "SwapFree": 8 * GIB - self.swap_used}  # fmt: skip

    def hooks(self) -> Hooks:
        def remove() -> None:
            self.removed += 1
            self.serving = None

        def ensure(model: Any) -> None:
            self.ensured.append(model.key)
            self.serving = model.key
            self.available -= int(model.size_bytes)

        def launch(args: dict[str, Any]) -> dict[str, Any]:
            self.launched.append(args)
            return {"run_id": "run-q"}

        return Hooks(
            active_work=lambda: self.active,
            serving=lambda: self.serving,
            remove_inference=remove,
            ensure_inference=ensure,
            inference_ready=lambda: (True, "running: ready"),
            chat=lambda body, timeout: self.chat(body, timeout),
            launch=launch,
            run_status=lambda run_id: {"phase": "finished", "conversations": [{}]},
            report=lambda run_id: {
                "verified": self.verified,
                "verdict": "verified",
                "evaluations": [{}],
            },
        )


def _qualifier(settings: Settings, host: FakeHost) -> Qualifier:
    return Qualifier(
        settings=settings, hooks=host.hooks(), clock=FakeClock(), meminfo=host.meminfo,
        sleep=lambda s: None, sample_every_s=0.01,
    )  # fmt: skip


def _flash_next() -> Any:
    return load_models(PACKAGED_MODELS_FILE).get("qwen3.8-flash-next")


def test_a_model_that_works_with_the_whole_workload_qualifies(settings: Settings) -> None:
    host = FakeHost(available_gib=100)
    q = _qualifier(settings, host)
    job = q.start(_flash_next(), background=False)
    assert job.status == "qualified", job.failures
    assert host.removed == 1 and host.ensured == ["qwen3.8-flash-next"]
    assert [p["ok"] for p in job.steps["tool_calls"]["probes"]] == [True] * len(TOOL_PROBES)
    assert job.steps["long_prompt"]["prefill_tokens_per_s"] == 812.4
    assert job.steps["workload"]["run_id"] == "run-q"
    [launch] = host.launched
    assert launch["model_key"] == "qwen3.8-flash-next" and launch["checks"]
    assert job.memory["samples"] >= 2 and job.memory["min_available_gib"] is not None
    saved = json.loads(job.path.read_text())
    assert saved["status"] == "qualified" and saved["samples"]
    assert q.latest("qwen3.8-flash-next")["status"] == "qualified"  # type: ignore[index]


def test_a_model_already_served_is_loaded_again_with_the_settings_under_test(
    settings: Settings,
) -> None:
    """hugo-dgx1: Flash-Next served at 64K while 256K was being qualified. Its own
    weights count as freed, and the server is replaced, not kept."""
    host = FakeHost(available_gib=27)
    host.serving = "qwen3.8-flash-next"
    job = _qualifier(settings, host).start(_flash_next(), background=False)
    assert job.steps["preflight"]["ok"], job.steps["preflight"]
    assert host.removed == 1 and host.ensured == ["qwen3.8-flash-next"]


def test_not_enough_memory_fails_before_anything_is_swapped(settings: Settings) -> None:
    host = FakeHost(available_gib=60)  # 60 + 15.7 freed < 83.8 + 8
    job = _qualifier(settings, host).start(_flash_next(), background=False)
    assert job.status == "failed"
    assert "hold the reservation first" in job.failures[0]
    assert host.removed == 0 and host.ensured == []


def test_active_work_blocks_qualification(settings: Settings) -> None:
    host = FakeHost()
    host.active = ["run r1"]
    job = _qualifier(settings, host).start(_flash_next(), background=False)
    assert job.status == "failed" and "run r1 still use the model" in job.failures[0]


def test_a_broken_tool_call_parser_fails_qualification(settings: Settings) -> None:
    host = FakeHost()

    def chat(body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        if body.get("tools") and body["messages"][-1]["role"] != "tool":
            return 200, {"choices": [{"message": {"content": "<tool_call>{...}</tool_call>"}}]}
        return _good_chat(body, timeout)

    host.chat = chat
    job = _qualifier(settings, host).start(_flash_next(), background=False)
    assert job.status == "failed"
    assert job.failures == ["tool_calls: 1/5 tool-call probes passed"]
    assert "workload" not in job.steps  # later steps do not run


def test_memory_pressure_during_the_run_fails_it(settings: Settings) -> None:
    host = FakeHost()
    host.verified = True
    original = host.hooks

    def hooks() -> Hooks:
        h = original()
        report = h.report

        def squeezed(run_id: str) -> dict[str, Any]:
            host.available = int(2 * GIB)
            host.swap_used = 3 * GIB
            return report(run_id)

        h.report = squeezed
        return h

    host.hooks = hooks  # type: ignore[method-assign]
    job = _qualifier(settings, host).start(_flash_next(), background=False)
    assert job.status == "failed"
    assert any("MemAvailable fell to 2.0 GiB" in f for f in job.failures)
    assert any("swap grew by 3.0 GiB" in f for f in job.failures)


def test_judging_probes() -> None:
    single = TOOL_PROBES[0]
    assert judge_probe(single, _tool_answer("get_weather", {"city": "Halifax, NS"}))[0]
    ok, why = judge_probe(single, _tool_answer("get_weather", {"town": "x"}))
    assert not ok and "unexpected arguments" in why
    ok, why = judge_probe(single, {"choices": [{"message": {"content": "sunny"}}]})
    assert not ok and "no tool call" in why
    bad_json = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": "{city"}}]}}]}  # fmt: skip
    assert "not JSON" in judge_probe(single, bad_json)[1]


def test_long_prompt_and_meminfo(tmp_path: Path) -> None:
    text = long_prompt(22_000, "quartzlight")
    assert text.startswith("Remember this: the secret word is quartzlight.")
    assert text.count("\n") > 900
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 127000000 kB\nMemAvailable:  58000000 kB\nSwapTotal: 0 kB\n")
    info = read_meminfo(meminfo)
    assert info["MemAvailable"] == 58000000 * 1024 and info["SwapTotal"] == 0


def test_the_control_ops(harness: Harness) -> None:
    with pytest.raises(RequestError, match="no qualification"):
        harness.controller.handle("qualify.status", {})
    with pytest.raises(RequestError, match="unknown model"):
        harness.controller.handle("qualify.start", {"model_key": "gpt-9"})
    with pytest.raises(RequestError, match="not a model key"):
        harness.controller.handle("qualify.status", {"model_key": "../../etc"})


# --- readiness ----------------------------------------------------------------------


def _junit(path: Path, *, failures: int = 0) -> None:
    case = '<testcase name="t"/>' if not failures else (
        '<testcase name="t"><failure message="boom">trace</failure></testcase>'
    )  # fmt: skip
    path.write_text(
        f'<testsuites><testsuite tests="1" failures="{failures}" errors="0" skipped="0">'
        f"{case}</testsuite></testsuites>"
    )


def test_readiness_runs_every_suite_and_reports(tmp_path: Path) -> None:
    ran: list[str] = []

    def runner(argv: Sequence[str], env: dict[str, str]) -> tuple[int, str]:
        path = argv[5]
        ran.append(path)
        junit = Path(next(a for a in argv if a.startswith("--junitxml=")).split("=", 1)[1])
        failed = "forced_reset" in path
        _junit(junit, failures=int(failed))
        return (1 if failed else 0), "output"

    report = run_readiness(
        tmp_path / "r", model_key="qwen3.6-35b-a3b", runner=runner, echo=lambda s: None
    )
    assert ran == [p for _, p in SUITES]
    assert report["passed"] is False
    statuses = {s["path"]: s["status"] for s in report["suites"]}
    assert statuses["tests/dgx/test_forced_reset.py"] == "failed"
    assert list(statuses.values()).count("passed") == len(SUITES) - 1
    md = (tmp_path / "r" / "report.md").read_text()
    assert "NOT PASSED" in md and "boom" in md and "test_reboot.md" in md
    assert "test_reserve_release.py (--with-reservation)" in md

    only = run_readiness(
        tmp_path / "o", model_key="m", runner=runner, only=["egress"], echo=lambda s: None
    )
    assert [s["path"] for s in only["suites"]] == ["tests/dgx/test_egress.py"]
    assert only["passed"] is True


def test_a_suite_without_a_report_is_an_error(tmp_path: Path) -> None:
    assert read_junit(tmp_path / "missing.xml")[2] == 1
    report = run_readiness(
        tmp_path / "r", model_key="m", runner=lambda argv, env: (2, "ImportError"),
        only=["egress"], echo=lambda s: None,
    )  # fmt: skip
    assert report["suites"][0]["status"] == "error" and report["passed"] is False


def test_without_server_timings_the_speeds_come_from_two_timed_requests(
    settings: Settings,
) -> None:
    """SGLang answers without llama.cpp's `timings`: the prefill is a one-token answer
    to the long prompt, the decode the full answer once that prompt is cached."""
    host = FakeHost(available_gib=250)
    long_bodies: list[dict[str, Any]] = []

    def sglang_chat(body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        if "secret word" in str(body["messages"][-1].get("content") or ""):
            long_bodies.append(body)
            return 200, {
                "choices": [{"message": {"content": "quartzlight"}}],
                "usage": {"prompt_tokens": 196000, "completion_tokens": 400},
            }
        return _good_chat(body, timeout)

    host.chat = sglang_chat
    model = load_models(PACKAGED_MODELS_FILE).get("qwen3.8-flash-next-sglang")
    job = _qualifier(settings, host).start(model, background=False)
    assert [b["max_tokens"] for b in long_bodies] == [1, 16384]
    step = job.steps["long_prompt"]
    assert step["ok"] and step["prompt_tokens"] == 196000
    assert step["prefill_tokens_per_s"] > 0 and step["decode_tokens_per_s"] > 0
