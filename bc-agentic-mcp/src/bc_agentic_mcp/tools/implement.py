"""bc_implement — core implementation engine. See spec Section 3.9.

V2 scope: two-phase tool.

Phase 1 (code=None): context preparation — unchanged from V1.
  Returns task context + implementer prompt for the model.

Phase 2 (code is provided): code execution.
  Validates scope, writes AL file, compiles via altool, returns diagnostics.
  Supports compile-and-fix loop (max 3 attempts).
"""
from pathlib import Path
from typing import Dict, Any, List, Optional

from bc_agentic_mcp.errors import MCPError, ErrorCode
from bc_agentic_mcp.scope import ScopeEnforcer
from bc_agentic_mcp.spec_loader import load_spec


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def _load_implementer_prompt() -> str:
    """Load the implementer.md prompt file."""
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "implementer.md"
    if not prompt_path.exists():
        return ""  # Graceful degradation — model can still implement without custom prompt
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File writer with scope enforcement
# ---------------------------------------------------------------------------

def _write_al_file(
    root: Path,
    scope: ScopeEnforcer,
    file_path: str,
    content: str,
) -> Path:
    """Write an AL file. Validates scope before writing."""
    from bc_agentic_mcp.validation import sanitize_path

    sanitize_path(file_path)
    target = root / file_path

    # Check if file already exists (modify) or is new (create)
    if target.exists():
        if not scope.check_write(file_path):
            reason = scope.block_reason(file_path)
            raise MCPError(ErrorCode.SCOPE_ERROR, reason,
                           hint="Expand scope boundaries in spec.json or choose an alternative approach.")
    else:
        if not scope.check_create(file_path):
            reason = scope.block_reason(file_path)
            raise MCPError(ErrorCode.SCOPE_ERROR, reason,
                           hint="Expand scope boundaries or choose a file in an allowed extension.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Implementation instructions builder
# ---------------------------------------------------------------------------

def _build_implementation_instructions(
    tasks_path: Path, task_id: str, spec: Dict[str, Any]
) -> str:
    """Build the instruction block for the AI model."""
    scope = spec.get("scope_boundaries", {})
    allowed = scope.get("allowed_files", [])
    forbidden = scope.get("forbidden_patterns", [])

    lines = [
        f"Read TASKS.md at: {tasks_path}",
        f"Find task {task_id} and implement it.",
        f"Allowed files (do NOT write outside these): {allowed}",
        f"Forbidden patterns (do NOT use these): {forbidden}",
        "",
        "After writing AL code:",
        "1. Compile the project",
        "2. Read diagnostics",
        "3. Fix any errors (max 3 attempts)",
        "4. If 3rd attempt fails, mark task as failed and report diagnostics",
        "",
        "Copy naming conventions and error handling patterns exactly from ",
        "the module analysis. Follow the implementer prompt rules.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TASKS.md updater
# ---------------------------------------------------------------------------


def _update_tasks_md(specs_dir: Path, task_ids: Optional[List[str]], failed: bool = False) -> None:
    """Update TASKS.md: mark tasks as complete or failed."""
    tasks_path = specs_dir / "TASKS.md"
    if not tasks_path.exists():
        return
    content = tasks_path.read_text(encoding="utf-8")
    for task_id in task_ids or []:
        old = f"- [ ] {task_id}"
        new = f"- [x] {task_id}" if not failed else f"- [x] {task_id} (FAILED)"
        content = content.replace(old, new)
    tasks_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Altool path helper (lazy import to avoid circular dependency)
# ---------------------------------------------------------------------------


def _get_altool_path() -> Optional[Path]:
    """Get altool path from server context. Returns None if not available."""
    try:
        from bc_agentic_mcp.server import _get_ctx
        return _get_ctx().config.al_tool.altool_path
    except (AssertionError, ImportError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def handle_implement(
    project_root: str,
    spec_name: str,
    task_ids: Optional[List[str]] = None,
    mode: str = "auto",
    dry_run: bool = False,
    # Phase 2 parameters
    code: Optional[str] = None,
    file_path: Optional[str] = None,
    attempt: int = 1,
    previous_diagnostics: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute implementation tasks. Two-phase behavior.

    Phase 1 (code=None): prepare context for model (unchanged from V1).
    Phase 2 (code is provided): write file, compile, return diagnostics.
    """
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name

    spec = load_spec(specs_dir)

    scope_boundaries = spec.get("scope_boundaries", {})
    scope = ScopeEnforcer(
        allowed_files=scope_boundaries.get("allowed_files", []),
        project_root=root,
        allowed_extensions=scope_boundaries.get("allowed_extensions", []),
    )

    # ------------------------------------------------------------------
    # Phase 2: code execution
    # ------------------------------------------------------------------
    if code is not None:
        # Validate file_path
        if not file_path:
            return {
                "status": "error",
                "message": "file_path is required when code is provided",
            }

        # Write file with scope validation
        try:
            _write_al_file(root, scope, file_path, code)
        except MCPError as e:
            return {
                "status": "scope_violation",
                "message": str(e),
                "hint": e.hint,
            }

        # Attempt compile
        altool_path = _get_altool_path()
        if altool_path is None:
            return {
                "status": "written_no_compile",
                "file": file_path,
                "message": "File written but cannot compile — AL MCP Server not available",
            }

        from bc_agentic_mcp.al_client import compile_extension
        compile_result = compile_extension(altool_path, root)

        if compile_result.success:
            _update_tasks_md(specs_dir, task_ids)
            return {
                "status": "completed",
                "file": file_path,
                "compile_result": {
                    "success": True,
                    "error_count": compile_result.error_count,
                    "warning_count": compile_result.warning_count,
                },
            }
        elif attempt < 3:
            return {
                "status": "compile_failed",
                "file": file_path,
                "diagnostics": [d for d in compile_result.diagnostics if d.get("severity") == "error"],
                "attempt": attempt,
                "retry": True,
                "guidance": "Fix the errors below and call bc_implement again with attempt+1",
            }
        else:
            _update_tasks_md(specs_dir, task_ids, failed=True)
            return {
                "status": "failed_after_retries",
                "file": file_path,
                "diagnostics": [d for d in compile_result.diagnostics if d.get("severity") == "error"],
                "human_action_required": "Review errors manually",
            }

    # ------------------------------------------------------------------
    # Phase 1: context preparation (unchanged from V1)
    # ------------------------------------------------------------------
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
    """Execute a single implementation task with compile-and-fix loop.

    Flow:
      1. Parse task from TASKS.md
      2. Load module analysis + implementer prompt
      3. Generate AL code (delegated to AI model via hosting agent)
      4. Validate file path against scope
      5. Write file
      6. Compile via al_client.compile_extension
      7. If errors: feed diagnostics back, regenerate (max 3 attempts)
      8. On success: update TASKS.md status
      9. On failure after 3 attempts: mark task failed, report diagnostics
    """
    if dry_run:
        return {
            "task_id": task_id, "status": "dry_run_skipped",
            "files_created": [], "files_modified": [],
            "diagnostics": {"errors": [], "warnings": []},
            "compile_result": {"success": False, "error_count": 0},
            "message": "Dry run — no files written.",
        }

    # 1. Parse task from TASKS.md
    tasks_path = specs_dir / "TASKS.md"
    if not tasks_path.exists():
        raise MCPError(ErrorCode.CLIENT_ERROR, "TASKS.md not found",
                       hint="Run bc_breakdown_tasks first.")

    # 2. Load context files
    analysis_path = specs_dir / "analysis.md"
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "implementer.md"

    context = {
        "task_id": task_id,
        "tasks_file": str(tasks_path),
        "analysis_file": str(analysis_path) if analysis_path.exists() else None,
        "prompt_file": str(prompt_path) if prompt_path.exists() else None,
        "spec_file": str(specs_dir / "spec.json"),
        "scope_boundaries": spec.get("scope_boundaries", {}),
        "instructions": _build_implementation_instructions(tasks_path, task_id, spec),
    }

    return {
        "task_id": task_id,
        "status": "ready_for_model",
        "context": context,
        "files_created": [],
        "files_modified": [],
        "diagnostics": {"errors": [], "warnings": []},
        "compile_result": {"success": False, "error_count": 0},
        "message": "Task context prepared. Generate AL code following the implementer prompt.",
    }
