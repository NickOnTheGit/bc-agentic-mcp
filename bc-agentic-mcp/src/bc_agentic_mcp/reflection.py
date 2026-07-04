"""reflection — make the MCP prompt itself to reflect, without a human asking.

Closed loop: the agent checkpoints a mistake/correction/scope-change/override; the server
then injects a ``reflection_due`` nudge into every spec-scoped response (via re-anchor) until
the agent calls ``bc_reflect`` to record the lesson(s). Reflection thus becomes an automatic
step of the workflow, not something the user must request each time.

Pure + deterministic: signals are derived from the durable checkpoint log.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import lessons as lessons_store

# Checkpoint kinds that warrant a lesson (things that went differently than planned).
REFLECTABLE_KINDS = {"mistake", "correction", "scope_change", "override", "blocked", "rework"}
REFLECTION_KIND = "reflection"


def pending_reflections(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Reflectable checkpoints recorded SINCE the last reflection (deterministic)."""
    try:
        cps = memory.load_checkpoints(Path(project_root).resolve(), spec_name)
    except Exception:
        return {"count": 0, "signals": []}
    last_reflection_idx = -1
    for i, c in enumerate(cps):
        if c.get("kind") == REFLECTION_KIND:
            last_reflection_idx = i
    signals = [
        {"kind": c.get("kind"), "summary": c.get("summary")}
        for c in cps[last_reflection_idx + 1:]
        if c.get("kind") in REFLECTABLE_KINDS
    ]
    return {"count": len(signals), "signals": signals}


def record_reflection(
    project_root: Path,
    spec_name: str,
    *,
    note: str,
    lessons: Optional[List[Dict[str, Any]]] = None,
    promote: bool = False,
) -> Dict[str, Any]:
    """Record lessons + a reflection checkpoint (which clears the pending nudge).

    Each lesson = {message, match?, severity?}. ``promote`` also writes them to the
    cross-project store so they apply to every repo.
    """
    root = Path(project_root).resolve()
    recorded: List[Dict[str, Any]] = []
    for lesson in lessons or []:
        msg = lesson.get("message", "")
        if not msg.strip():
            continue
        match = lesson.get("match") or {}
        severity = lesson.get("severity", "warning")
        recorded.append(lessons_store.record_human_lesson(root, message=msg, match=match, severity=severity))
        if promote:
            lessons_store.record_global_lesson(message=msg, match=match, severity=severity)
    before = pending_reflections(root, spec_name)
    memory.append_checkpoint(
        root, spec_name, kind=REFLECTION_KIND, summary=note or "reflection",
        details={"lessons_recorded": len(recorded), "promoted": bool(promote),
                 "addressed_signals": before["count"]},
    )
    return {
        "reflected": True,
        "lessons_recorded": len(recorded),
        "promoted": bool(promote),
        "addressed_signals": before["count"],
    }
