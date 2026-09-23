"""The frozen agreement: written once at launch, read-only to the agent and evaluator,
and checked against the digest recorded at launch."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from dgx_autonomy import cli, frozen
from dgx_autonomy.controller import RequestError
from dgx_autonomy.evaluation import evaluator_spec
from dgx_autonomy.runtime import agent_container_name, docker_run_argv
from dgx_autonomy.state import Evaluation

from fakes import Harness

BRIEF = "# Brief\n\nServe a page that says hello on port 3000.\n"
CRITERIA = """\
criteria:
  - key: home
    description: The home page says hello
    test: home.spec.ts
  - key: version
    description: GET /version.txt returns 2
    kind: automated
    test: test_version.py
    required: false
  - key: tidy
    description: The page looks tidy
    kind: human_judgment
"""
CHECKS = {
    "criteria.yaml": CRITERIA,
    "home.spec.ts": "import { test, expect } from '@playwright/test'\n",
    "test_version.py": "def test_version():\n    assert True\n",
}


def _bytes(checks: dict[str, str]) -> dict[str, bytes]:
    return frozen.encode(checks)


def launch_with_checks(h: Harness, checks: dict[str, str] | None = None, **extra: Any) -> str:
    checks = CHECKS if checks is None else checks
    args = {
        "brief_text": BRIEF,
        "checks": checks,
        "bundle_digest": frozen.bundle_digest(BRIEF.encode(), _bytes(checks)),
        "budget_hours": 1,
        **extra,
    }
    return str(h.controller.handle("launch", args)["run_id"])


# --- the format ----------------------------------------------------------------------


def test_criteria_are_parsed_with_their_runner() -> None:
    home, version, tidy = frozen.validate(BRIEF.encode(), _bytes(CHECKS))
    assert (home.key, home.kind, home.required, home.runner) == (
        "home",
        "automated",
        True,
        "playwright",
    )
    assert (version.runner, version.required) == ("pytest", False)
    assert (tidy.kind, tidy.test, tidy.runner) == ("human_judgment", None, None)


def test_a_brief_without_checks_has_no_criteria() -> None:
    assert frozen.validate(BRIEF.encode(), {}) == ()


@pytest.mark.parametrize(
    ("checks", "message"),
    [
        ({"home.spec.ts": "x"}, "no criteria.yaml"),
        ({"criteria.yaml": "criteria: []"}, "non-empty"),
        ({"criteria.yaml": "criteria: [{key: Home, description: d, test: a.spec.ts}]"}, "key"),
        ({"criteria.yaml": "criteria: [{key: a, description: d, test: gone.spec.ts}]"}, "exist"),
        ({"criteria.yaml": "criteria: [{key: a, description: d, test: t.py, required: 1}]",
          "t.py": ""}, "true or false"),
        ({"criteria.yaml": "criteria: [{key: a, description: d, test: notes.md}]", "notes.md": ""},
         "neither"),
        ({"criteria.yaml": "criteria: [{key: a, description: d, kind: human_judgment, test: t.py}]",
          "t.py": ""}, "no test"),
        ({"criteria.yaml": "criteria: [{key: a, description: d, kind: vibes}]"}, "kind"),
        ({"criteria.yaml": "criteria: [{key: a, description: d, owner: me}]"}, "unknown fields"),
        ({"criteria.yaml": "criteria:\n - {key: a, description: d, kind: human_judgment}\n"
                           " - {key: a, description: e, kind: human_judgment}"}, "duplicate"),
        ({"criteria.yaml": "criteria: [{key: a, description: d}]", "../escape.py": ""}, "path"),
        ({"criteria.yaml": "criteria: [{key: a, description: d}]", "sub/.hidden": ""}, "path"),
    ],
)  # fmt: skip
def test_bad_checks_are_refused(checks: dict[str, str], message: str) -> None:
    with pytest.raises(frozen.FrozenError, match=message):
        frozen.validate(BRIEF.encode(), _bytes(checks))


def test_the_digest_covers_every_path_and_byte() -> None:
    base = frozen.bundle_digest(BRIEF.encode(), _bytes(CHECKS))
    assert base.startswith("sha256:")
    assert frozen.bundle_digest(BRIEF.encode(), _bytes(dict(reversed(CHECKS.items())))) == base
    renamed = {**CHECKS, "test_version2.py": CHECKS["test_version.py"]}
    del renamed["test_version.py"]
    for other in (
        frozen.bundle_digest((BRIEF + " ").encode(), _bytes(CHECKS)),
        frozen.bundle_digest(BRIEF.encode(), _bytes({**CHECKS, "home.spec.ts": "changed"})),
        frozen.bundle_digest(BRIEF.encode(), _bytes(renamed)),
        frozen.bundle_digest(BRIEF.encode(), _bytes({**CHECKS, "extra.py": ""})),
    ):
        assert other != base


def test_freeze_writes_once_read_only_modes_and_a_manifest(tmp_path: Path) -> None:
    root = tmp_path / "frozen"
    agreement = frozen.freeze(root, BRIEF.encode(), _bytes(CHECKS))
    assert agreement.digest == frozen.bundle_digest(BRIEF.encode(), _bytes(CHECKS))
    assert (root / "brief.md").read_text() == BRIEF
    assert (root / "checks" / "home.spec.ts").read_text() == CHECKS["home.spec.ts"]
    for path in (root, root / "checks"):
        assert path.stat().st_mode & 0o777 == 0o755
    for path in (root / "brief.md", root / "checks" / "criteria.yaml", root / "manifest.json"):
        assert path.stat().st_mode & 0o777 == 0o644
    assert frozen.load(root, agreement.digest).criteria == agreement.criteria
    assert not (tmp_path / ".frozen.tmp").exists()
    with pytest.raises(frozen.FrozenError, match="written once"):
        frozen.freeze(root, BRIEF.encode(), {})


@pytest.mark.parametrize("tamper", ["edit", "add", "remove", "symlink"])
def test_a_changed_agreement_no_longer_loads(tmp_path: Path, tamper: str) -> None:
    root = tmp_path / "frozen"
    digest = frozen.freeze(root, BRIEF.encode(), _bytes(CHECKS)).digest
    target = root / "checks" / "test_version.py"
    if tamper == "edit":
        target.write_text("def test_version():\n    pass  # always green\n")
    elif tamper == "add":
        (root / "checks" / "conftest.py").write_text("")
    elif tamper == "remove":
        target.unlink()
    else:
        target.unlink()
        target.symlink_to(tmp_path)
    with pytest.raises(frozen.FrozenError):
        frozen.load(root, digest)


# --- the CLI side ------------------------------------------------------------------


def _brief_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plan"
    (d / "checks" / "__pycache__").mkdir(parents=True)
    (d / "brief.md").write_text(BRIEF)
    for name, text in CHECKS.items():
        (d / "checks" / name).write_text(text)
    (d / "checks" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    return d


def test_read_bundle_takes_a_file_or_a_directory(tmp_path: Path) -> None:
    d = _brief_dir(tmp_path)
    assert frozen.read_bundle(d / "brief.md") == (BRIEF, {})
    text, checks = frozen.read_bundle(d)
    assert text == BRIEF and checks == CHECKS  # caches are left out
    (d / "checks" / "link.py").symlink_to(d / "brief.md")
    with pytest.raises(frozen.FrozenError, match="regular files"):
        frozen.read_bundle(d)
    (d / "checks" / "link.py").unlink()
    (d / "checks" / "image.png").write_bytes(b"\x89PNG\xff")
    with pytest.raises(frozen.FrozenError, match="UTF-8"):
        frozen.read_bundle(d)


def test_the_cli_sends_the_checks_and_the_digest_of_what_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: dict[str, Any] = {}

    def fake_call(_socket: Path, op: str, args: dict[str, Any], **_: Any) -> dict[str, Any]:
        sent.update(op=op, **args)
        return {"run_id": "r1", "deadline_at": "d", "frozen_digest": args["bundle_digest"]}

    monkeypatch.setattr(cli, "call", fake_call)
    ns = argparse.Namespace(
        brief=str(_brief_dir(tmp_path)), model=None, budget_hours=2.0, json=False, socket=None
    )
    assert cli.cmd_launch(ns) == 0
    assert sent["op"] == "launch" and sent["checks"] == CHECKS
    assert sent["bundle_digest"] == frozen.bundle_digest(BRIEF.encode(), _bytes(CHECKS))
    out = capsys.readouterr().out
    assert "home" in out and "playwright checks/home.spec.ts" in out and "human judgment" in out


# --- launch ----------------------------------------------------------------------------


def test_launch_freezes_the_agreement_and_records_its_criteria(harness: Harness) -> None:
    h = harness
    run_id = launch_with_checks(h)
    run = h.state.get_run(run_id)
    paths = h.controller.paths(run_id)
    assert run is not None
    assert run.frozen_digest == frozen.bundle_digest(BRIEF.encode(), _bytes(CHECKS))
    assert run.brief_path == str(paths.frozen_dir / "brief.md")
    assert [(c.key, c.kind, c.required) for c in h.state.criteria(run_id)] == [
        ("home", "automated", True),
        ("version", "automated", False),
        ("tidy", "human_judgment", True),
    ]
    assert frozen.load(paths.frozen_dir, run.frozen_digest).digest == run.frozen_digest


def test_a_digest_mismatch_refuses_the_launch(harness: Harness) -> None:
    h = harness
    with pytest.raises(RequestError, match=r"refusing to launch.*changed after launch"):
        launch_with_checks(h, bundle_digest="sha256:" + "0" * 64)
    assert h.state.list_runs() == []
    runs_dir = h.settings.runs_dir
    assert not any((d / "frozen").exists() for d in runs_dir.iterdir())


def test_invalid_checks_refuse_the_launch(harness: Harness) -> None:
    with pytest.raises(RequestError, match=r"criteria\.yaml"):
        launch_with_checks(harness, checks={"home.spec.ts": "x"})
    assert harness.state.list_runs() == []


# --- mounts ----------------------------------------------------------------------------


def test_the_agent_sees_the_agreement_read_only_and_nothing_of_the_evaluation(
    harness: Harness,
) -> None:
    h = harness
    run_id = launch_with_checks(h)
    h.inference_ready()
    h.controller.reconcile_once()
    spec = h.runtime.specs[agent_container_name(run_id)]
    paths = h.controller.paths(run_id)
    mounts = {m.target: m for m in spec.mounts}
    assert mounts["/brief"].source == str(paths.frozen_dir) and mounts["/brief"].read_only
    writable = [m.source for m in spec.mounts if not m.read_only]
    assert writable == [str(paths.agent_dir)]
    for m in spec.mounts:
        assert not m.source.startswith(str(paths.evidence_dir))
        assert not m.source.startswith(str(paths.snapshots_dir))


def test_an_agreement_changed_before_the_sandbox_starts_fails_the_run(harness: Harness) -> None:
    h = harness
    run_id = launch_with_checks(h)
    checks = h.controller.paths(run_id).frozen_dir / "checks" / "test_version.py"
    os.chmod(checks, 0o644)
    checks.write_text("def test_version():\n    pass\n")
    h.inference_ready()
    h.controller.reconcile_once()
    run = h.state.get_run(run_id)
    assert run is not None and run.phase == "failed"
    op = h.state.get_operation(run_id, "workspace.create")
    assert op is not None and "changed after launch" in str(op.error)
    assert agent_container_name(run_id) not in h.runtime.specs


def test_the_evaluator_gets_the_checks_read_only_and_only_its_own_output(
    harness: Harness, tmp_path: Path
) -> None:
    h = harness
    run_id = launch_with_checks(h)
    [home, *_] = h.state.criteria(run_id)
    ev = h.state.begin_evaluation(
        run_id, trigger="claim", check_digest="d", evidence_root=str(tmp_path), now=h.clock.now()
    )
    assert isinstance(ev, Evaluation)
    checks = h.controller.paths(run_id).frozen_dir / "checks"
    out = tmp_path / "eval-1" / "0-home"
    spec = evaluator_spec(
        h.settings, run_id=run_id, ev=ev, index=0, criterion=home, checks_dir=checks,
        out_dir=out, url="http://sandbox:3000",
    )  # fmt: skip
    argv = docker_run_argv(spec)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert sorted(mounts) == sorted(
        [
            f"type=bind,source={checks},target=/checks,readonly",
            f"type=bind,source={out},target=/out",
        ]
    )
    assert spec.user == "10002:10002" and spec.cap_drop_all and spec.no_new_privileges
    # The egress network only: the demo by name, no model, no Agent Server network.
    assert spec.networks == (h.settings.egress_network,)
    assert h.settings.internal_network not in argv
    assert not spec.ports and not spec.gpus
    assert spec.env["APP_URL"] == "http://sandbox:3000"
    assert spec.command[-1] == "/checks/home.spec.ts"
