from __future__ import annotations

from pathlib import Path

import pytest

from dgx_autonomy.config import PACKAGED_MODELS_FILE, Settings

from fakes import Harness, make_harness


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        models_dir=tmp_path / "models",
        models_file=PACKAGED_MODELS_FILE,
        socket_path=tmp_path / "control.sock",
        boot_id_file=tmp_path / "proc" / "boot_id",
    )


@pytest.fixture
def harness(settings: Settings) -> Harness:
    return make_harness(settings)
