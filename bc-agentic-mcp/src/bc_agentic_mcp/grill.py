"""grill — deterministic background self-challenge ("is there a better solution?").

The agent cannot be trusted to spontaneously doubt itself, and a passive MCP
server cannot speak first. So the grill is scheduled machinery: the cockpit's
RoutineScheduler (action ``grill_sweep``) periodically ASSEMBLES an adversarial
challenge packet for every active mission and records it as a ``grill``
checkpoint. The working agent meets the challenge in-band — checkpoints surface
in ``bc_status`` / recovery context — and answers by recording a
``grill_answer`` checkpoint. An unanswered grill is visible state, not a vibe.

Everything in the packet is deterministic and evidence-derived:
  * fixed first-principles challenges (simpler design? rejected alternative?
    extend instead of create? sibling precedent?)
  * knowledge worklist — the corpus (incl. the vendor layer) re-ranked against
    the Charter: "does article X contradict your approach? read it."
  * applicable lessons — BM25 over the confirmed lessons stores.

The MODEL writes the judgment (the answers); the machine only asks, records,
and tracks answered/unanswered. No auto-generated prose, no LLM in the loop.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import checkpoints as memory

# First-principles challenges asked of EVERY mission (the "grill me" core).
CORE_CHALLENGES: List[Dict[str, str]] = [
    {"id": "simpler",
     "question": "Name one strictly simpler design that still satisfies every acceptance "
                 "criterion — or state the specific criterion that forces the current "
                 "complexity."},
    {"id": "alternative",
     "question": "Which alternative approach was rejected, and what EVIDENCE rejected it? "
                 "'None considered' is itself a finding to record."},
    {"id": "extend_not_create",
     "question": "Could the same outcome be reached by extending an existing object or "
                 "event instead of creating a new one?"},
    {"id": "sibling_precedent",
     "question": "Which sibling object in the repo already solves part of this? Cite it, "
                 "or state why no precedent applies."},
]


def build_grill_packet(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Assemble the deterministic challenge packet for one spec (read-only)."""
    root = Path(project_root).resolve()
    charter = memory.load_charter(root, spec_name) or {}
    purpose = str(charter.get("purpose") or "")
    criteria = [str(c) for c in charter.get("acceptance_criteria") or []]
    query = " ".join([purpose] + criteria).strip()

    challenges: List[Dict[str, Any]] = [dict(c) for c in CORE_CHALLENGES]

    # Knowledge worklist: the corpus is the "better solution" library — every
    # sweep re-ranks it, so newly added articles confront existing plans.
    if query:
        try:
            from bc_agentic_mcp import knowledge
            for art in knowledge.select_articles(root, query, top_k=4):
                challenges.append({
                    "id": f"knowledge:{art.get('path')}",
                    "question": (f"Does the approach honor '{art.get('title') or art.get('path')}'? "
                                 f"Read the article IN FULL and confirm or record a correction."),
                    "article": {"path": art.get("path"), "layer": art.get("layer"),
                                "file": art.get("file")},
                })
        except Exception:  # noqa: BLE001 — advisory, never blocks the sweep
            pass
        try:
            from bc_agentic_mcp import lessons
            for lesson in lessons.applicable_lessons(root, api="", keywords_text=query)[:3]:
                challenges.append({
                    "id": f"lesson:{lesson.get('id')}",
                    "question": (f"Lesson {lesson.get('id')} says: "
                                 f"\"{str(lesson.get('message', ''))[:160]}\" — does the current "
                                 "approach violate it?"),
                })
        except Exception:  # noqa: BLE001
            pass

    return {
        "spec_name": spec_name,
        "purpose": purpose,
        "challenges": challenges,
        "summary": f"self-challenge: {len(challenges)} question(s) pending an evidence-based answer",
        "answer_via": ("Record the answers as ONE checkpoint: bc_checkpoint(spec_name=..., "
                       "kind='grill_answer', summary='<per-challenge verdicts>', "
                       "details={'challenge_ids': [...]}). A changed decision is ALSO a "
                       "'correction' checkpoint (triggers reflection)."),
    }


def record_grill(project_root: Path, spec_name: str, packet: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the challenge as a ``grill`` checkpoint (the in-band prompt)."""
    root = Path(project_root).resolve()
    memory.append_checkpoint(
        root, spec_name,
        kind="grill",
        summary=packet.get("summary", "self-challenge issued"),
        details={"challenge_ids": [c.get("id") for c in packet.get("challenges", [])],
                 "answer_via": packet.get("answer_via")},
    )
    return {"recorded": True, "challenges": len(packet.get("challenges", []))}


def grill_status(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Answered/unanswered state from the checkpoint log (order-based, deterministic)."""
    root = Path(project_root).resolve()
    last_grill: Optional[int] = None
    last_answer: Optional[int] = None
    for i, cp in enumerate(memory.load_checkpoints(root, spec_name)):
        kind = str(cp.get("kind") or "")
        if kind == "grill":
            last_grill = i
        elif kind == "grill_answer":
            last_answer = i
    answered = last_grill is None or (last_answer is not None and last_answer > last_grill)
    return {"grilled": last_grill is not None, "answered": answered}
