"""Team lesson sync — the shared git-backed store every teammate learns from.

Covers the contracts that make multi-user learning safe:
- disabled = inert (no URL, no clone -> no-ops, no network)
- append is idempotent per author (content hash) and survives push failure
- merge-on-read unions authors, dedupes across them, skips poison lines
- load_global_lessons() surfaces team lessons to consumers with local-wins dedupe
- record_global_lesson() tees into the team store when enabled

Real git repos on disk (tmp_path), no network: the 'remote' is a local bare repo —
the same mechanics as a GitHub remote, deterministic and offline.
"""
import json
import subprocess
from pathlib import Path

import pytest

from bc_agentic_mcp import lessons as lessons_store
from bc_agentic_mcp import team_lessons


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture()
def team_repo(tmp_path, monkeypatch):
    """A local bare 'remote' + configured env; returns (remote_path, clone_dir)."""
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "--quiet", str(remote)], tmp_path)
    seed = tmp_path / "seed"
    _git(["clone", "--quiet", str(remote), str(seed)], tmp_path)
    _git(["-c", "user.name=seed", "-c", "user.email=s@t", "commit", "--allow-empty",
          "--quiet", "-m", "init"], seed)
    _git(["push", "--quiet"], seed)
    clone = tmp_path / "team-lessons"
    monkeypatch.setenv("BC_MCP_TEAM_LESSONS_URL", str(remote))
    monkeypatch.setenv("BC_MCP_TEAM_LESSONS_DIR", str(clone))
    return remote, clone


def _configure_identity(clone: Path) -> None:
    _git(["config", "user.name", "Test User"], clone)
    _git(["config", "user.email", "test@example.com"], clone)


def test_disabled_without_url_or_clone(monkeypatch, tmp_path):
    monkeypatch.delenv("BC_MCP_TEAM_LESSONS_URL", raising=False)
    monkeypatch.setenv("BC_MCP_TEAM_LESSONS_DIR", str(tmp_path / "nope"))
    assert team_lessons.enabled() is False
    assert team_lessons.sync_pull() == {
        "enabled": False, "reason": "BC_MCP_TEAM_LESSONS_URL not set"}
    assert team_lessons.load_team_lessons() == []
    out = team_lessons.append_team_lesson({"message": "x", "signature": "s"})
    assert out["recorded"] is False
    assert team_lessons.status() == {"enabled": False}


def test_sync_pull_clones_then_pulls(team_repo):
    remote, clone = team_repo
    first = team_lessons.sync_pull()
    assert first["synced"] is True and first["action"] == "cloned"
    assert (clone / ".git").exists()
    second = team_lessons.sync_pull()
    assert second["synced"] is True and second["action"] == "pulled"


def test_append_records_pushes_and_dedupes(team_repo):
    remote, clone = team_repo
    team_lessons.sync_pull()
    _configure_identity(clone)
    lesson = {"signature": "global::container", "message": "Fresh containers need bootstrap",
              "severity": "warning"}
    first = team_lessons.append_team_lesson(lesson)
    assert first["recorded"] is True and first["pushed"] is True
    # Same content again -> dedupe, no second line.
    again = team_lessons.append_team_lesson(dict(lesson))
    assert again.get("deduped") is True
    author_file = clone / "lessons" / f"{team_lessons.author_name()}.jsonl"
    lines = [l for l in author_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["author"] == team_lessons.author_name()
    assert record["content_hash"] == first["content_hash"]
    # The push actually reached the 'remote'.
    ahead = _git(["rev-list", "--count", "@{u}..HEAD"], clone)
    assert ahead.stdout.strip() == "0"


def test_merge_on_read_unions_authors_dedupes_and_skips_poison(team_repo):
    remote, clone = team_repo
    team_lessons.sync_pull()
    lessons_dir = clone / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    a = {"signature": "s::a", "message": "lesson A"}
    dup_of_a = {"signature": "s::a", "message": "lesson A"}  # same content, other author
    b = {"signature": "s::b", "message": "lesson B"}
    (lessons_dir / "alice.jsonl").write_text(
        json.dumps(a) + "\n" + "NOT-JSON{{{\n", encoding="utf-8")
    (lessons_dir / "bob.jsonl").write_text(
        json.dumps(dup_of_a) + "\n" + json.dumps(b) + "\n", encoding="utf-8")
    merged = team_lessons.load_team_lessons()
    messages = sorted(l["message"] for l in merged)
    assert messages == ["lesson A", "lesson B"]  # dedupe across authors, poison skipped


def test_load_global_lessons_unions_team_local_wins(team_repo, tmp_path, monkeypatch):
    remote, clone = team_repo
    team_lessons.sync_pull()
    local_path = tmp_path / "global-lessons.json"
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(local_path))
    local_lesson = {"id": "G-0001", "signature": "global::x", "message": "shared msg",
                    "status": "confirmed"}
    local_path.write_text(json.dumps([local_lesson]), encoding="utf-8")
    lessons_dir = clone / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    (lessons_dir / "colleague.jsonl").write_text(
        json.dumps({"signature": "global::x", "message": "shared msg",
                    "author": "colleague"}) + "\n" +
        json.dumps({"signature": "global::y", "message": "only from team",
                    "status": "confirmed", "author": "colleague"}) + "\n",
        encoding="utf-8")
    merged = lessons_store.load_global_lessons()
    by_msg = {l["message"]: l for l in merged}
    assert len(merged) == 2
    assert by_msg["shared msg"]["id"] == "G-0001"  # local copy won the tie
    assert by_msg["only from team"]["author"] == "colleague"


