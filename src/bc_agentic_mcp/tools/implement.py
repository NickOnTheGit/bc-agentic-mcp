"""bc_implement — core implementation engine. See spec Section 3.9.

V1 scope: orchestration, scope enforcement, and task bookkeeping. The actual
AL code generation + compile/fix loop is delegated to the model + AL MCP Server
and is intentionally left as an explicit, auditable stub here.
"""
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from bc_agentic_mcp.scope import ScopeEnforcer


async def handle_implement(
    project_root: str,
    spec_name: str,
    task_ids: Optional[List[str]] = None,
    mode: str = "auto",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute implementation tasks from TASKS.md."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name

    spec = json.loads((specs_dir / "spec.json").read_text())
    # TASKS.md is read by the model during real implementation.

    scope = ScopeEnforcer(
        allowed_files=spec.get("scope_boundaries", {}).get("allowed_files", []),
        project_root=root,
    )

    results: List[Dict[str, Any]] = []

    for task_id in task_ids or []:
        result = await _execute_task(root, specs_dir, spec, scope, task_id, dry_run)
        results.append(result)
        if result["status"] == "failed" and mode == "auto":
            break  # Stop on failure in auto mode

    return {
        "spec_name": spec_name,
        "tasks_executed": len(results),
        "results": results,
    }


async def _execute_task(
    root: Path,
    specs_dir: Path,
    spec: Dict[str, Any],
    scope: ScopeEnforcer,
    task_id: str,
    dry_run: bool,
) -> Dict[str, Any]:
    """Execute a single implementation task.

    Real implementation steps (handled by model + AL MCP Server):
      1. Read task definition from TASKS.md
      2. Read referenced similar implementations
      3. Generate AL code with the implementer prompt
      4. Validate file path against scope (scope.check_write/check_create)
      5. Write AL code
      6. Compile via al_client.compile_extension
      7. Run diagnostics
      8. Fix compile errors (max 3 attempts)
      9. Update TASKS.md status
    """
    return {
        "task_id": task_id,
        "status": "dry_run_skipped" if dry_run else "not_implemented",
        "files_created": [],
        "files_modified": [],
        "diagnostics": {"errors": [], "warnings": []},
        "compile_result": {"success": False, "error_count": 0},
    }
