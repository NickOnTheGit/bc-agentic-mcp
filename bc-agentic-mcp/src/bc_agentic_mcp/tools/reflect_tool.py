"""bc_reflect — record lessons learned and clear the automatic reflection nudge."""
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import reflection


async def handle_reflect(
    project_root: str,
    spec_name: str,
    note: str = "",
    lessons: Optional[List[Dict[str, Any]]] = None,
    promote: bool = False,
) -> Dict[str, Any]:
    """Record lessons (project store; also global when ``promote``) and a reflection
    checkpoint, which clears the ``reflection_due`` nudge for the spec.
    """
    root = Path(project_root).resolve()
    result = reflection.record_reflection(
        root, spec_name, note=note, lessons=lessons, promote=promote
    )
    result["still_pending"] = reflection.pending_reflections(root, spec_name)
    return result
