import subprocess

import pytest

from bc_agentic_mcp.tools.pr_thread_guard import handle_guard_pr_thread_resolution


def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo_with_remote(base, branch="feature/s1"):
    origin = base / "origin.git"
    repo = base / "repo"
    _run(base, "init", "--bare", str(origin))

    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    _run(repo, "checkout", "-b", branch)
    _run(repo, "remote", "add", "origin", str(origin))

    src = repo / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "X.Table.al").write_text("table 50000 X {}", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "baseline")
    _run(repo, "push", "-u", "origin", branch)
    return repo


@pytest.mark.asyncio
async def test_guard_blocks_without_tracking_upstream(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    _run(repo, "checkout", "-b", "feature/s1")

    result = await handle_guard_pr_thread_resolution(
        project_root=str(repo), touched_files=["src/X.Table.al"]
    )
    assert result["allowed"] is False
    assert "tracking upstream" in result["reason"].lower()


@pytest.mark.asyncio
async def test_guard_blocks_without_unpushed_commits(tmp_path):
    repo = _init_repo_with_remote(tmp_path)

    result = await handle_guard_pr_thread_resolution(
        project_root=str(repo), touched_files=["src/X.Table.al"]
    )
    assert result["allowed"] is False
    assert "no unpushed commits" in result["reason"].lower()


@pytest.mark.asyncio
async def test_guard_allows_when_unpushed_covers_touched_files(tmp_path):
    repo = _init_repo_with_remote(tmp_path)
    target = repo / "src" / "X.Table.al"
    target.write_text("table 50000 X { fields {} }", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "update x")

    result = await handle_guard_pr_thread_resolution(
        project_root=str(repo), touched_files=["src/X.Table.al"]
    )
    assert result["allowed"] is True
    assert result["unpushed_commits"] >= 1


@pytest.mark.asyncio
async def test_guard_blocks_when_unpushed_does_not_cover_touched_files(tmp_path):
    repo = _init_repo_with_remote(tmp_path)
    other = repo / "src" / "Other.Table.al"
    other.write_text("table 50001 Other {}", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "update other")

    result = await handle_guard_pr_thread_resolution(
        project_root=str(repo), touched_files=["src/X.Table.al"]
    )
    assert result["allowed"] is False
    assert result["missing_files"] == ["src/X.Table.al"]


@pytest.mark.asyncio
async def test_guard_blocks_on_protected_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    _run(repo, "checkout", "-b", "main")

    result = await handle_guard_pr_thread_resolution(
        project_root=str(repo), touched_files=["src/X.Table.al"]
    )
    assert result["allowed"] is False
    assert "protected" in result["reason"].lower()
