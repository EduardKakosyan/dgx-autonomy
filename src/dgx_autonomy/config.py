"""Controller settings (from the environment) and the model catalog (from models.yaml)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DATA_DIR = "/var/lib/dgx-autonomy"
DEFAULT_MODELS_DIR = "/home/jim/models"
DEFAULT_INFERENCE_IMAGE = "dgx-autonomy/inference:f95b0d9"
DEFAULT_AGENT_IMAGE = "dgx-autonomy/agent:1.49.4"
PACKAGED_MODELS_FILE = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


class ConfigError(ValueError):
    """The settings or model catalog are unusable."""


@dataclass(frozen=True)
class ModelConfig:
    key: str
    repo: str
    revision: str
    gguf: str
    size_bytes: int
    ctx: int
    parallel: int
    template: str
    cache_type_k: str
    cache_type_v: str
    extra_args: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class ModelCatalog:
    default: str
    models: Mapping[str, ModelConfig]

    def get(self, key: str | None) -> ModelConfig:
        chosen = key or self.default
        try:
            return self.models[chosen]
        except KeyError:
            known = ", ".join(sorted(self.models))
            raise ConfigError(f"unknown model {chosen!r}; configured: {known}") from None


def _model_from(key: str, raw: Mapping[str, Any]) -> ModelConfig:
    try:
        gguf = str(raw["gguf"])
        if gguf.startswith("/") or ".." in Path(gguf).parts:
            raise ConfigError(f"model {key}: gguf must be relative to the models directory")
        return ModelConfig(
            key=key,
            repo=str(raw["repo"]),
            revision=str(raw["revision"]),
            gguf=gguf,
            size_bytes=int(raw["size_bytes"]),
            ctx=int(raw["ctx"]),
            parallel=int(raw.get("parallel", 1)),
            template=str(raw.get("template", "embedded")),
            cache_type_k=str(raw.get("cache_type_k", "f16")),
            cache_type_v=str(raw.get("cache_type_v", "f16")),
            extra_args=tuple(str(a) for a in raw.get("extra_args") or ()),
            status=str(raw.get("status", "unqualified")),
        )
    except KeyError as missing:
        raise ConfigError(f"model {key}: missing field {missing}") from None


def load_models(path: Path) -> ModelCatalog:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ConfigError(f"{path}: expected a mapping with a `models` mapping")
    models = {str(k): _model_from(str(k), v) for k, v in data["models"].items()}
    default = str(data.get("default") or next(iter(models), ""))
    if default not in models:
        raise ConfigError(f"{path}: default model {default!r} is not configured")
    return ModelCatalog(default=default, models=models)


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Settings:
    """Everything the controller needs to know about the host.

    Paths are host paths. The controller container mounts the data directory at
    the same path, so the bind-mount sources it hands to `docker run` are valid
    on the host.
    """

    data_dir: Path
    models_dir: Path
    models_file: Path
    socket_path: Path
    operator_uid: int | None = None
    operator_gid: int | None = None
    inference_image: str = DEFAULT_INFERENCE_IMAGE
    agent_image: str = DEFAULT_AGENT_IMAGE
    internal_network: str = "dgx-autonomy-internal"
    egress_network: str = "dgx-autonomy-egress"
    inference_name: str = "dgx-autonomy-inference"
    inference_port: int = 8080
    agent_port: int = 8000
    # UID/GID of the `openhands` user baked into the Agent Server image.
    agent_uid: int = 10001
    agent_gid: int = 10001
    # llama-server needs no identity of its own; it only reads the model.
    inference_user: str = "65534:65534"
    agent_memory: str = "32g"
    agent_cpus: str = "8"
    agent_pids_limit: int = 4096
    max_budget_hours: float = 40.0
    poll_seconds: float = 5.0
    inference_load_timeout_s: float = 30 * 60.0
    agent_start_timeout_s: float = 5 * 60.0

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state" / "controller.sqlite3"

    @property
    def inference_url(self) -> str:
        return f"http://{self.inference_name}:{self.inference_port}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        data_dir = Path(e.get("DGX_AUTONOMY_DATA", DEFAULT_DATA_DIR))
        return cls(
            data_dir=data_dir,
            models_dir=Path(e.get("DGX_AUTONOMY_MODELS", DEFAULT_MODELS_DIR)),
            models_file=Path(e.get("DGX_AUTONOMY_MODELS_FILE", str(PACKAGED_MODELS_FILE))),
            socket_path=Path(e.get("DGX_AUTONOMY_SOCKET", str(default_socket_path(data_dir)))),
            operator_uid=_int_or_none(e.get("DGX_AUTONOMY_OPERATOR_UID")),
            operator_gid=_int_or_none(e.get("DGX_AUTONOMY_OPERATOR_GID")),
            inference_image=e.get("DGX_AUTONOMY_INFERENCE_IMAGE", DEFAULT_INFERENCE_IMAGE),
            agent_image=e.get("DGX_AUTONOMY_AGENT_IMAGE", DEFAULT_AGENT_IMAGE),
        )


def default_socket_path(data_dir: Path | None = None) -> Path:
    return (data_dir or Path(DEFAULT_DATA_DIR)) / "control" / "control.sock"
