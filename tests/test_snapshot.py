"""Project snapshots with the real git CLI, in a controller-owned git directory."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from dgx_autonomy.snapshot import GitSnapshots, SnapshotError


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "agent" / "project"
    (p / "src").mkdir(parents=True)
    (p / "index.html").write_text("hello\n")
    (p / "src" / "app.js").write_text("console.log(1)\n")
    return p


def test_the_same_content_has_the_same_id_and_a_change_a_new_one(
    tmp_path: Path, project: Path
) -> None:
    git, store = GitSnapshots(), tmp_path / "snapshots.git"
    a = git.take(store, project, "one")
    assert git.take(store, project, "two").tree == a.tree
    (project / "index.html").write_text("hello v2\n")
    b = git.take(store, project, "three")
    assert b.tree != a.tree and b.commit != a.commit
    assert git.changed(store, a.commit, b.commit) == ["M\tindex.html"]
    # mtimes are the agent's to set; content decides.
    os.utime(project / "index.html", (0, 0))
    assert git.take(store, project, "four").tree == b.tree


def test_caches_and_ignored_files_do_not_identify_the_project(
    tmp_path: Path, project: Path
) -> None:
    git, store = GitSnapshots(), tmp_path / "snapshots.git"
    before = git.take(store, project, "before").tree
    (project / "node_modules" / "left-pad").mkdir(parents=True)
    (project / "node_modules" / "left-pad" / "index.js").write_text("x")
    (project / ".next").mkdir()
    (project / ".next" / "cache").write_text("x")
    assert git.take(store, project, "after").tree == before
    (project / ".gitignore").write_text("dist/\n")
    with_ignore = git.take(store, project, "ignore").tree
    (project / "dist").mkdir()
    (project / "dist" / "bundle.js").write_text("x")
    assert git.take(store, project, "dist").tree == with_ignore


def test_symlinks_are_recorded_not_followed(tmp_path: Path, project: Path) -> None:
    secret = tmp_path / "state" / "controller.sqlite3"
    secret.parent.mkdir()
    secret.write_text("controller secrets")
    (project / "escape").symlink_to(secret.parent)
    git, store = GitSnapshots(), tmp_path / "snapshots.git"
    snap = git.take(store, project, "links")
    out = tmp_path / "project.tar"
    git.archive(store, snap.commit, out)
    with tarfile.open(out) as tar:
        member = tar.getmember("escape")
        assert member.issym() and member.linkname == str(secret.parent)
        assert not any("controller" in n for n in tar.getnames())


def test_the_agents_own_repository_config_and_hooks_never_run(
    tmp_path: Path, project: Path
) -> None:
    marker = tmp_path / "pwned"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    agent_git = project / ".git"
    (agent_git / "hooks").mkdir(parents=True)
    (agent_git / "config").write_text(
        f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {agent_git / 'hooks'}\n"
    )
    for name in ("pre-commit", "post-commit", "reference-transaction"):
        (agent_git / "hooks" / name).write_text(hook.read_text())
        (agent_git / "hooks" / name).chmod(0o755)
    git, store = GitSnapshots(), tmp_path / "snapshots.git"
    git.take(store, project, "with an agent repo")
    assert not marker.exists()
    tree = git.take(store, project, "again").commit
    out = tmp_path / "p.tar"
    git.archive(store, tree, out)
    with tarfile.open(out) as tar:
        assert not any(n == ".git" or n.startswith(".git/") for n in tar.getnames())


def test_a_project_swapped_for_a_symlink_is_refused(tmp_path: Path, project: Path) -> None:
    moved = tmp_path / "elsewhere"
    project.rename(moved)
    project.symlink_to(moved)
    with pytest.raises(SnapshotError, match="not a directory"):
        GitSnapshots().take(tmp_path / "snapshots.git", project, "x")
