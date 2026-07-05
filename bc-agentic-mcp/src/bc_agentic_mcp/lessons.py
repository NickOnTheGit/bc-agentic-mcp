"""Auto-improver lessons store.

A deterministic, repo-scoped knowledge base that learns from past planning runs.

Design principles:
- Data-driven, not self-modifying code. Lessons are JSON rules the analyzer reads.
- Append/upsert only, with provenance (source, recurrence count, where seen).
- Analyzer findings are auto-recorded as observations; a finding that recurs across
  specs is promoted to a confirmed lesson and surfaced proactively next time.
- Humans can also teach durable lessons that the live analyzer cannot compute.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional

CONFIRM_THRESHOLD = 2
DEFAULT_TTL_DAYS = 90

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "for", "on", "with", "that",
    "this", "be", "are", "it", "as", "at", "by", "if", "not", "when", "then", "must",
}


def _tokens(text: str) -> set:
    return {
        w for w in __import__("re").split(r"[^a-z0-9]+", str(text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def overlap_score(text_a: str, text_b: str) -> float:
    """Deterministic Jaccard token overlap in [0, 1] (dependency-free 'semantic-ish' recall)."""
    a, b = _tokens(text_a), _tokens(text_b)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def _token_list(text: str) -> List[str]:
    return [
        w for w in __import__("re").split(r"[^a-z0-9]+", str(text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    ]


def bm25_scores(query: str, docs: List[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    """Okapi BM25 over a small in-memory corpus — dependency-free lexical ranking.

    Beats set-overlap because rare terms weigh more (IDF) and long documents are
    length-normalized: the right 60-token container lesson outranks a generic
    10-token one. Deterministic; the seam to swap in embeddings later is exactly
    this function.
    """
    import math
    doc_tokens = [_token_list(d) for d in docs]
    n = len(doc_tokens)
    if n == 0:
        return []
    avg_len = sum(len(t) for t in doc_tokens) / n or 1.0
    df: Dict[str, int] = {}
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    q_terms = _token_list(query)
    scores: List[float] = []
    for tokens in doc_tokens:
        tf: Dict[str, int] = {}
        for term in tokens:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log((n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1.0)
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * len(tokens) / avg_len))
        scores.append(round(score, 4))
    return scores


def _lessons_path(root: Path) -> Path:
    return specs_root(root) / ".lessons" / "lessons.json"


def _global_path() -> Path:
    """Cross-project lessons store. Location is env-driven, not hardcoded task data."""
    env = os.environ.get("BC_MCP_GLOBAL_LESSONS")
    if env:
        return Path(env)
    return Path.home() / ".bc-agentic-mcp" / "global-lessons.json"


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def load_lessons(root: Path) -> List[Dict[str, Any]]:
    return _load_json_list(_lessons_path(root))


def load_global_lessons() -> List[Dict[str, Any]]:
    """Cross-project lessons: the local file UNION the team store (if configured).

    The team layer is invisible to consumers — same shape, deduped on
    (signature, message) with local lessons winning ties. This is the single
    read door; wiring the union here means applicable_lessons/BM25 ranking and
    every other consumer learn from teammates with zero changes.
    """
    local = _load_json_list(_global_path())
    try:
        from bc_agentic_mcp import team_lessons
        team = team_lessons.load_team_lessons() if team_lessons.enabled() else []
    except Exception:
        team = []  # the shared layer must never break local operation
    if not team:
        return local
    seen = {(l.get("signature"), l.get("message")) for l in local}
    out = list(local)
    for lesson in team:
        key = (lesson.get("signature"), lesson.get("message"))
        if key in seen:
            continue
        seen.add(key)
        out.append(lesson)
    return out


def record_global_lesson(
    *,
    message: str,
    match: Optional[Dict[str, str]] = None,
    severity: str = "warning",
) -> Dict[str, Any]:
    """Record a durable lesson in the cross-project store (confirmed immediately)."""
    path = _global_path()
    lessons = _load_json_list(path)
    match = match or {}
    key = match.get("api") or match.get("keyword") or ""
    lesson = {
        "id": f"G-{len(lessons) + 1:04d}",
        "signature": _signature("GLOBAL", key),
        "source_type": "global",
        "code": "GLOBAL-LESSON",
        "severity": severity,
        "message": message,
        "match": match,
        "hits": 1,
        "status": "confirmed",
        "created": _now(),
        "last_seen": _now(),
        "seen_in": [],
    }
    # Idempotent on (signature, message) so repeated promotion does not duplicate.
    for existing in lessons:
        if existing.get("signature") == lesson["signature"] and existing.get("message") == message:
            return existing
    lessons.append(lesson)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lessons, indent=2), encoding="utf-8")
    # Tee into the team store (best-effort): promotions AND reflection-distilled
    # mistakes flow to every teammate through this single write door.
    try:
        from bc_agentic_mcp import team_lessons
        if team_lessons.enabled():
            shared = team_lessons.append_team_lesson(lesson)
            lesson["team"] = {k: shared[k] for k in ("recorded", "pushed", "deduped") if k in shared}
    except Exception:
        pass  # sharing is a bonus, never a failure mode
    return lesson


def promote_lesson(root: Path, lesson_id: str) -> Optional[Dict[str, Any]]:
    """Copy a project lesson into the cross-project store so it applies everywhere."""
    for lesson in load_lessons(root):
        if lesson.get("id") == lesson_id:
            return record_global_lesson(
                message=lesson.get("message", ""),
                match=lesson.get("match", {}),
                severity=lesson.get("severity", "warning"),
            )
    return None


def _save(root: Path, lessons: List[Dict[str, Any]]) -> None:
    path = _lessons_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lessons, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(code: str, key: str) -> str:
    return f"{code}::{key}".lower()


def record_observation(
    root: Path,
    *,
    code: str,
    message: str,
    severity: str,
    match: Optional[Dict[str, str]],
    spec_name: str,
    confirm_threshold: int = CONFIRM_THRESHOLD,
) -> Dict[str, Any]:
    """Record an analyzer finding; promote to 'confirmed' once it recurs."""
    lessons = load_lessons(root)
    match = match or {}
    key = match.get("api") or match.get("keyword") or ""
    signature = _signature(code, key)
    now = _now()

    for lesson in lessons:
        if lesson.get("signature") == signature and lesson.get("source_type") == "analyzer":
            lesson["hits"] = int(lesson.get("hits", 1)) + 1
            lesson["last_seen"] = now
            lesson["message"] = message
            lesson["severity"] = severity
            if lesson["hits"] >= confirm_threshold:
                lesson["status"] = "confirmed"
            seen = lesson.setdefault("seen_in", [])
            if spec_name not in seen:
                seen.append(spec_name)
            _save(root, lessons)
            return lesson

    lesson = {
        "id": f"L-{len(lessons) + 1:04d}",
        "signature": signature,
        "source_type": "analyzer",
        "code": code,
        "severity": severity,
        "message": message,
        "match": match,
        "hits": 1,
        "status": "observed",
        "created": now,
        "last_seen": now,
        "seen_in": [spec_name],
    }
    lessons.append(lesson)
    _save(root, lessons)
    return lesson


def record_human_lesson(
    root: Path,
    *,
    message: str,
    match: Optional[Dict[str, str]] = None,
    severity: str = "warning",
) -> Dict[str, Any]:
    """Record a durable, human-taught lesson (confirmed immediately)."""
    lessons = load_lessons(root)
    match = match or {}
    key = match.get("api") or match.get("keyword") or ""
    lesson = {
        "id": f"L-{len(lessons) + 1:04d}",
        "signature": _signature("HUMAN", key),
        "source_type": "human",
        "code": "HUMAN-LESSON",
        "severity": severity,
        "message": message,
        "match": match,
        "hits": 1,
        "status": "confirmed",
        "created": _now(),
        "last_seen": _now(),
        "seen_in": [],
    }
    lessons.append(lesson)
    _save(root, lessons)
    return lesson


def applicable_lessons(
    root: Path,
    *,
    api: str,
    keywords_text: str,
    min_score: float = 0.12,
) -> List[Dict[str, Any]]:
    """Return confirmed lessons that apply to the current spec.

    Project lessons match by exact api/keyword (as before). Cross-project (global) lessons
    are additionally surfaced by BM25 ranking over their messages (rare terms weigh more,
    long lessons are length-normalized), so a lesson learned in one repo can help another
    without embeddings. Read-time decay runs first.
    """
    apply_decay(root)
    api_lower = (api or "").lower()
    text_lower = (keywords_text or "").lower()
    query = f"{api_lower} {text_lower}"
    out: List[Dict[str, Any]] = []
    seen_sig: set = set()
    for lesson in load_lessons(root):
        if lesson.get("status") != "confirmed":
            continue
        match = lesson.get("match", {})
        lesson_api = (match.get("api") or "").lower()
        lesson_keyword = (match.get("keyword") or "").lower()
        if (lesson_api and lesson_api == api_lower) or (lesson_keyword and lesson_keyword in text_lower):
            out.append(lesson)
            seen_sig.add(lesson.get("signature"))
    candidates = [l for l in load_global_lessons()
                  if l.get("status") == "confirmed" and l.get("signature") not in seen_sig]
    scores = bm25_scores(query, [str(l.get("message", "")) for l in candidates])
    top = max(scores) if scores else 0.0
    ranked = sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)
    included = 0
    for lesson, score in ranked:
        match = lesson.get("match", {})
        lesson_api = (match.get("api") or "").lower()
        lesson_keyword = (match.get("keyword") or "").lower()
        exact = (lesson_api and lesson_api == api_lower) or (lesson_keyword and lesson_keyword in text_lower)
        # Exact matches always surface; the rest by BM25 rank — top 5 that clear a
        # relative bar (35% of the best score), so weak tails never flood context.
        relevant = bool(top) and score > 0 and score >= 0.35 * top and included < 5
        if exact or relevant:
            surfaced = dict(lesson)
            surfaced["_score"] = round(score / top, 4) if top else 0.0
            out.append(surfaced)
            if not exact:
                included += 1
    return out


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def apply_decay(
    root: Path,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: Optional[datetime] = None,
) -> int:
    """Demote confirmed analyzer lessons not seen within ttl_days to 'decayed'.

    Human lessons never decay. A decayed lesson is resurrected automatically if it
    recurs (record_observation pushes hits past the confirm threshold again).
    Returns the number of lessons decayed.
    """
    lessons = load_lessons(root)
    if not lessons:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    changed = 0
    for lesson in lessons:
        if lesson.get("source_type") == "human":
            continue
        if lesson.get("status") != "confirmed":
            continue
        seen = _parse_ts(lesson.get("last_seen", ""))
        if seen is not None and seen < cutoff:
            lesson["status"] = "decayed"
            changed += 1
    if changed:
        _save(root, lessons)
    return changed


def summarize_lessons(root: Path) -> Dict[str, Any]:
    """Aggregate counts and the top recurring lessons for a human-readable view."""
    lessons = load_lessons(root)
    by_status: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    by_code: Dict[str, int] = {}
    for lesson in lessons:
        by_status[lesson.get("status", "?")] = by_status.get(lesson.get("status", "?"), 0) + 1
        by_severity[lesson.get("severity", "?")] = by_severity.get(lesson.get("severity", "?"), 0) + 1
        by_code[lesson.get("code", "?")] = by_code.get(lesson.get("code", "?"), 0) + 1
    top = sorted(lessons, key=lambda l: int(l.get("hits", 0)), reverse=True)[:10]
    return {
        "total": len(lessons),
        "by_status": by_status,
        "by_severity": by_severity,
        "by_code": by_code,
        "top": [
            {
                "id": l.get("id"),
                "code": l.get("code"),
                "hits": l.get("hits"),
                "status": l.get("status"),
                "message": l.get("message"),
            }
            for l in top
        ],
    }


def render_lessons_summary(root: Path) -> str:
    summary = summarize_lessons(root)
    lines = [
        "# Lessons Learned Summary",
        "",
        f"- Total lessons: {summary['total']}",
        f"- By status: {summary['by_status']}",
        f"- By severity: {summary['by_severity']}",
        f"- By code: {summary['by_code']}",
        "",
    ]
    if summary["top"]:
        lines.append("## Top recurring")
        for item in summary["top"]:
            lines.append(
                f"- [{item['code']}] hits={item['hits']} status={item['status']} — {item['message']}"
            )
    return "\n".join(lines)
