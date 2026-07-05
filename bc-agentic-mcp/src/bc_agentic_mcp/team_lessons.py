"""Team lesson sync — a git-backed shared store so every user's machine learns from all.

Design (agreed 2026-07-05):
- The canonical store is a git repo (env ``BC_MCP_TEAM_LESSONS_URL``); each author
  appends ONLY to their own ``lessons/<author>.jsonl`` — append-only + one file per
  writer = zero merge conflicts (belt: ``.gitattributes`` merge=union in the repo).
- Merge-on-read: ``load_team_lessons()`` unions all authors' files, deduped by
  content hash. Consumers never know the difference — the union is exposed through
  ``lessons.load_global_lessons()``, the single existing read door.
- Write door: ``lessons.record_global_lesson()`` (promotions AND reflection-distilled
  mistakes) tees into ``append_team_lesson()`` best-effort. A failed push is never an
  error: the commit stays local and rides out with the next append or warmup sync.
- No URL configured -> the module is inert (all functions are cheap no-ops). The MCP
  never gains a network dependency it wasn't given.

Deliberately NOT here: real-time sync (lessons propagate across days, not ms),
databases (team-scale corpus is single-digit MB), and runtime web access.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_GIT_TIMEOUT = 25  # seconds; sync must never hang a warmup thread indefinitely


def team_url() -> Optional[str]:
    url = (os.environ.get("BC_MCP_TEAM_LESSONS_URL") or "").strip()
    return url or None


def team_dir() -> Path:
    env = os.environ.get("BC_MCP_TEAM_LESSONS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".bc-agentic-mcp" / "team-lessons"


def enabled() -> bool:
    """Active when a URL is configured OR a clone already exists (offline reads work)."""
    return bool(team_url()) or (team_dir() / ".git").exists()


def author_name() -> str:
    """Stable, filename-safe author id: git user.name, else OS username."""
    raw = ""
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, timeout=10,
        )
        raw = (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        raw = ""
    raw = raw or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug or "unknown"


def content_hash(lesson: Dict[str, Any]) -> str:
    """Identity of a lesson = what it teaches, not who/when recorded it."""
    basis = f"{lesson.get('signature', '')}\n{lesson.get('message', '')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=_GIT_TIMEOUT,
    )


def _author_file(root: Path) -> Path:
    return root / "lessons" / f"{author_name()}.jsonl"


def sync_pull() -> Dict[str, Any]:
    """Clone-or-pull the team store; push any local commits stranded by past offline
    appends. Never raises — offline just means running on yesterday's knowledge."""
    url = team_url()
    root = team_dir()
    if not url and not (root / ".git").exists():
        return {"enabled": False, "reason": "BC_MCP_TEAM_LESSONS_URL not set"}
    try:
        if not (root / ".git").exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "clone", "--quiet", url, str(root)],
                capture_output=True, text=True, timeout=_GIT_TIMEOUT * 2,
            )
            if proc.returncode != 0:
                return {"enabled": True, "synced": False,
                        "reason": f"clone failed: {(proc.stderr or '').strip()[:200]}"}
            return {"enabled": True, "synced": True, "action": "cloned"}
        pull = _git(["pull", "--rebase", "--quiet"], root)
        pushed = False
        ahead = _git(["rev-list", "--count", "@{u}..HEAD"], root)
        if ahead.returncode == 0 and (ahead.stdout or "").strip() not in ("", "0"):
            pushed = _git(["push", "--quiet"], root).returncode == 0
        return {
            "enabled": True,
            "synced": pull.returncode == 0,
            "action": "pulled",
            "pushed_pending": pushed,
            **({"reason": (pull.stderr or "").strip()[:200]} if pull.returncode != 0 else {}),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"enabled": True, "synced": False, "reason": str(exc)[:200]}


def load_team_lessons() -> List[Dict[str, Any]]:
    """Union of every author's file, deduped by content hash (first occurrence wins).
    Pure local reads — sync freshness is warmup's job, not the read path's."""
    root = team_dir()
    lessons_dir = root / "lessons"
    if not lessons_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for path in sorted(lessons_dir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lesson = json.loads(line)
            except json.JSONDecodeError:
                continue  # one bad line must not poison the corpus
            if not isinstance(lesson, dict):
                continue
            h = lesson.get("content_hash") or content_hash(lesson)
            if h in seen:
                continue
            seen.add(h)
            out.append(lesson)
    return out


def append_team_lesson(lesson: Dict[str, Any]) -> Dict[str, Any]:
    """Append one lesson to the author's own file; commit; push best-effort.

    Idempotent on content hash within the author file. A push failure downgrades to
    'recorded locally' — eventual consistency via the next append or sync_pull.
    """
    if not enabled():
        return {"recorded": False, "reason": "team store not configured"}
    root = team_dir()
    if not (root / ".git").exists():
        status = sync_pull()
        if not (root / ".git").exists():
            return {"recorded": False, "reason": status.get("reason", "no clone")}
    record = dict(lesson)
    record["content_hash"] = content_hash(record)
    record["author"] = author_name()
    record.setdefault("shared", datetime.now(timezone.utc).isoformat())
    path = _author_file(root)
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if f'"{record["content_hash"]}"' in existing:
            return {"recorded": True, "deduped": True, "content_hash": record["content_hash"]}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _git(["add", str(path)], root)
        msg = f"lesson({record['author']}): {str(record.get('message', ''))[:60]}"
        commit = _git(["commit", "--quiet", "-m", msg], root)
        pushed = False
        if commit.returncode == 0:
            pushed = _git(["push", "--quiet"], root).returncode == 0
        return {"recorded": True, "pushed": pushed, "content_hash": record["content_hash"]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"recorded": False, "reason": str(exc)[:200]}


def status() -> Dict[str, Any]:
    """Observability for bc_lessons: who contributes, how much, and what's unpushed."""
    if not enabled():
        return {"enabled": False}
    root = team_dir()
    lessons = load_team_lessons()
    authors: Dict[str, int] = {}
    for lesson in lessons:
        a = str(lesson.get("author", "unknown"))
        authors[a] = authors.get(a, 0) + 1
    pending = 0
    if (root / ".git").exists():
        ahead = _git(["rev-list", "--count", "@{u}..HEAD"], root)
        if ahead.returncode == 0:
            try:
                pending = int((ahead.stdout or "0").strip())
            except ValueError:
                pending = 0
    return {"enabled": True, "dir": str(root), "count": len(lessons),
            "authors": authors, "pending_push_commits": pending}
