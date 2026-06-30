"""bc_status — show all specs and progress. See spec Section 3.10."""
import json
from pathlib import Path
from typing import Dict, Any, Optional


async def handle_status(
    project_root: str,
    spec_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Show current state of specs."""
    root = Path(project_root).resolve()
    state_path = root / ".specs" / "state.json"

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
        return {"active_spec": state.get("active_spec"), "specs": {spec_name: spec}}

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
