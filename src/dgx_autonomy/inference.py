"""The environment's own inference server: start it, and tell loading from ready from busy.

Two backends serve the OpenAI chat API under the model's key:

- llama.cpp's llama-server (the default). `/health` answers 503 while the model loads
  and 200 once it can serve; `/slots` reports per-slot activity, so ready does not
  mean free.
- SGLang, for checkpoints llama.cpp serves slowly. Its port stays closed while it
  loads (read as "unreachable": still starting) and `/health` answers 200 once it
  serves. It has no `/slots`.

The server lives only on the internal network; nothing is published on the host.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import ModelConfig, Settings
from .ports import ContainerPort, ContainerSpec, ContainerState, HttpClient, HttpResponse, Mount
from .runtime import LABEL_PREFIX, LABEL_ROLE

LABEL_MODEL = f"{LABEL_PREFIX}.model"
# Injected before the end of the thinking block when the reasoning budget runs out.
REASONING_BUDGET_MESSAGE = (
    "\n\nI have thought about this long enough. Time to act on it, one step at a time.\n"
)
MODELS_MOUNT = "/models"
# SGLang's writable directories (host: <data dir>/inference/{ple,cache}). The N-gram
# (PLE) table of Qwen3.8-Flash-Next lives in a file there, not in memory.
SGLANG_PLE_MOUNT = "/ple"
SGLANG_CACHE_MOUNT = "/cache"
# The table file is rewritten on every boot. Over a populated file that takes about 55
# minutes (a read-modify-write per page), over a fresh sparse file about 10, so the old
# one is deleted first.
SGLANG_START = (
    'find /ple -name "ple_table_*.bin" -delete; exec python3 -m sglang.launch_server "$@"'
)

# Paths the diagnostic passthrough may reach. Everything else stays unreachable
# from the control socket.
PROXY_PATHS: Mapping[str, frozenset[str]] = {
    "GET": frozenset({"/health", "/slots", "/props", "/v1/models"}),
    "POST": frozenset({"/v1/chat/completions", "/apply-template"}),
}


class InferenceError(RuntimeError):
    pass


class ModelMismatchError(InferenceError):
    """The owned llama-server is serving a different model than the run asked for."""


def inference_command(model: ModelConfig, port: int) -> tuple[str, ...]:
    args = [
        "--model", f"{MODELS_MOUNT}/{model.gguf}",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--alias", model.key,
        "--ctx-size", str(model.ctx),
        "--parallel", str(model.parallel),
        "--n-gpu-layers", "all",
        "--flash-attn", "on",
        "--cache-type-k", model.cache_type_k,
        "--cache-type-v", model.cache_type_v,
        "--n-predict", str(model.max_output_tokens),
        "--jinja",
        "--metrics",
    ]  # fmt: skip
    if model.reasoning_budget is not None:
        args += [
            "--reasoning-budget", str(model.reasoning_budget),
            "--reasoning-budget-message", REASONING_BUDGET_MESSAGE,
        ]  # fmt: skip
    if model.template not in ("", "embedded"):
        args += ["--chat-template-file", f"{MODELS_MOUNT}/{model.template}"]
    return (*args, *model.extra_args)


def sglang_command(model: ModelConfig, port: int) -> tuple[str, ...]:
    args = [
        "--model-path", f"{MODELS_MOUNT}/{model.path}",
        "--served-model-name", model.key,
        "--host", "0.0.0.0",
        "--port", str(port),
        "--context-length", str(model.ctx),
        "--max-running-requests", str(model.parallel),
        "--ple-offload-dir", SGLANG_PLE_MOUNT,
    ]  # fmt: skip
    if model.mem_fraction is not None:
        args += ["--mem-fraction-static", str(model.mem_fraction)]
    return (*args, *model.extra_args)


def sglang_dirs(settings: Settings) -> tuple[Path, Path]:
    root = settings.data_dir / "inference"
    return root / "ple", root / "cache"


def inference_container_spec(settings: Settings, model: ModelConfig) -> ContainerSpec:
    if model.backend == "sglang":
        assert model.image is not None
        ple, cache = sglang_dirs(settings)
        return ContainerSpec(
            name=settings.inference_name,
            image=model.image,
            labels={LABEL_ROLE: "inference", LABEL_MODEL: model.key},
            networks=(settings.internal_network,),
            mounts=(
                Mount(str(settings.models_dir), MODELS_MOUNT, read_only=True),
                Mount(str(ple), SGLANG_PLE_MOUNT),
                Mount(str(cache), SGLANG_CACHE_MOUNT),
            ),
            # No network beyond the internal one: nothing may be fetched at runtime.
            # JIT kernels and compile caches persist in /cache across restarts.
            env={
                "HOME": SGLANG_CACHE_MOUNT,
                "XDG_CACHE_HOME": SGLANG_CACHE_MOUNT,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "SGLANG_CACHE_DIR": f"{SGLANG_CACHE_MOUNT}/sglang",
                "TRITON_CACHE_DIR": f"{SGLANG_CACHE_MOUNT}/triton",
                "TORCHINDUCTOR_CACHE_DIR": f"{SGLANG_CACHE_MOUNT}/inductor",
            },
            entrypoint="/bin/sh",
            command=("-c", SGLANG_START, "sglang", *sglang_command(model, settings.inference_port)),
            user=settings.inference_user,
            gpus=True,
            shm_size="8g",
        )
    return ContainerSpec(
        name=settings.inference_name,
        image=settings.inference_image,
        labels={LABEL_ROLE: "inference", LABEL_MODEL: model.key},
        networks=(settings.internal_network,),
        mounts=(Mount(str(settings.models_dir), MODELS_MOUNT, read_only=True),),
        # The unprivileged user has no home directory; give CUDA somewhere to write.
        env={"HOME": "/tmp"},
        command=inference_command(model, settings.inference_port),
        user=settings.inference_user,
        gpus=True,
    )


@dataclass(frozen=True)
class InferenceStatus:
    container: str  # missing | running | exited | ...
    model_key: str | None
    health: str  # ready | loading | unreachable | error
    slots_total: int | None = None
    slots_busy: int | None = None
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.container == "running" and self.health == "ready"

    def as_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "model_key": self.model_key,
            "health": self.health,
            "ready": self.ready,
            "slots_total": self.slots_total,
            "slots_busy": self.slots_busy,
            "detail": self.detail,
        }


# Memory a new llama-server must leave available for the agent, its demo and the
# evaluator's browser (qualification measured about 24 GiB spare on hugo-dgx1).
LOAD_HEADROOM_BYTES = 8 * 1024**3


def mem_available(path: Path = Path("/proc/meminfo")) -> int | None:
    """Host MemAvailable in bytes (a container sees the host's /proc/meminfo)."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class InferenceManager:
    def __init__(
        self,
        settings: Settings,
        runtime: ContainerPort,
        http: HttpClient,
        available: Callable[[], int | None] = mem_available,
        prepare_dir: Callable[[Path], None] | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._http = http
        self._available = available
        self._prepare_dir = prepare_dir or _owned_by_inference_user(settings)

    def ensure(self, model: ModelConfig) -> ContainerState:
        """Start (or keep) the owned llama-server for `model`. Never swaps models silently.

        A new server is created only when the host has memory for its weights plus
        headroom; a model that does not fit next to claude-qwen needs the reservation.
        """
        current = self._runtime.inspect_container(self._settings.inference_name)
        if current is not None:
            serving = current.labels.get(LABEL_MODEL)
            if serving != model.key:
                raise ModelMismatchError(
                    f"{self._settings.inference_name} serves {serving!r}, run wants {model.key!r}"
                )
        else:
            available = self._available()
            needed = model.memory_bytes + LOAD_HEADROOM_BYTES
            if available is not None and available < needed:
                raise InferenceError(
                    f"not enough memory to load {model.key}: {available / 1024**3:.1f} GiB"
                    f" available, {needed / 1024**3:.1f} GiB needed. Hold the reservation"
                    " first (`dgx-autonomy reserve`), or use a smaller model."
                )
        if model.backend == "sglang":
            for d in sglang_dirs(self._settings):
                self._prepare_dir(d)
        return self._runtime.ensure_container(inference_container_spec(self._settings, model))

    def status(self) -> InferenceStatus:
        container = self._runtime.inspect_container(self._settings.inference_name)
        if container is None:
            return InferenceStatus(container="missing", model_key=None, health="unreachable")
        model_key = container.labels.get(LABEL_MODEL)
        if not container.running:
            logs = self._runtime.container_logs(self._settings.inference_name, tail=20)
            return InferenceStatus(
                container=container.status,
                model_key=model_key,
                health="unreachable",
                detail=f"exit code {container.exit_code}: {logs[-2000:]}",
            )
        health = self._http.request("GET", f"{self._settings.inference_url}/health")
        if health.status == 503:
            return InferenceStatus("running", model_key, "loading")
        if not health.ok:
            label = "unreachable" if health.status == 0 else "error"
            return InferenceStatus("running", model_key, label, detail=_detail(health))
        total, busy = self._slots()
        return InferenceStatus("running", model_key, "ready", total, busy)

    def _slots(self) -> tuple[int | None, int | None]:
        res = self._http.request("GET", f"{self._settings.inference_url}/slots")
        if not res.ok or not isinstance(res.body, list):
            return None, None
        busy = sum(1 for s in res.body if isinstance(s, dict) and s.get("is_processing"))
        return len(res.body), busy

    def request(self, method: str, path: str, body: Any = None) -> HttpResponse:
        """Diagnostic passthrough used by the target-host tool-call test."""
        method = method.upper()
        if path not in PROXY_PATHS.get(method, frozenset()):
            raise InferenceError(f"{method} {path} is not an allowed inference path")
        return self._http.request(
            method, f"{self._settings.inference_url}{path}", json_body=body, timeout=600.0
        )


def _owned_by_inference_user(settings: Settings) -> Callable[[Path], None]:
    """mkdir, owned by the unprivileged inference user when the controller runs as root."""

    def prepare(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            uid, _, gid = settings.inference_user.partition(":")
            os.chown(path, int(uid), int(gid or uid))

    return prepare


def _detail(res: HttpResponse) -> str:
    if res.error:
        return res.error
    return f"HTTP {res.status}: {str(res.body)[:500]}"


class HttpxClient:
    """HttpClient over httpx. Connection failures become status 0, never exceptions."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(transport=transport)

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 5.0,
    ) -> HttpResponse:
        # A server that failed a request may close the pooled connection under the next
        # one ("connection reset"); idempotent requests get one more try on a new one.
        attempts = 2 if method.upper() in ("GET", "HEAD", "DELETE") else 1
        for attempt in range(attempts):
            try:
                resp = self._client.request(
                    method, url, json=json_body, headers=dict(headers or {}), timeout=timeout
                )
                break
            except httpx.TransportError as exc:
                if attempt + 1 < attempts and not isinstance(exc, httpx.TimeoutException):
                    continue
                return HttpResponse(status=0, error=f"{type(exc).__name__}: {exc}")
            except httpx.HTTPError as exc:
                return HttpResponse(status=0, error=f"{type(exc).__name__}: {exc}")
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        return HttpResponse(status=resp.status_code, body=body)
