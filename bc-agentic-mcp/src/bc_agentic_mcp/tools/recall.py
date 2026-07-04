"""bc_recall — re-anchor on a spec's durable Charter + recent checkpoints.

Read this before making changes so the item's PURPOSE and in-scope OPERATIONS are
recovered from disk rather than from (rot-prone) conversational memory.
"""
from pathlib import Path
from typing import Any, Dict, Optional

from bc_agentic_mcp import checkpoints as ckpt


def _render_reanchor(digest: Dict[str, Any]) -> str:
    charter = digest.get("charter") or {}
    if not digest.get("found"):
        return f"No charter recorded for spec '{digest.get('spec_name')}'. Run bc_prepare_review first."
    ops = charter.get("operations", {})
    ops_line = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in ops.items()) or "(unspecified)"
    lines = [
        f"PURPOSE: {charter.get('purpose', '(none)')}",
        f"OPERATIONS IN SCOPE: {ops_line}",
        "ACCEPTANCE CRITERIA:",
    ]
    lines += [f"  - {c}" for c in charter.get("acceptance_criteria", [])] or ["  - (none)"]
    non_goals = charter.get("non_goals", [])
    if non_goals:
        lines += ["NON-GOALS:"] + [f"  - {n}" for n in non_goals]
    checkpoints = digest.get("recent_checkpoints", [])
    if checkpoints:
        lines += ["RECENT CHECKPOINTS:"]
        for entry in checkpoints:
            lines.append(f"  #{entry.get('seq')} [{entry.get('kind')}] {entry.get('summary')}")
    return "\n".join(lines)


async def handle_recall(
    project_root: str,
    spec_name: str,
    checkpoint_limit: int = 8,
) -> Dict[str, Any]:
    """Return the durable Charter + recent checkpoints for a spec (re-anchoring digest)."""
    root = Path(project_root).resolve()
    digest = ckpt.recall_digest(root, spec_name, checkpoint_limit=checkpoint_limit)
    digest["reanchor"] = _render_reanchor(digest)
    return digest


async def handle_checkpoint(
    project_root: str,
    spec_name: str,
    summary: str,
    kind: str = "decision",
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append a durable checkpoint (decision / milestone) to a spec's log."""
    root = Path(project_root).resolve()
    entry = ckpt.append_checkpoint(root, spec_name, kind=kind, summary=summary, details=details)
    result: Dict[str, Any] = {"recorded": True, "checkpoint": entry}
    if kind == "scope_change":
        # A recorded scope change re-opens PLANNING: the spec/packet no longer match
        # the agreed scope, so plan-stage tools (bc_write_spec, bc_prepare_review)
        # must be callable again and any prior gate approval must be re-earned.
        # Without this the lifecycle has no legal way back from implement to plan.
        result["_timeline_phase"] = "review_prepared"
        result["scope_change_effect"] = (
            "planning re-opened: regenerate spec + review packet for the new scope, "
            "then re-approve the plan gate (bc_submit_decision)"
        )
    return result
