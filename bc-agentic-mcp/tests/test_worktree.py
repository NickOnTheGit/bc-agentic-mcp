"""bc_worktree tests — real git repos in tmp dirs, no mocks."""
import subprocess

import pytest

from bc_agentic_mcp.tools.worktree import handle_worktree, load_record


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A real git repo with one commit + external specs root (parallel-safe)."""
    monkeypatch.setenv("BC_AGENTIC_SPECS_ROOT", str(tmp_path / "workspaces"))
    root = tmp_path / "repo"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (root / "README.md").write_text("x", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    return root


def test_create_status_remove_roundtrip(repo, tmp_path):
    base = tmp_path / "wts"
    created = handle_worktree(str(repo), "create", spec_name="wi1-item",
                              worktrees_base=str(base))
    assert created["status"] == "created", created
    assert created["branch"] == "agent/wi1-item"
    wt = base / "wt-wi1-item"
    assert wt.is_dir()
    assert load_record(repo, "wi1-item")["path"] == str(wt)

    status = handle_worktree(str(repo), "status", spec_name="wi1-item")
    assert status["status"] == "ok" and status["exists"] is True

    listing = handle_worktree(str(repo), "list")
    assert any(w["path"].endswith("wt-wi1-item") for w in listing["worktrees"])

    removed = handle_worktree(str(repo), "remove", spec_name="wi1-item")
    assert removed["status"] == "removed"
    assert not wt.exists()
    assert load_record(repo, "wi1-item") is None


def test_create_is_idempotent(repo, tmp_path):
    base = str(tmp_path / "wts")
    first = handle_worktree(str(repo), "create", spec_name="wi2-item", worktrees_base=base)
    again = handle_worktree(str(repo), "create", spec_name="wi2-item", worktrees_base=base)
    assert first["status"] == "created"
    assert again["status"] == "already_exists"
    assert again["path"] == first["path"]


def test_remove_refuses_dirty_worktree_without_force(repo, tmp_path):
    base = tmp_path / "wts"
    created = handle_worktree(str(repo), "create", spec_name="wi3-item",
                              worktrees_base=str(base))
    wt = base / "wt-wi3-item"
    (wt / "uncommitted.al").write_text("codeunit 1 X {}", encoding="utf-8")
    blocked = handle_worktree(str(repo), "remove", spec_name="wi3-item")
    assert blocked["status"] == "blocked_dirty"
    assert wt.exists()
    forced = handle_worktree(str(repo), "remove", spec_name="wi3-item", force=True)
    assert forced["status"] == "removed"


def test_errors_are_structured(repo):
    assert handle_worktree(str(repo), "create")["status"] == "error"          # no spec
    assert handle_worktree(str(repo), "explode", spec_name="x")["status"] == "error"
    assert handle_worktree(str(repo / "nope"), "list")["status"] == "error"   # not a repo
