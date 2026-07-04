"""bc_auto_clarify — C4 handler: propose evidence-grounded answers; only genuine
ambiguity reaches the human.

Proposals cite their exact captured-context source. ``auto_submit=True`` routes
qualifying proposals through the SAME fenced bc_answer_clarification path (same
validation — an auto-answer must satisfy every rule a human answer must).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from bc_agentic_mcp import auto_clarify
from bc_agentic_mcp.tools.answer_clarification import handle_answer_clarification, _validate_answer
from bc_agentic_mcp.workspace import specs_root


async def handle_auto_clarify(
    project_root: str,
    spec_name: str,
    auto_submit: bool = False,
) -> Dict[str, Any]:
    root = Path(project_root).resolve()
    clar_path = specs_root(root) / spec_name / "clarifications.md"
    if not clar_path.exists():
        return {
            "status": "no_clarifications",
            "reason": "No clarifications.md for this item — nothing to answer.",
            "next_action": {"tool": "bc_clarify", "params_hint": {"spec_name": spec_name}},
        }

    analysis = auto_clarify.analyze(
        str(root), spec_name, clar_path.read_text(encoding="utf-8", errors="replace")
    )
    if analysis["open_questions"] == 0:
        return {"status": "all_answered", "open_questions": 0}

    result: Dict[str, Any] = {
        "status": "proposals_ready" if analysis["proposals"] else "needs_human_only",
        "open_questions": analysis["open_questions"],
        "corpus_paragraphs": analysis["corpus_paragraphs"],
        "proposals": analysis["proposals"],
        "needs_human": analysis["needs_human"],
    }

    if auto_submit and analysis["proposals"]:
        # Only proposals that pass the SAME answer validation are submitted; the rest
        # stay proposals (surfaced for a human/model to complete, e.g. add .al evidence).
        submittable = {
            qid: p["answer"]
            for qid, p in analysis["proposals"].items()
            if not _validate_answer(qid, p["answer"])
        }
        if submittable:
            written = await handle_answer_clarification(
                str(root), spec_name, answers=submittable
            )
            result["auto_submitted"] = sorted(submittable)
            result["submit_result"] = {
                k: written.get(k) for k in ("ok", "written", "issues") if k in written
            }
        result["not_submittable"] = sorted(set(analysis["proposals"]) - set(submittable))

    if analysis["needs_human"]:
        result["next_action"] = {
            "tool": "bc_answer_clarification",
            "reason": (
                f"{len(analysis['needs_human'])} question(s) have no evidence-grounded "
                "answer in the captured context — a human/model must answer them."
            ),
            "params_hint": {
                "spec_name": spec_name,
                "answers": {q["id"]: "<answer + .al evidence>" for q in analysis["needs_human"]},
            },
        }
    return result
