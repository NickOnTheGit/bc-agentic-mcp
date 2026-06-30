"""bc_quality_check — run AL analyzers via the AL MCP Server. See spec Section 3.14."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.config import discover_al_tool
from bc_agentic_mcp.al_client import get_diagnostics


# ---------------------------------------------------------------------------
# Baseline management (GAP 4 — Regression Comparison)
# ---------------------------------------------------------------------------

def _key(d: Dict[str, Any]) -> str:
    """Stable diagnostic key immune to minor wording changes.

    Uses: code + message_prefix (before first ':') + file + line.
    This is more robust than matching on full message text, which can
    change between altool versions.
    """
    parts = [
        d.get("code", ""),
        d.get("message", "").split(":")[0] if d.get("message") else "",
        d.get("sourceLocation", {}).get("file", ""),
        str(d.get("sourceLocation", {}).get("line", "")),
    ]
    return "|".join(parts)


def save_baseline(baseline_dir: Path, diagnostics: List[Dict[str, Any]]) -> Path:
    """Save a baseline snapshot of current diagnostics.

    Args:
        baseline_dir: Directory to store baselines (e.g. .specs/.baselines/).
        diagnostics: List of diagnostic dicts from altool.

    Returns:
        Path to the saved baseline file.
    """
    baseline_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    basename = f"baseline_{timestamp}.json"
    path = baseline_dir / basename

    data = {
        "timestamp": timestamp,
        "diagnostics": diagnostics,
    }
    path.write_text(json.dumps(data, indent=2))
    _cleanup_old_baselines(baseline_dir)
    return path


def load_baseline(baseline_dir: Path) -> Optional[Dict[str, Any]]:
    """Load the most recent baseline from the baseline directory.

    Args:
        baseline_dir: Directory containing baseline files.

    Returns:
        The most recent baseline dict with 'timestamp' and 'diagnostics' keys,
        or None if no baselines exist.
    """
    files = sorted(
        baseline_dir.glob("baseline_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None
    return json.loads(files[0].read_text())


def compare_diagnostics(
    baseline: List[Dict[str, Any]],
    current: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare current diagnostics against a baseline.

    Uses the robust _key() matching (code + message_prefix + file + line)
    to avoid false regressions from cosmetic message changes.

    Args:
        baseline: List of diagnostic dicts from the saved baseline.
        current: List of diagnostic dicts from the current run.

    Returns:
        Dict with:
          - 'new_errors': List of diagnostics present in current but not baseline.
          - 'fixed_errors': List of diagnostics in baseline but not current.
          - 'unchanged': Count of diagnostics present in both.
          - 'regression': True if new_errors is non-empty.
    """
    baseline_keys = set(_key(d) for d in baseline)
    current_keys = set(_key(d) for d in current)

    # New: in current but not in baseline
    new_keys = current_keys - baseline_keys
    new_errors = [
        d for d in current if _key(d) in new_keys
    ]

    # Fixed: in baseline but not in current
    fixed_keys = baseline_keys - current_keys
    fixed_errors = [
        d for d in baseline if _key(d) in fixed_keys
    ]

    # Unchanged: intersection
    unchanged = len(current) - len(new_errors)

    return {
        "new_errors": new_errors,
        "fixed_errors": fixed_errors,
        "unchanged": max(0, unchanged),
        "regression": len(new_errors) > 0,
    }


def _cleanup_old_baselines(baseline_dir: Path, keep: int = 5) -> None:
    """Remove old baselines, keeping only the most recent N.

    Args:
        baseline_dir: Directory containing baseline files.
        keep: Number of most recent baselines to retain.
    """
    files = sorted(
        baseline_dir.glob("baseline_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle_quality_check(
    project_root: str,
    spec_name: str = "",
) -> Dict[str, Any]:
    """Run CodeCop/AppSourceCop/UICop diagnostics if the AL tool is available.

    Baseline behavior (GAP 4):
      - If no baseline exists: saves current diagnostics as baseline.
      - If a baseline exists: compares current vs baseline, reports regression.
    """
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

    result: Dict[str, Any] = {
        "spec_name": spec_name,
        "mode": "full",
        "available": True,
        "errors": len(errors),
        "warnings": len(warnings),
        "diagnostics": diagnostics,
    }

    # --- Baseline comparison (GAP 4) ---
    baseline_dir = root / ".specs" / ".baselines"
    existing_baseline = load_baseline(baseline_dir)

    if existing_baseline is None:
        # No baseline exists — save current as baseline
        saved_path = save_baseline(baseline_dir, diagnostics)
        result["baseline"] = {
            "action": "saved",
            "path": str(saved_path),
            "message": "Initial baseline saved. Next run will compare against this.",
            "diagnostic_count": len(diagnostics),
        }
    else:
        # Compare current vs baseline
        comparison = compare_diagnostics(
            existing_baseline.get("diagnostics", []), diagnostics
        )
        result["baseline"] = {
            "action": "compared",
            "baseline_timestamp": existing_baseline.get("timestamp"),
        }
        result["baseline"].update(comparison)

        # Auto-refresh baseline
        save_baseline(baseline_dir, diagnostics)

    return result
