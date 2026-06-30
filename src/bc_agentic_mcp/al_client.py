"""AL MCP Server client — delegates to altool for compile, diagnostics, symbols."""
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any


@dataclass
class CompileResult:
    success: bool
    error_count: int
    warning_count: int
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)


def compile_extension(
    altool_path: Path,
    project_root: Path,
    enable_code_analysis: bool = True,
) -> CompileResult:
    """Compile the AL extension using altool. Returns structured result."""
    cmd = [
        str(altool_path),
        "compile",
        "--project",
        str(project_root),
        "--output",
        str(project_root / ".alc"),
    ]
    if enable_code_analysis:
        cmd.extend(["--enableCodeAnalysis", "true"])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    diagnostics: List[Dict[str, Any]] = []
    if result.stdout:
        try:
            diagnostics = json.loads(result.stdout)
        except json.JSONDecodeError:
            diagnostics = []

    errors = [d for d in diagnostics if d.get("severity") == "error"]
    warnings = [d for d in diagnostics if d.get("severity") == "warning"]

    return CompileResult(
        success=result.returncode == 0,
        error_count=len(errors),
        warning_count=len(warnings),
        diagnostics=diagnostics,
    )


def get_diagnostics(altool_path: Path, project_root: Path) -> List[Dict[str, Any]]:
    """Get diagnostics for the project using altool."""
    cmd = [str(altool_path), "diagnostics", "--project", str(project_root)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
