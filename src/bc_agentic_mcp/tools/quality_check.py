"""bc_quality_check — run AL analyzers via the AL MCP Server. See spec Section 3.14."""
from pathlib import Path
from typing import Dict, Any

from bc_agentic_mcp.config import discover_al_tool
from bc_agentic_mcp.al_client import get_diagnostics


async def handle_quality_check(
    project_root: str,
    spec_name: str = "",
) -> Dict[str, Any]:
    """Run CodeCop/AppSourceCop/UICop diagnostics if the AL tool is available."""
    root = Path(project_root).resolve()
    al_tool = discover_al_tool()

    if not al_tool.available or al_tool.altool_path is None:
        return {
            "spec_name": spec_name,
            "mode": "spec-only",
            "available": False,
            "message": "AL MCP Server not available; quality check skipped.",
            "errors": 0,
            "warnings": 0,
            "diagnostics": [],
        }

    diagnostics = get_diagnostics(al_tool.altool_path, root)
    errors = [d for d in diagnostics if d.get("severity") == "error"]
    warnings = [d for d in diagnostics if d.get("severity") == "warning"]

    return {
        "spec_name": spec_name,
        "mode": "full",
        "available": True,
        "errors": len(errors),
        "warnings": len(warnings),
        "diagnostics": diagnostics,
    }
