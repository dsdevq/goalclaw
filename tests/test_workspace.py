"""Workspace prep — real git in tmp (no network, no auth)."""

from __future__ import annotations

import subprocess

import pytest

from goalclaw.workspace import WorkspaceError, prepare_workspace


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _make_origin_and_clone(tmp_path):
    origin = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    repo = str(tmp_path / "repo")
    subprocess.run(["git", "clone", "-q", origin, repo], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (tmp_path / "repo" / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD")
    return origin, repo


def _porcelain(repo) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _branch(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.asyncio
async def test_resets_a_dirty_branched_workspace_to_clean_default(tmp_path):
    _, repo = _make_origin_and_clone(tmp_path)
    # simulate a previous action: a branch, a commit, and stray/untracked files
    _git(repo, "checkout", "-q", "-b", "devclaw/old-work")
    (tmp_path / "repo" / "leftover.txt").write_text("junk\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "old work")
    (tmp_path / "repo" / "untracked.txt").write_text("stray\n")

    base = await prepare_workspace(repo)

    assert base in ("main", "master")
    assert _branch(repo) == base           # back on the default branch
    assert _porcelain(repo) == ""          # pristine — no stray files
    assert not (tmp_path / "repo" / "untracked.txt").exists()
    assert not (tmp_path / "repo" / "leftover.txt").exists()


@pytest.mark.asyncio
async def test_clones_when_missing(tmp_path):
    origin, _ = _make_origin_and_clone(tmp_path)
    fresh = str(tmp_path / "fresh")  # does not exist yet

    base = await prepare_workspace(fresh, repo_url=origin)

    assert base in ("main", "master")
    assert (tmp_path / "fresh" / "base.txt").exists()
    assert _porcelain(fresh) == ""


@pytest.mark.asyncio
async def test_missing_and_no_repo_url_errors(tmp_path):
    with pytest.raises(WorkspaceError):
        await prepare_workspace(str(tmp_path / "nope"), repo_url=None)
