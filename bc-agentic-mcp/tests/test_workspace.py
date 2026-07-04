"""Tests for workspace — the single resolver deciding where .specs artifacts live."""
import importlib

import pytest

from bc_agentic_mcp import workspace


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(workspace.ENV_VAR, raising=False)
    yield


def test_colocated_by_default(tmp_path):
    """With no override, artifacts stay inside the repo (backward compatible)."""
    assert workspace.specs_root(tmp_path) == tmp_path / ".specs"
    assert workspace.external_base() is None


def test_external_when_configured(tmp_path, monkeypatch):
    base = tmp_path / "store"
    repo = tmp_path / "ERP AL"
    repo.mkdir()
    monkeypatch.setenv(workspace.ENV_VAR, str(base))

    root = workspace.specs_root(repo)
    # Lives under the external base, NOT inside the repo.
    assert base in root.parents
    assert ".specs" not in str(repo) or root != repo / ".specs"
    assert str(repo) not in str(root)


def test_key_is_stable_across_resolved_and_unresolved(tmp_path, monkeypatch):
    base = tmp_path / "store"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(workspace.ENV_VAR, str(base))

    a = workspace.specs_root(repo)
    b = workspace.specs_root(repo.resolve())
    b2 = workspace.specs_root(str(repo) + "\\")
    assert a == b == b2


def test_distinct_repos_get_distinct_dirs(tmp_path, monkeypatch):
    base = tmp_path / "store"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setenv(workspace.ENV_VAR, str(base))

    assert workspace.specs_root(tmp_path / "a") != workspace.specs_root(tmp_path / "b")


def test_key_folder_name_is_human_navigable(tmp_path, monkeypatch):
    base = tmp_path / "store"
    repo = tmp_path / "ERP AL"
    repo.mkdir()
    monkeypatch.setenv(workspace.ENV_VAR, str(base))

    key = workspace.specs_root(repo).name
    # Starts with a sanitized repo name and carries a short hash suffix.
    assert key.startswith("ERP-AL-")
    assert len(key.split("-")[-1]) == 8
