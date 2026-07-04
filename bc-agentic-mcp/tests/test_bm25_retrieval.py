"""BM25 retrieval tests — the lexical ranking seam that replaced set-overlap."""
import json

from bc_agentic_mcp import lessons


def test_bm25_rare_terms_outweigh_common():
    docs = [
        "container install truths: symbols from filesystem only, stale cache entry deleted",
        "test pyramid is law for every lane: happy negative edge shapes",
        "generic advice about tests and code and process",
    ]
    scores = lessons.bm25_scores("container symbols stale cache", docs)
    assert scores[0] > scores[1] and scores[0] > scores[2]


def test_bm25_length_normalization():
    docs = [
        "container " * 50,  # long spammy doc
        "container publish requires filesystem symbols",  # short precise doc
    ]
    scores = lessons.bm25_scores("container filesystem symbols", docs)
    assert scores[1] > scores[0], "short precise doc must outrank keyword spam"


def test_bm25_empty_inputs():
    assert lessons.bm25_scores("query", []) == []
    assert lessons.bm25_scores("", ["doc one", "doc two"]) == [0.0, 0.0]


def test_applicable_lessons_ranks_globals_by_bm25(tmp_path, monkeypatch):
    global_path = tmp_path / "global-lessons.json"
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(global_path))
    entries = [
        {"signature": f"sig-{i}", "status": "confirmed", "match": {},
         "message": msg, "hits": 3, "created": "2026-01-01", "last_seen": "2026-07-01",
         "severity": "warning", "seen_in": []}
        for i, msg in enumerate([
            "Container install: symbols from filesystem only; stale cache entry deleted.",
            "Test pyramid: happy AND negative AND edge shapes plus regression codeunit.",
            "PR threads: resolve only after the fix is pushed.",
            "Unrelated trivia about wiki formatting styles.",
        ])
    ]
    global_path.write_text(json.dumps(entries), encoding="utf-8")
    out = lessons.applicable_lessons(
        tmp_path, api="", keywords_text="publish symbols to the container filesystem")
    messages = [l["message"] for l in out]
    assert any("filesystem only" in m for m in messages), "the container lesson must surface"
    assert not any("wiki formatting" in m for m in messages), "irrelevant lesson must stay out"
    # Best hit is normalized to 1.0.
    top = max(l.get("_score", 0) for l in out)
    assert top == 1.0
