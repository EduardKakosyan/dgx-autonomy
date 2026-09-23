"""Model qualification: does a model work here, with the whole workload running?

Download size says little about fit. `dgx-autonomy qualify MODEL` measures instead,
in the controller, one step after the other:

    preflight   no run or plan is active (the model is swapped); enough memory is
                available for the weights plus headroom (hold the reservation first
                for a model that does not fit next to claude-qwen)
    load        the owned llama-server serves MODEL (another model is removed first);
                load time and memory after load
    tool calls  a suite of OpenAI-style tool calls through llama.cpp's parser: single
                and multiple arguments, a choice between tools, nested arrays of
                objects (like write_handoff), and a tool-result round trip
    long prompt one request filling most of the configured context; prefill and
                decode speed from llama.cpp's timings, and a recall check
    workload    a real run: the agent builds and serves a small page, and the frozen
                checks run in the evaluator's browser against the demo. It must
                finish VERIFIED within its budget.

A sampler records MemAvailable and swap every few seconds for the whole time. The
verdict is `qualified` when every step passed and memory never ran short; otherwise
`failed`, with the reason. Results are written to `qualification/<model>/<time>.json`
in the data directory; the operator copies the measured settings into
config/models.yaml. A controller restart interrupts a qualification (it is then
reported `interrupted`); run it again.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ModelConfig, Settings
from .ports import Clock

GIB = 1024**3
MEMORY_HEADROOM_BYTES = 8 * GIB
MIN_AVAILABLE_BYTES = 4 * GIB
MAX_SWAP_GROWTH_BYTES = 1 * GIB
# Leaves room for the answer (4096 tokens) and for tokenizer estimates that run long.
LONG_PROMPT_FRACTION = 0.75
WORKLOAD_BUDGET_HOURS = 0.75


# --- memory -------------------------------------------------------------------------


def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    """Host memory in bytes (a container sees the host's /proc/meminfo)."""
    out: dict[str, int] = {}
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


@dataclass
class MemorySampler:
    read: Callable[[], dict[str, int]]
    samples: list[dict[str, Any]] = field(default_factory=list)

    def sample(self, now: datetime) -> dict[str, Any]:
        info = self.read()
        swap_used = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
        s = {"at": now.isoformat(), "available": info.get("MemAvailable"), "swap_used": swap_used}
        self.samples.append(s)
        return s

    def summary(self) -> dict[str, Any]:
        avail = [s["available"] for s in self.samples if s["available"] is not None]
        swap = [s["swap_used"] for s in self.samples]
        return {
            "samples": len(self.samples),
            "min_available_gib": round(min(avail) / GIB, 2) if avail else None,
            "swap_growth_gib": round((max(swap) - swap[0]) / GIB, 3) if swap else None,
        }


# --- tool-call suite ----------------------------------------------------------------


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


WEATHER = _tool("get_weather", "Current weather for a city.",
                {"city": {"type": "string"}}, ["city"])  # fmt: skip
ADD = _tool("add_numbers", "Add two integers.",
            {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"])  # fmt: skip
RUN = _tool("run_command", "Run a shell command in the terminal.",
            {"command": {"type": "string"}}, ["command"])  # fmt: skip
PLAN = _tool(
    "record_plan",
    "Record a plan as a list of items.",
    {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["todo", "done"]},
                },
                "required": ["title", "status"],
            },
        }
    },
    ["items"],
)


@dataclass(frozen=True)
class ToolProbe:
    name: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    # None: a plain answer (no tool call) is expected, containing this text.
    expect_tool: str | None
    check: Callable[[dict[str, Any]], bool] = lambda args: True
    expect_text: str = ""


