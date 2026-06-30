"""bc_converge — compare implementation against the spec. See spec Section 3.13."""
import json
from pathlib import Path
from typing import Dict, Any

from bc_agentic_mcp.tools.analyze import scan_al_files


async def handle_converge(
    project_root: str,
    spec_name: str,
) -> Dict[str, Any]:
    """Compare what was declared in spec.json against what exists on disk."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name
    spec = json.loads((specs_dir / "spec.json").read_text())

    declared = spec.get("objects_to_create", [])
    declared_names = {(o["type"], o["name"]) for o in declared}

    existing = scan_al_files(root)
    existing_names = {(o["type"], o["name"]) for o in existing}

    missing = sorted(
        [f"{t} {n}" for (t, n) in declared_names - existing_names]
    )
    extra = sorted(
        [f"{t} {n}" for (t, n) in existing_names - declared_names]
    )
    converged = len(missing) == 0

    return {
        "spec_name": spec_name,
        "converged": converged,
        "declared_count": len(declared_names),
        "implemented_count": len(declared_names & existing_names),
        "missing": missing,
        "unexpected": extra,
    }
