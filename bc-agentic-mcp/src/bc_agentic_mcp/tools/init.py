"""bc_init tool — creates .specs/ structure. See spec Section 3.1."""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, Optional

from bc_agentic_mcp.state import StateManager
from bc_agentic_mcp.config import discover_app_json, discover_al_tool


DEFAULT_CONSTITUTION = """# Project Constitution

Immutable principles for this BC AL module. Edit to fit your project.

1. All new objects must stay within the module's declared `idRanges`.
2. Follow existing naming conventions discovered by bc_analyze_module.
3. No object outside the declared scope boundaries may be modified.
4. Every business rule must be testable.
5. Breaking changes require an upgrade codeunit.
"""


async def handle_init(
    project_root: str,
    module_name: Optional[str] = None,
    constitution: Optional[str] = None,
) -> Dict[str, Any]:
    """Initialize .specs/ directory structure for the BC project."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root)
    specs_dir.mkdir(parents=True, exist_ok=True)

    # State file
    sm = StateManager(specs_dir)
    sm.init()

    # Constitution — always present so downstream tools can rely on it
    const_path = specs_dir / "CONSTITUTION.md"
    if not const_path.exists():
        const_path.write_text(constitution or DEFAULT_CONSTITUTION, encoding="utf-8")

    # Config
    config_path = specs_dir / "config.md"
    if not config_path.exists():
        al_tool = discover_al_tool()
        app_json = discover_app_json(root)
        config_lines = [
            "# bc-agentic-mcp Configuration",
            f"## Module: {module_name or root.name}",
            f"## AL MCP Server: {'available' if al_tool.available else 'spec-only mode'}",
            f"## Project Root: {root}",
        ]
        if app_json:
            config_lines.append(f"## app.json: {app_json}")
        config_path.write_text("\n\n".join(config_lines), encoding="utf-8")

    app_info: Dict[str, Any] = {}
    app_json_path = discover_app_json(root)
    if app_json_path:
        try:
            app_info = json.loads(app_json_path.read_text())
        except (json.JSONDecodeError, OSError):
            app_info = {}

    return {
        "success": True,
        "created_paths": [
            str(specs_dir),
            str(specs_dir / "state.json"),
            str(const_path),
            str(config_path),
        ],
        "app_info": app_info,
        "state_file": str(specs_dir / "state.json"),
    }