def _user(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


TOOL_PROBES: tuple[ToolProbe, ...] = (
    ToolProbe(
        "single argument",
        _user("What is the weather in Halifax right now? Use the tool."),
        [WEATHER],
        "get_weather",
        lambda a: "halifax" in str(a.get("city", "")).lower(),
    ),
    ToolProbe(
        "choose between tools",
        _user("What is 17 plus 25? Use a tool to compute it."),
        [WEATHER, ADD],
        "add_numbers",
        lambda a: {int(a.get("a", 0)), int(a.get("b", 0))} == {17, 25},
    ),
    ToolProbe(
        "nested array of objects",
        _user(
            "Record a plan with exactly two items: 'build the page' with status todo and"
            " 'write the brief' with status done. Use the tool."
        ),
        [PLAN],
        "record_plan",
        lambda a: (
            sorted((i.get("title", ""), i.get("status")) for i in a.get("items", []))
            == [("build the page", "todo"), ("write the brief", "done")]
        ),
    ),
    ToolProbe(
        "shell command",
        _user("List the files in /tmp, including hidden ones. Use the tool."),
        [RUN],
        "run_command",
        lambda a: "ls" in str(a.get("command", "")) and "/tmp" in str(a.get("command", "")),
    ),
    ToolProbe(
        "tool result round trip",
        [
            {"role": "user", "content": "What is the weather in Halifax? Use the tool."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Halifax"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"temp_c": 14, "sky": "fog"}'},
        ],
        [WEATHER],
        None,
        expect_text="14",
    ),
)


def judge_probe(probe: ToolProbe, body: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether a /v1/chat/completions answer is what the probe expects, and why not."""
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        return False, f"no choice in the answer: {str(body)[:300]}"
    calls = message.get("tool_calls") or []
    if probe.expect_tool is None:
        if calls:
            return False, f"called {calls[0]['function']['name']} instead of answering"
        content = str(message.get("content") or "")
        if probe.expect_text not in content:
            return False, f"the answer does not mention {probe.expect_text!r}: {content[:200]}"
        return True, "answered from the tool result"
    if not calls:
        return False, f"no tool call; the message was {str(message)[:300]}"
    fn = calls[0].get("function") or {}
    if fn.get("name") != probe.expect_tool:
        return False, f"called {fn.get('name')!r}, expected {probe.expect_tool!r}"
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return False, f"arguments are not JSON: {str(fn.get('arguments'))[:200]}"
    if not isinstance(args, dict) or not probe.check(args):
        return False, f"unexpected arguments {json.dumps(args)[:300]}"
    return True, f"{fn['name']}({json.dumps(args)[:120]})"


# --- long prompt --------------------------------------------------------------------


def long_prompt(target_tokens: int, secret: str) -> str:
    """Filler of about `target_tokens` tokens, with a fact to recall at the start."""
    head = f"Remember this: the secret word is {secret}.\n"
    line = "Line {n:05d}: the quick brown fox jumps over the lazy dog near the river bank.\n"
    # About 22 tokens per filler line with Qwen tokenizers; the answer's usage says
    # how many there really were.
    lines = [line.format(n=i) for i in range(max(1, target_tokens // 22))]
    return head + "".join(lines) + "\nWhat is the secret word? Answer with the word only."


# --- the workload run ----------------------------------------------------------------

WORKLOAD_BRIEF = """\
# Brief: qualification page

1. Create `index.html` in the project directory: a page titled "Qualified" whose
   <h1> reads "Qualification page", with a button labelled "Count" and a paragraph
   with id "count" showing 0. Each click on the button adds 1 to the number shown.
2. Serve it with the `start_demo` tool: command `python3 -m http.server 3000 --bind 0.0.0.0`,
   port 3000.
3. Check it yourself, then finish.
"""

WORKLOAD_CHECKS = {
    "criteria.yaml": """\
criteria:
  - key: counter
    description: The page shows the heading and the button counts clicks
    test: counter.spec.ts
  - key: served
    description: GET / returns the page
    test: test_served.py
""",
    "counter.spec.ts": """\
import { test, expect } from '@playwright/test'

test('the button counts clicks', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Qualification page')
  const count = page.locator('#count')
  await expect(count).toHaveText('0')
  await page.getByRole('button', { name: 'Count' }).click()
  await page.getByRole('button', { name: 'Count' }).click()
  await expect(count).toHaveText('2')
})
""",
    "test_served.py": """\
import os

import httpx


def test_served() -> None:
    r = httpx.get(os.environ["APP_URL"], timeout=10)
    assert r.status_code == 200
    assert "Qualification page" in r.text
""",
}


# --- the job --------------------------------------------------------------------------


class QualificationError(RuntimeError):
    pass


@dataclass
class Job:
    model_key: str
    started_at: str
    path: Path
    status: str = "running"  # running | qualified | failed | interrupted
    step: str = "preflight"
    steps: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    finished_at: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "status": self.status,
            "step": self.step,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": self.steps,
            "failures": self.failures,
            "memory": self.memory,
            "path": str(self.path),
        }

    def save(self, samples: list[dict[str, Any]] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self.view() | ({"samples": samples} if samples is not None else {})
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)


@dataclass
class Hooks:
    """What the qualification needs from the controller (injected for tests)."""

    active_work: Callable[[], list[str]]
    serving: Callable[[], str | None]
    remove_inference: Callable[[], object]
    ensure_inference: Callable[[ModelConfig], object]
    inference_ready: Callable[[], tuple[bool, str]]
    chat: Callable[[dict[str, Any], float], tuple[int, Any]]
    launch: Callable[[dict[str, Any]], dict[str, Any]]
    run_status: Callable[[str], dict[str, Any]]
    report: Callable[[str], dict[str, Any]]


class Qualifier:
    def __init__(
        self,
        *,
        settings: Settings,
        hooks: Hooks,
        clock: Clock,
        meminfo: Callable[[], dict[str, int]] = read_meminfo,
        sleep: Callable[[float], None] = time.sleep,
        sample_every_s: float = 5.0,
    ) -> None:
        self._settings = settings
        self._hooks = hooks
        self._clock = clock
        self._meminfo = meminfo
        self._sleep = sleep
        self._sample_every_s = sample_every_s
        self._lock = threading.Lock()
        self.job: Job | None = None

    @property
    def root(self) -> Path:
        return self._settings.data_dir / "qualification"

    def start(self, model: ModelConfig, *, background: bool = True) -> Job:
        with self._lock:
            if self.job is not None and self.job.status == "running":
                raise QualificationError(
                    f"a qualification of {self.job.model_key} is running ({self.job.step})"
                )
            now = self._clock.now()
            path = self.root / model.key / f"{now:%Y%m%dT%H%M%S}.json"
            self.job = Job(model.key, now.isoformat(), path)
            self.job.save()
        if background:
            threading.Thread(target=self.run, args=(model,), name="qualify", daemon=True).start()
        else:
            self.run(model)
        return self.job

    def latest(self, model_key: str | None = None) -> dict[str, Any] | None:
        """The running job, or the newest result on disk (a `running` one left by a
        controller that died is reported `interrupted`)."""
        if self.job is not None and (model_key in (None, self.job.model_key)):
            return self.job.view()
        dirs = [self.root / model_key] if model_key else sorted(self.root.glob("*"))
        files = sorted(
            (f for d in dirs if d.is_dir() for f in d.glob("*.json")), key=lambda f: f.name
        )
        if not files:
            return None
        data: dict[str, Any] = json.loads(files[-1].read_text())
        data.pop("samples", None)
        if data.get("status") == "running":
            data["status"] = "interrupted"
        return data

    def run(self, model: ModelConfig) -> None:
        job = self.job
        assert job is not None
        sampler = MemorySampler(self._meminfo)
        stop = threading.Event()

        def sample_loop() -> None:
            while not stop.is_set():
                sampler.sample(self._clock.now())
                stop.wait(self._sample_every_s)

        thread = threading.Thread(target=sample_loop, name="qualify-memory", daemon=True)
        thread.start()
        try:
            for name, step in (
                ("preflight", self._preflight),
                ("load", self._load),
                ("tool_calls", self._tool_calls),
                ("long_prompt", self._long_prompt),
                ("workload", self._workload),
            ):
                job.step = name
                job.save()
                started = time.monotonic()
                try:
                    result = step(model)
                except QualificationError as exc:
                    job.steps[name] = {"ok": False, "error": str(exc)}
                    job.failures.append(f"{name}: {exc}")
                    break
                result["seconds"] = round(time.monotonic() - started, 1)
                job.steps[name] = result
                if not result.get("ok"):
                    job.failures.append(f"{name}: {result.get('error', 'failed')}")
                    break
        except Exception as exc:  # the job must always end with a verdict
            job.failures.append(f"{job.step}: {type(exc).__name__}: {exc}")
        finally:
            stop.set()
            thread.join(timeout=10)
            sampler.sample(self._clock.now())
            job.memory = sampler.summary()
            low = job.memory.get("min_available_gib")
            if low is not None and low * GIB < MIN_AVAILABLE_BYTES:
                job.failures.append(f"memory: MemAvailable fell to {low} GiB")
            swap = job.memory.get("swap_growth_gib")
            if swap is not None and swap * GIB > MAX_SWAP_GROWTH_BYTES:
                job.failures.append(f"memory: swap grew by {swap} GiB")
            job.status = "failed" if job.failures else "qualified"
            job.step = "done"
            job.finished_at = self._clock.now().isoformat()
            job.save(sampler.samples)

    # --- steps --------------------------------------------------------------------------

    def _preflight(self, model: ModelConfig) -> dict[str, Any]:
        busy = self._hooks.active_work()
        if busy:
            raise QualificationError(
                f"{', '.join(busy)} still use the model; qualification swaps it"
            )
        info = self._meminfo()
        available = info.get("MemAvailable", 0)
        serving = self._hooks.serving()
        # The model we would remove frees roughly its weights.
        freed = 0
        if serving not in (None, model.key):
            freed = self._settings_size(serving)
        needed = model.size_bytes + MEMORY_HEADROOM_BYTES
        result = {
            "ok": available + freed >= needed,
            "available_gib": round(available / GIB, 1),
            "freed_by_swap_gib": round(freed / GIB, 1),
            "needed_gib": round(needed / GIB, 1),
            "serving_before": serving,
        }
        if not result["ok"]:
            result["error"] = (
                f"{result['available_gib']} GiB available (+{result['freed_by_swap_gib']} after"
                f" removing {serving}), {result['needed_gib']} GiB needed; hold the reservation"
                " first (dgx-autonomy reserve --no-inference)"
            )
        return result

    def _settings_size(self, key: str) -> int:
        from .config import load_models

        try:
            return load_models(self._settings.models_file).get(key).size_bytes
        except Exception:
            return 0

    def _load(self, model: ModelConfig) -> dict[str, Any]:
        serving = self._hooks.serving()
        if serving not in (None, model.key):
            self._hooks.remove_inference()
        before = self._meminfo().get("MemAvailable", 0)
        started = time.monotonic()
        self._hooks.ensure_inference(model)
        deadline = started + self._settings.inference_load_timeout_s
        while True:
            ready, detail = self._hooks.inference_ready()
            if ready:
                break
            if time.monotonic() > deadline or detail.startswith("exited"):
                raise QualificationError(f"llama-server did not become ready: {detail}")
            self._sleep(5)
        after = self._meminfo().get("MemAvailable", 0)
        return {
            "ok": True,
            "load_seconds": round(time.monotonic() - started, 1),
            "available_after_gib": round(after / GIB, 1),
            "memory_used_gib": round((before - after) / GIB, 1),
        }

    def _chat(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        status, answer = self._hooks.chat(body, timeout)
        if status != 200 or not isinstance(answer, dict):
            raise QualificationError(f"HTTP {status}: {str(answer)[:300]}")
        return answer

    def _tool_calls(self, model: ModelConfig) -> dict[str, Any]:
        results = []
        for probe in TOOL_PROBES:
            body = {
                "model": model.key,
                "messages": probe.messages,
                "tools": probe.tools,
                "tool_choice": "auto",
                "temperature": 0,
                "max_tokens": 4096,
            }
            try:
                ok, why = judge_probe(probe, self._chat(body, 600))
            except QualificationError as exc:
                ok, why = False, str(exc)
            results.append({"probe": probe.name, "ok": ok, "detail": why})
        passed = sum(1 for r in results if r["ok"])
        out: dict[str, Any] = {"ok": passed == len(results), "passed": passed, "probes": results}
        if not out["ok"]:
            out["error"] = f"{passed}/{len(results)} tool-call probes passed"
        return out

    def _long_prompt(self, model: ModelConfig) -> dict[str, Any]:
        target = int(model.ctx * LONG_PROMPT_FRACTION)
        secret = "quartzlight"
        answer = self._chat(
            {
                "model": model.key,
                "messages": _user(long_prompt(target, secret)),
                "temperature": 0,
                # Room for a reasoning model to think before it answers.
                "max_tokens": 4096,
            },
            1800,
        )
        usage = answer.get("usage") or {}
        timings = answer.get("timings") or {}
        content = str(((answer.get("choices") or [{}])[0].get("message") or {}).get("content"))
        recalled = secret in content.lower()
        out = {
            "ok": recalled,
            "prompt_tokens": usage.get("prompt_tokens"),
            "prefill_tokens_per_s": _round(timings.get("prompt_per_second")),
            "decode_tokens_per_s": _round(timings.get("predicted_per_second")),
            "recalled": recalled,
        }
        if not recalled:
            out["error"] = f"did not recall the secret word: {content[:200]}"
        return out

    def _workload(self, model: ModelConfig) -> dict[str, Any]:
        from . import frozen

        checks = frozen.encode(WORKLOAD_CHECKS)
        launched = self._hooks.launch(
            {
                "brief_text": WORKLOAD_BRIEF,
                "checks": WORKLOAD_CHECKS,
                "bundle_digest": frozen.bundle_digest(WORKLOAD_BRIEF.encode(), checks),
                "budget_hours": WORKLOAD_BUDGET_HOURS,
                "model_key": model.key,
                "brief_source": f"qualification of {model.key}",
            }
        )
        run_id = str(launched["run_id"])
        if self.job is not None:
            self.job.steps["workload"] = {"run_id": run_id, "ok": None}
            self.job.save()
        limit = time.monotonic() + WORKLOAD_BUDGET_HOURS * 3600 + 20 * 60
        while True:
            status = self._hooks.run_status(run_id)
            if status.get("phase") in ("finished", "failed", "stopped"):
                break
            if time.monotonic() > limit:
                raise QualificationError(f"run {run_id} did not end in time")
            self._sleep(15)
        report = self._hooks.report(run_id)
        out = {
            "ok": bool(report.get("verified")),
            "run_id": run_id,
            "phase": status.get("phase"),
            "outcome": status.get("outcome"),
            "verdict": report.get("verdict"),
            "evaluations": len(report.get("evaluations") or []),
            "conversations": len(status.get("conversations") or []),
        }
        if not out["ok"]:
            out["error"] = f"run {run_id} {status.get('phase')}: {report.get('verdict')}"
        return out


def _round(value: object) -> float | None:
    return round(float(value), 1) if isinstance(value, int | float) else None


def valid_model_key(key: object) -> bool:
    return isinstance(key, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,63}", key))
