"""bc_archive — close out a spec. See spec Section 3.16."""
from pathlib import Path
from typing import Dict, Any

from bc_agentic_mcp.state import StateManager


async def handle_archive(
    project_root: str,
    spec_name: str,
    outcome: str = "merged",
) -> Dict[str, Any]:
    """Mark a spec as closed with an outcome."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs"

    sm = StateManager(specs_dir)
    sm.archive_spec(spec_name, outcome)

    return {
        "spec_name": spec_name,
        "status": "closed",
        "outcome": outcome,
    }
