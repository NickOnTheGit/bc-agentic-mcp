"""bc_status — show all specs and progress. See spec Section 3.10."""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, Optional


async def handle_status(
    project_root: str,
    spec_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Show current state of specs."""
    root = Path(project_root).resolve()
    state_path = specs_root(root) / "state.json"

    if not state_path.exists():
        return {
            "active_spec": None,
            "specs": {},
            "summary": {"total_specs": 0, "active": 0, "completed": 0, "blocked": 0},
        }

    state = json.loads(state_path.read_text())
    specs = state.get("specs", {})

    if spec_name:
        spec = specs.get(spec_name)
        if not spec:
            return {"error": f"Spec '{spec_name}' not found"}
        result: Dict[str, Any] = {"active_spec": state.get("active_spec"), "specs": {spec_name: spec}}
        # Mechanical engine enforcement status (same checks the commit gate applies).
        try:
            from bc_agentic_mcp import enforcement
            result["enforcement"] = enforcement.engine_status(root, spec_name)
            # Plain-language translation of the blockers, in fix order — a human must
            # understand WHY the item is blocked without knowing engine names.
            from bc_agentic_mcp import narrator
            blockers_plain = narrator.explain_blockers(result["enforcement"])
            if blockers_plain:
                result["human_blockers"] = blockers_plain
        except Exception:
            pass
        # Context-loss recovery surface: bc_status is the prescribed when-in-doubt
        # call, so it must hand a compacted agent everything needed to resume from
        # DISK: the timeline story (even pre-charter) + a map of what exists on disk.
        try:
            from bc_agentic_mcp import timeline as _timeline
            tl = _timeline.digest(root, spec_name, limit=10)
            if tl:
                result.setdefault("timeline", tl)
        except Exception:
            pass
        try:
            from bc_agentic_mcp import context_recovery
            result["on_disk"] = context_recovery.disk_map(root, spec_name)
        except Exception:
            pass
        return result

    completed = sum(1 for s in specs.values() if s.get("phase") == "closed")
    blocked = sum(1 for s in specs.values() if s.get("phase") == "blocked")
    active = len(specs) - completed

    return {
        "active_spec": state.get("active_spec"),
        "specs": specs,
        "summary": {
            "total_specs": state.get("total_specs", len(specs)),
            "active": active,
            "completed": completed,
            "blocked": blocked,
        },
    }
