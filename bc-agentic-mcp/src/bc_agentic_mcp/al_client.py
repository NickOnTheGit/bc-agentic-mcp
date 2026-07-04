"""AL MCP Server client — delegates to altool for compile, diagnostics, symbols."""
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any

from bc_agentic_mcp.errors import MCPError, ErrorCode


@dataclass
class CompileResult:
    success: bool
    error_count: int
    warning_count: int
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    stderr: str = ""


def _run_altool(cmd: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run altool with consistent error handling."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise MCPError(
            ErrorCode.EXTERNAL_ERROR,
            f"AL MCP Server (altool) not found. Command: {cmd[0]}",
            hint="Install the AL Language extension or set ALTOOL_PATH environment variable.",
            retry_after=0,
        )
    except subprocess.TimeoutExpired:
        raise MCPError(
            ErrorCode.EXTERNAL_ERROR,
            f"AL compilation timed out after {timeout}s",
            hint="The project may be too large. Increase timeout or split into smaller extensions.",
            retry_after=30,
        )
    except OSError as e:
        raise MCPError(
            ErrorCode.EXTERNAL_ERROR,
            f"Failed to run altool: {e}",
            hint="Check file permissions and disk space.",
            retry_after=10,
        )


def _parse_diagnostics(result: subprocess.CompletedProcess) -> List[Dict[str, Any]]:
    """Parse altool output. Returns empty list on parse failure, but raises
    MCPError if altool exited non-zero and produced unparseable output
    (avoids false green: '0 errors, 0 warnings' when altool crashed)."""
    if not result.stdout:
        return []

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        if result.returncode != 0:
            # Non-JSON output + non-zero exit = something went wrong
            raise MCPError(
                ErrorCode.EXTERNAL_ERROR,
                f"altool failed (exit {result.returncode})",
                hint=f"Check altool output manually. Stderr: {result.stderr[:200]}",
                details={"stderr": result.stderr[:500], "stdout": result.stdout[:500]},
            )
        # Non-JSON output + zero exit = maybe altool writes plain text on success
        return []


def compile_extension(
    altool_path: Path,
    project_root: Path,
    enable_code_analysis: bool = True,
) -> CompileResult:
    """Compile the AL extension using altool. Returns structured result."""
    cmd = [
        str(altool_path), "compile",
        "--project", str(project_root),
        "--output", str(project_root / ".alc"),
    ]
    if enable_code_analysis:
        cmd.extend(["--enableCodeAnalysis", "true"])

    result = _run_altool(cmd)
    diagnostics = _parse_diagnostics(result)

    errors = [d for d in diagnostics if d.get("severity") == "error"]
    warnings = [d for d in diagnostics if d.get("severity") == "warning"]

    return CompileResult(
        success=result.returncode == 0,
        error_count=len(errors),
        warning_count=len(warnings),
        diagnostics=diagnostics,
        stderr=result.stderr,
    )


def get_diagnostics(altool_path: Path, project_root: Path) -> List[Dict[str, Any]]:
    """Get diagnostics for the project using altool."""
    cmd = [str(altool_path), "diagnostics", "--project", str(project_root)]
    result = _run_altool(cmd, timeout=60)
    return _parse_diagnostics(result)
