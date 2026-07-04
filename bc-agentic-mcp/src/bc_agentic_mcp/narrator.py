"""narrator — translate machine state into plain human language, on every response.

The lifecycle speaks in phase names (`spec_written`), engine names (`refinement`) and
tool names (`bc_refine_item`). A human reading a status should never need the glossary:
every spec-scoped response carries a `human` block that says WHERE the item stands,
WHAT happens next and WHO acts — in ordinary sentences. Pure + deterministic; additive
only (nothing existing changes shape).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# phase -> (where the item stands, what happens next, who acts next)
PHASE_STORY: Dict[str, tuple] = {
    "intake_started": (
        "Raw material (email/notes/requirements) landed in the refinement lab.",
        "Analyze it: find similar past work, check the codebase, surface the open questions.",
        "agent",
    ),
    "intake_analyzed": (
        "The evidence dossier is built — precedents, code reality, open questions, lane hint.",
        "Answer the open questions with the human, then graduate into bug / PBI / feature.",
        "human",
    ),
    "intake_graduated": (
        "The refined intake became a real work item in its delivery lane.",
        "Run the standard lifecycle on the new item (diagnosis first if it is a bug).",
        "agent",
    ),
    "item_received": (
        "The work item's full context was captured fresh from Azure DevOps.",
        "The item's claims get checked against the actual code before anything is planned.",
        "agent",
    ),
    "item_refined": (
        "The ticket's claims were checked against the real code (mismatches surfaced, if any).",
        "Write the specification from the verified facts.",
        "agent",
    ),
    "root_cause_identified": (
        "The bug's root cause is diagnosed and every piece of evidence was verified against the code.",
        "Write the fix specification — it must include a test that reproduces the bug "
        "(failing before the fix, passing after).",
        "agent",
    ),
    "spec_written": (
        "The specification (what to build and how to prove it) is written.",
        "Check the ticket's claims against the code if not done yet, then plan the technical design.",
        "agent",
    ),
    "design_planned": (
        "The technical design (which objects change and why) is planned.",
        "Read the surrounding code so the plan is grounded in how the module really works.",
        "agent",
    ),
    "code_context_built": (
        "The relevant source code has been read and summarized as evidence.",
        "Break the design into small, ordered implementation tasks.",
        "agent",
    ),
    "tasks_broken_down": (
        "The work is broken into ordered tasks.",
        "Assemble the review packet so a human can judge the whole plan in one place.",
        "agent",
    ),
    "review_prepared": (
        "The review packet is ready — everything a human needs to approve or reject the plan.",
        "Ask for plan approval. Nothing gets implemented until a human says yes.",
        "agent",
    ),
    "approval_requested": (
        "Plan approval has been requested.",
        "A HUMAN must now review the packet and approve or reject it. The agent waits.",
        "human",
    ),
    "decision_recorded": (
        "A human decision on the plan was recorded.",
        "If approved: write the code (through the guarded write path only).",
        "agent",
    ),
    "plan_approved": (
        "The plan is approved — implementation is authorized.",
        "Write the code through the guarded write path, then run the mistake detector and review.",
        "agent",
    ),
    "implemented": (
        "The code is written (within the approved scope).",
        "Generate tests, then execute them in a real Business Central container for proof.",
        "agent",
    ),
    "tests_generated": (
        "The test plan/scaffold exists.",
        "Run the tests in a local Business Central container — static checks are not proof.",
        "agent",
    ),
    "tests_run": (
        "Tests were executed in a container and results recorded.",
        "Compute the verification verdict from the recorded evidence.",
        "agent",
    ),
    "verified": (
        "The evidence was weighed: every acceptance criterion is matched against real test results.",
        "Prepare the pull request from that evidence.",
        "agent",
    ),
    "reviewed": (
        "An independent review of the diff is recorded.",
        "Prepare the pull request.",
        "agent",
    ),
    "pr_prepared": (
        "The pull request description is prepared from the recorded evidence.",
        "Create the pull request in Azure DevOps.",
        "agent",
    ),
    "pr_created": (
        "The pull request exists in Azure DevOps.",
        "HUMAN reviewers vote on it there; open comments come back as rework.",
        "human",
    ),
    "review_comments_open": (
        "Reviewers left comments that require code changes.",
        "Fix each comment within the approved scope, then resolve the threads.",
        "agent",
    ),
    "merged": (
        "The pull request is merged.",
        "Archive the item and bank the lessons learned.",
        "agent",
    ),
    "archived": (
        "The item is closed and its lessons are stored.",
        "Nothing — the lifecycle is complete.",
        "nobody",
    ),
    "feature_captured": (
        "The whole feature tree (every child item) was captured fresh.",
        "Check every child's claims against the actual code before planning.",
        "agent",
    ),
    "feature_refined": (
        "Every child item's claims were confronted with code reality.",
        "Produce the feature plan (facts plus delivery waves).",
        "agent",
    ),
    "feature_planned": (
        "The feature plan (order of delivery, shared decisions) is written.",
        "A HUMAN approves the feature plan; then items are delivered one by one.",
        "human",
    ),
}

# engine -> what its failure means, in plain words
ENGINE_STORY: Dict[str, str] = {
    "timeline": "the item was never started properly (no lifecycle history)",
    "refinement": "the ticket's claims were not yet checked against the actual code",
    "root_cause": "this is a bug, and its root cause has not been diagnosed with verified evidence yet",
    "traceability": "the specification does not connect every requirement to a test",
    "code_context": "the surrounding source code was not read as evidence yet",
    "quality": "the code analyzers have not passed cleanly yet",
    "clarifications": "open clarification questions still need grounded answers",
}

_WHO_LABEL = {"agent": "I act next.", "human": "YOU act next.", "nobody": ""}


def explain_phase(phase: Optional[str], lane: str = "pbi") -> Optional[Dict[str, str]]:
    """Plain-language WHERE / NEXT / WHO for a lifecycle phase (None when unknown)."""
    story = PHASE_STORY.get(phase or "")
    if story is None:
        return None
    where, nxt, who = story
    out = {"where": where, "next": nxt, "who_acts": _WHO_LABEL.get(who, "")}
    if lane == "bugfix":
        out["lane"] = ("Bugfix lane: diagnose first, then a fix spec with a bug-reproducing "
                       "test, human approval, guarded implementation, container proof.")
    return out


def explain_blockers(engine_result: Dict[str, Any]) -> List[str]:
    """Plain-language sentences for each blocking enforcement engine, in fix order."""
    out: List[str] = []
    for na in engine_result.get("next_actions", []) or []:
        engine = str(na.get("engine", ""))
        tool = str(na.get("tool", ""))
        meaning = ENGINE_STORY.get(engine, f"the '{engine}' check has not passed")
        out.append(f"Blocked because {meaning} — fix it with {tool}.")
    return out
