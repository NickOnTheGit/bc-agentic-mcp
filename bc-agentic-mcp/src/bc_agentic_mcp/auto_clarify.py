"""auto_clarify — C4: answer clarification questions from CAPTURED evidence.

Deterministic, no-model matching: each open question's significant keywords are
scored against every paragraph of the item's captured context bundle (description,
comments, wiki pages, related items — all fetched fresh by bc_capture_item_context).
A paragraph that matches strongly enough becomes a PROPOSED answer with its exact
source path; questions with no strong match are the ONLY ones that reach the human.

Proposals are never silently written: they flow through the same fenced
``bc_answer_clarification`` path (same validation) when explicitly auto-submitted.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import item_context

# Minimum distinct keyword hits for a paragraph to qualify as an evidence-grounded answer.
MIN_KEYWORD_HITS = 2

_QUESTION_RE = re.compile(r"^##\s+(Q-\d{3}):\s*(.+)$", re.MULTILINE)
_ANSWER_RE = re.compile(r"^_Answer:_\s*(.*)$", re.MULTILINE)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "not", "for", "from", "with", "this", "that",
    "what", "which", "should", "would", "could", "does", "how", "are", "is", "be",
    "there", "any", "list", "apply", "specific", "when", "where", "who", "will",
    "can", "must", "may", "have", "has", "was", "were", "been", "being", "yes",
    "user", "users", "please", "into", "onto", "about", "than", "then", "them",
}


def parse_open_questions(clar_text: str) -> List[Dict[str, str]]:
    """Return [{id, question}] for every question whose _Answer:_ line is empty.

    Image-transcription questions (Q-95x band) are EXCLUDED: keyword matching over
    text cannot read pixels — auto-answering them would be fabrication. They must
    be answered by a seeing agent or a human (image wall, 2026-07-06).
    """
    out: List[Dict[str, str]] = []
    blocks = _QUESTION_RE.split(clar_text)
    # split yields: [prefix, id1, q1, body1, id2, q2, body2, ...]
    for i in range(1, len(blocks) - 2, 3):
        qid, question, body = blocks[i], blocks[i + 1].strip(), blocks[i + 2]
        if qid.startswith("Q-95"):
            continue
        m = _ANSWER_RE.search(body)
        answered = bool(m and m.group(1).strip())
        if not answered:
            out.append({"id": qid, "question": question})
    return out


def keywords(question: str) -> List[str]:
    """Significant, order-stable, deduplicated lowercase tokens of the question."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", question.lower())
    seen: List[str] = []
    for t in tokens:
        if t not in _STOPWORDS and t not in seen:
            seen.append(t)
    return seen


def collect_corpus(context_dir: Path) -> List[Dict[str, str]]:
    """Paragraphs of every captured context artifact, each tagged with its source path."""
    corpus: List[Dict[str, str]] = []
    if not context_dir.is_dir():
        return corpus
    for path in sorted(context_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(context_dir)).replace("\\", "/")
        for para in re.split(r"\n\s*\n", text):
            cleaned = para.strip()
            if len(cleaned) >= 40:  # ignore headings/fragments
                corpus.append({"source": rel, "paragraph": cleaned})
    return corpus


def propose(
    questions: List[Dict[str, str]],
    corpus: List[Dict[str, str]],
    *,
    min_hits: int = MIN_KEYWORD_HITS,
) -> Dict[str, Any]:
    """Best-matching paragraph per question; deterministic ties -> first (stable order)."""
    proposals: Dict[str, Dict[str, Any]] = {}
    needs_human: List[Dict[str, str]] = []
    for q in questions:
        kws = keywords(q["question"])
        best: Optional[Dict[str, Any]] = None
        for entry in corpus:
            para_lower = entry["paragraph"].lower()
            hits = [k for k in kws if k in para_lower]
            if len(hits) >= min_hits and (best is None or len(hits) > best["hits"]):
                best = {"hits": len(hits), "matched": hits, **entry}
        if best is None:
            needs_human.append(q)
        else:
            answer = best["paragraph"]
            if len(answer) > 500:
                answer = answer[:497] + "..."
            proposals[q["id"]] = {
                "answer": f"{answer} [source: context/{best['source']}]",
                "source": f"context/{best['source']}",
                "matched_keywords": best["matched"],
                "score": best["hits"],
            }
    return {"proposals": proposals, "needs_human": needs_human}


def analyze(project_root: str, spec_name: str, clar_text: str) -> Dict[str, Any]:
    """Full C4 pass: open questions x captured-context corpus -> proposals + residue."""
    questions = parse_open_questions(clar_text)
    if not questions:
        return {"proposals": {}, "needs_human": [], "open_questions": 0, "corpus_paragraphs": 0}
    cdir = item_context.context_dir(Path(project_root).resolve(), spec_name)
    corpus = collect_corpus(cdir)
    result = propose(questions, corpus)
    result["open_questions"] = len(questions)
    result["corpus_paragraphs"] = len(corpus)
    return result
