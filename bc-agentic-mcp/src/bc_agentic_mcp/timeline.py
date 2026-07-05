"""timeline — a per-item lifecycle timeline, persisted on disk in the item's folder.

Design (deliberately NOT a second store): the timeline is a coarse, phase-level VIEW over
the existing episodic memory. Each lifecycle step (item received -> context -> spec ->
design -> code-context -> tasks -> review -> decision -> implement -> tests -> verify)
is recorded as a ``kind="phase"`` checkpoint in the SAME ``checkpoints.jsonl`` that already
lives in ``.../<item>/`` (see checkpoints.py). From those we derive a human-readable
``TIMELINE.md`` and a compact digest that the server re-injects into every response, so the
narrative of "what happened to this item so far" is always in context.

Single source of truth = checkpoints.jsonl. This module only adds phase events, a derived
Markdown view, and a digest — no duplicate log.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp.workspace import specs_root

# Canonical lifecycle phases, in order. Maps an MCP tool name to the phase it completes.
TOOL_PHASE: Dict[str, str] = {
    "bc_capture_item_context": "item_received",
    "bc_write_spec": "spec_written",
    "bc_plan_design": "design_planned",
    "bc_read_code_context": "code_context_built",
    "bc_breakdown_tasks": "tasks_broken_down",
    "bc_prepare_review": "review_prepared",
    "bc_request_approval": "approval_requested",
    "bc_submit_decision": "decision_recorded",
    "bc_implement": "implemented",
    "bc_implement_write": "implemented",
    "bc_implement_delete": "implemented",
    "bc_generate_tests": "tests_generated",
    "bc_run_tests": "tests_run",
    "bc_verify": "verified",
    "bc_review": "reviewed",
    "bc_prepare_pr": "pr_prepared",
    "bc_create_pr": "pr_created",
    # bc_get_review_comments / bc_merge_status deliberately absent: their phase depends
    # on ADO ground truth and arrives via the result's `_timeline_phase` marker.
    "bc_archive": "archived",
    # Feature tier (Workstream H): a feature folder is an ordinary spec folder.
    "bc_capture_feature": "feature_captured",
    "bc_refine_feature": "feature_refined",
    "bc_plan_feature": "feature_planned",
    "bc_refine_item": "item_refined",
    # Bugfix lane: diagnosis-before-planning (symptom -> verified root cause).
    "bc_root_cause": "root_cause_identified",
    # Refinement lab (intake tier): raw material -> dossier -> graduation.
    "bc_intake_start": "intake_started",
    "bc_intake_analyze": "intake_analyzed",
    "bc_intake_graduate": "intake_graduated",
}

# Checkpoint kinds that make up the human timeline narrative (phase events + notable signals).
_NARRATIVE_KINDS = {"phase", "milestone", "mistake", "correction", "gate", "decision", "reflection", "override", "artifact"}

_PHASE_LABEL = {
    "item_received": "Item received — timeline started",
    "spec_written": "Spec written",
    "design_planned": "Technical design planned",
    "code_context_built": "Code read-context built",
    "tasks_broken_down": "Tasks broken down",
    "review_prepared": "Review package prepared",
    "approval_requested": "Approval requested",
    "decision_recorded": "Human decision recorded",
    "plan_approved": "Plan gate approved — implementation authorized",
    "implemented": "Implementation written",
    "tests_generated": "Tests generated",
    "tests_run": "Tests run",
    "verified": "Verification computed",
    "reviewed": "Independent review",
    "pr_prepared": "PR description prepared from evidence",
    "pr_created": "Pull request created in ADO",
    "review_comments_open": "PR review comments open — rework loop",
    "merged": "PR merged",
    "archived": "Archived",
    "feature_captured": "Feature tree captured (all children, fresh)",
    "feature_refined": "Feature refined — claims confronted with code reality",
    "feature_planned": "Feature plan generated (facts + wave narrative)",
    "item_refined": "Item refined — claims confronted with code reality",
    "root_cause_identified": "Root cause identified — evidence verified against code",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_dir(project_root: Path, spec_name: str) -> Path:
    return specs_root(project_root) / spec_name


def _artifacts_from_result(result: Any) -> List[str]:
    """Best-effort extraction of the artifact path(s) a tool produced, for the timeline entry."""
    if not isinstance(result, dict):
        return []
    keys = (
        "spec_path", "tdd_path", "design_path", "review_path", "charter_path", "approval_path",
        "verification_path", "report_path", "plan_path", "code_context_path", "summary_path",
        "timeline_path", "tasks_path", "pr_path", "pr_record_path",
    )
    out: List[str] = []
    for k in keys:
        v = result.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    return out


def record_phase(
    project_root: Path,
    spec_name: str,
    phase: str,
    *,
    summary: Optional[str] = None,
    artifacts: Optional[List[str]] = None,
    status: str = "done",
) -> Optional[Dict[str, Any]]:
    """Append a phase event (as a ``kind="phase"`` checkpoint) and regenerate TIMELINE.md.

    Idempotent against noise: a phase event identical to the immediately preceding phase
    event (same phase + summary) is skipped, so re-reading state does not spam the timeline.
    Re-runs that change something still record (different artifacts/summary).
    """
    root = Path(project_root).resolve()
    label = summary or _PHASE_LABEL.get(phase, phase.replace("_", " ").title())

    existing = memory.load_checkpoints(root, spec_name)
    last_phase = next((c for c in reversed(existing) if c.get("kind") == "phase"), None)
    if last_phase is not None:
        details = last_phase.get("details", {})
        if details.get("phase") == phase and last_phase.get("summary") == label and status == "done":
            return None  # no-op: identical consecutive phase

    entry = memory.append_checkpoint(
        root, spec_name, kind="phase", summary=label,
        details={"phase": phase, "status": status, "artifacts": artifacts or []},
    )
    _write_timeline_md(root, spec_name)
    _write_item_md(root, spec_name)
    return entry


def record_tool_phase(project_root: Any, spec_name: Any, tool_name: str, result: Any) -> None:
    """Map a completed tool to its lifecycle phase and record it. Best-effort, never raises.

    A result may carry ``_timeline_phase`` — ground truth observed by the tool (e.g.
    open PR threads, merged PR) — which wins over the static tool->phase map.
    """
    phase = None
    if isinstance(result, dict):
        # A REFUSED tool never advances the lifecycle: a blocked submit_decision is
        # not a recorded decision (observed live: blocked approve wrote
        # 'decision_recorded', flipping the stage to verify and locking out the
        # very plan tools the blocker prescribed as the fix).
        status = str(result.get("status", ""))
        if result.get("isError") is True or status.startswith(("blocked", "error", "failed")):
            return
        candidate = result.get("_timeline_phase")
        if isinstance(candidate, str) and candidate:
            phase = candidate
    phase = phase or TOOL_PHASE.get(tool_name)
    if not phase or not spec_name or not project_root:
        return
    try:
        record_phase(
            Path(str(project_root)).resolve(), str(spec_name), phase,
            artifacts=_artifacts_from_result(result),
        )
    except Exception:
        return


def load_timeline(project_root: Path, spec_name: str) -> List[Dict[str, Any]]:
    """Ordered lifecycle events (phase + notable signals) that make up the item's story."""
    root = Path(project_root).resolve()
    events = [c for c in memory.load_checkpoints(root, spec_name) if c.get("kind") in _NARRATIVE_KINDS]
    return sorted(events, key=lambda c: c.get("seq", 0))