def test_record_global_lesson_tees_to_team(team_repo, tmp_path, monkeypatch):
    remote, clone = team_repo
    team_lessons.sync_pull()
    _configure_identity(clone)
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(tmp_path / "global-lessons.json"))
    lesson = lessons_store.record_global_lesson(
        message="Editable(false) fields reject boundary writes", match={"keyword": "editable"})
    assert lesson["team"]["recorded"] is True
    team = team_lessons.load_team_lessons()
    assert any(l["message"].startswith("Editable(false)") for l in team)


def test_applicable_lessons_surfaces_team_knowledge(team_repo, tmp_path, monkeypatch):
    """The whole point: a colleague's lesson reaches MY spec analysis."""
    remote, clone = team_repo
    team_lessons.sync_pull()
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(tmp_path / "global-lessons.json"))
    (clone / "lessons").mkdir(parents=True, exist_ok=True)
    (clone / "lessons" / "colleague.jsonl").write_text(
        json.dumps({
            "signature": "global::symbolcache", "status": "confirmed",
            "message": "Parallel worktrees poison the shared symbolcache; use per-worktree namespaces",
            "match": {"keyword": "symbolcache"}, "author": "colleague",
        }) + "\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    hits = lessons_store.applicable_lessons(
        project, api="", keywords_text="the symbolcache broke again in the container build")
    assert any("per-worktree" in l["message"] for l in hits)


def test_status_reports_authors_and_pending(team_repo):
    remote, clone = team_repo
    team_lessons.sync_pull()
    _configure_identity(clone)
    team_lessons.append_team_lesson({"signature": "s::1", "message": "m1"})
    st = team_lessons.status()
    assert st["enabled"] is True and st["count"] == 1
    assert st["authors"] == {team_lessons.author_name(): 1}
    assert st["pending_push_commits"] == 0  # pushed synchronously


def test_append_survives_push_failure_then_syncs(team_repo, tmp_path):
    """Offline append: commit lands locally; next sync_pull pushes the stranded commit."""
    remote, clone = team_repo
    team_lessons.sync_pull()
    _configure_identity(clone)
    # Simulate offline: point origin at a void.
    _git(["remote", "set-url", "origin", str(tmp_path / "void.git")], clone)
    out = team_lessons.append_team_lesson({"signature": "s::off", "message": "offline lesson"})
    assert out["recorded"] is True and out["pushed"] is False
    assert team_lessons.status()["pending_push_commits"] == 1
    # Back online.
    _git(["remote", "set-url", "origin", str(remote)], clone)
    synced = team_lessons.sync_pull()
    assert synced["synced"] is True and synced["pushed_pending"] is True
    assert team_lessons.status()["pending_push_commits"] == 0