def current_phase(project_root: Path, spec_name: str) -> Optional[str]:
    root = Path(project_root).resolve()
    for c in reversed(memory.load_checkpoints(root, spec_name)):
        if c.get("kind") == "phase":
            return c.get("details", {}).get("phase")
    return None


def latest_phase_in(project_root: Path, spec_name: str, allowed: set) -> Optional[str]:
    """Latest phase that belongs to `allowed`, skipping evidence-only phases.

    Stage inference must not regress when an evidence event (item_refined,
    checkpointed, ...) lands after a lifecycle transition — observed live:
    implemented -> reviewed -> item_refined knocked a spec back to stage 'plan'
    and policy-blocked bc_run_tests mid-verification.
    """
    root = Path(project_root).resolve()
    for c in reversed(memory.load_checkpoints(root, spec_name)):
        if c.get("kind") != "phase":
            continue
        phase = c.get("details", {}).get("phase")
        if phase in allowed:
            return phase
    return None


def phases_in_order(project_root: Path, spec_name: str, allowed: set) -> List[str]:
    """All phases from `allowed`, oldest first (for monotonic stage reasoning)."""
    root = Path(project_root).resolve()
    out: List[str] = []
    for c in memory.load_checkpoints(root, spec_name):
        if c.get("kind") != "phase":
            continue
        phase = c.get("details", {}).get("phase")
        if phase in allowed:
            out.append(phase)
    return out


def digest(project_root: Path, spec_name: str, limit: int = 6) -> Optional[Dict[str, Any]]:
    """Compact timeline for re-injection into context. None when nothing has happened yet."""
    events = load_timeline(project_root, spec_name)
    if not events:
        return None
    phase = None
    for e in reversed(events):
        if e.get("kind") == "phase":
            phase = e.get("details", {}).get("phase")
            break
    recent = [
        {"ts": e.get("ts"), "kind": e.get("kind"), "summary": e.get("summary")}
        for e in events[-limit:]
    ]
    return {"current_phase": phase, "event_count": len(events), "recent": recent}


def _render_timeline_md(spec_name: str, events: List[Dict[str, Any]]) -> str:
    lines = [
        f"# Timeline: {spec_name}",
        "",
        "> Chronological lifecycle of this item. Derived from checkpoints.jsonl (single source of",
        "> truth). Re-read to recover exactly what has been done so far.",
        "",
    ]
    if not events:
        lines.append("_(no events yet)_")
        return "\n".join(lines) + "\n"
    for e in events:
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        kind = e.get("kind", "")
        summary = e.get("summary", "")
        marker = {"phase": "▶", "mistake": "✗", "correction": "↺", "gate": "⛔",
                  "decision": "✓", "reflection": "★", "override": "!", "milestone": "●",
                  "artifact": "💾"}.get(kind, "•")
        line = f"- `{ts}` {marker} **{summary}**"
        artifacts = e.get("details", {}).get("artifacts") or []
        if artifacts:
            line += "  \n  " + "  \n  ".join(f"↳ `{a}`" for a in artifacts)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _write_timeline_md(project_root: Path, spec_name: str) -> str:
    root = Path(project_root).resolve()
    events = load_timeline(root, spec_name)
    directory = _item_dir(root, spec_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "TIMELINE.md"
    path.write_text(_render_timeline_md(spec_name, events), encoding="utf-8")
    return str(path)


ITEM_MD_MAX_BYTES = 1024


def render_item_md(spec_name: str, events: List[Dict[str, Any]]) -> str:
    """D4: ONE <=1KB reanchor digest — the first thing to re-read after any context loss.

    Deliberately tiny and boring: current phase, the last few events, the latest
    artifacts. Derived from checkpoints.jsonl (single source of truth), regenerated on
    every phase record, hard-capped at ITEM_MD_MAX_BYTES so it can never bloat.
    """
    phase = None
    artifacts: List[str] = []
    for e in reversed(events):
        if e.get("kind") == "phase":
            details = e.get("details", {})
            phase = phase or details.get("phase")
            if not artifacts:
                artifacts = list(details.get("artifacts") or [])
            if phase and artifacts:
                break
    lines = [
        f"# {spec_name}",
        f"phase: {phase or 'none'}",
        f"events: {len(events)}",
        "recent:",
    ]
    for e in events[-4:]:
        ts = (e.get("ts") or "")[:16].replace("T", " ")
        lines.append(f"- {ts} [{e.get('kind')}] {str(e.get('summary', ''))[:80]}")
    if artifacts:
        lines.append("artifacts:")
        lines.extend(f"- {a}" for a in artifacts[:3])
    lines.append("full story: TIMELINE.md | charter: CHARTER.md")
    text = "\n".join(lines) + "\n"
    while len(text.encode("utf-8")) > ITEM_MD_MAX_BYTES and len(lines) > 4:
        lines.pop(len(lines) - 2)  # drop detail lines, keep header + footer
        text = "\n".join(lines) + "\n"
    return text


def _write_item_md(project_root: Path, spec_name: str) -> str:
    root = Path(project_root).resolve()
    events = load_timeline(root, spec_name)
    directory = _item_dir(root, spec_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ITEM.md"
    path.write_text(render_item_md(spec_name, events), encoding="utf-8")
    return str(path)


def handle_timeline(project_root: str, spec_name: str, write_file: bool = True) -> Dict[str, Any]:
    """bc_timeline: return the item's lifecycle timeline; optionally (re)write TIMELINE.md."""
    root = Path(project_root).resolve()
    events = load_timeline(root, spec_name)
    result: Dict[str, Any] = {
        "spec_name": spec_name,
        "current_phase": current_phase(root, spec_name),
        "event_count": len(events),
        "events": events,
    }
    if write_file:
        result["timeline_path"] = _write_timeline_md(root, spec_name)
    return result
