"""bc_quality_check — run AL analyzers via the AL MCP Server. See spec Section 3.14."""
import hashlib
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.config import discover_al_tool
from bc_agentic_mcp.al_client import get_diagnostics
from bc_agentic_mcp.guidelines_policy import scan as scan_guidelines


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
        key=lambda p: (p.stat().st_mtime, p.name),  # name tiebreak for mtime collisions
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
        key=lambda p: (p.stat().st_mtime, p.name),  # name tiebreak for mtime collisions
        reverse=True,
    )
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def _normalize_rel_path(path_text: str) -> str:
    return str(path_text).replace("\\", "/").lstrip("./")


def _load_allowed_files(project_root: Path, spec_name: str) -> set[str]:
    if not spec_name:
        return set()
    spec_file = specs_root(project_root) / spec_name / "spec.json"
    if not spec_file.exists():
        return set()
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return set()
    allowed = spec.get("scope_boundaries", {}).get("allowed_files", [])
    out = set()
    for entry in allowed or []:
        if isinstance(entry, str) and entry.strip():
            out.add(_normalize_rel_path(entry))
    return out


_UMBRELLA_REQ_RE = re.compile(r"shall implement the change described", re.IGNORECASE)


def _traceability_grain_findings(project_root: Path, spec_name: str) -> List[Dict[str, Any]]:
    """GL-COV002 — the spec collapsed to the single umbrella fallback requirement.

    Observed live on wi240435: verification reported '100% coverage' against ONE
    generic criterion, so the behavior→test mapping existed only in test design,
    not in machine-tracked traceability. Warn (never block) so the author re-runs
    bc_write_spec with itemized behavioral bullets before relying on bc_verify.
    """
    if not spec_name:
        return []
    spec_file = specs_root(project_root) / spec_name / "spec.json"
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    reqs = spec.get("requirements") or []
    if len(reqs) != 1:
        return []
    statement = str((reqs[0] or {}).get("statement", ""))
    if not _UMBRELLA_REQ_RE.search(statement):
        return []
    return [{
        "code": "GL-COV002",
        "message": (
            "Traceability grain: the spec has exactly ONE umbrella requirement "
            "('...shall implement the change described...'). bc_verify can only prove "
            "coverage at this grain — 100% here does NOT mean every behavior is traced "
            "to a named test. Re-run bc_write_spec with itemized behavioral bullets "
            "(one requirement per demanded behavior) before trusting the coverage number."
        ),
        "severity": "warning",
        "sourceLocation": {"file": str(spec_file), "line": 0},
    }]


def _is_relevant_path(rel_path: str, allowed_files: set[str]) -> bool:
    if not allowed_files:
        return True
    rel = _normalize_rel_path(rel_path)
    if rel in allowed_files:
        return True
    return any(rel.endswith(a) or a.endswith(rel) for a in allowed_files)


def _filter_diagnostics_to_scope(
    diagnostics: List[Dict[str, Any]],
    allowed_files: set[str],
) -> List[Dict[str, Any]]:
    if not allowed_files:
        return diagnostics
    scoped: List[Dict[str, Any]] = []
    for diag in diagnostics:
        src = diag.get("sourceLocation") or {}
        file_path = str(src.get("file") or "")
        if not file_path:
            continue
        # GOVERNANCE findings point at spec-folder artifacts (data_model_approval.json,
        # coverage contracts) — never .al scope files. Filtering them out silently
        # swallowed the machine's own approval demand (GL-DM001 observed live).
        if str(diag.get("code", "")).startswith(("GL-DM", "GL-COV", "GL-RM")):
            scoped.append(diag)
            continue
        if _is_relevant_path(file_path, allowed_files):
            scoped.append(diag)
    return scoped


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def handle_quality_check(
    project_root: str,
    spec_name: str = "",
    run_compiler: bool = False,
) -> Dict[str, Any]:
    """Run CodeCop/AppSourceCop/UICop diagnostics if the AL tool is available.

    The body is fully synchronous (file scans, subprocesses) — it runs in a worker
    thread so a slow scan can never freeze the MCP event loop (which would also
    disable the server-side timeout that protects the caller).
    """
    import asyncio as _asyncio
    return await _asyncio.to_thread(
        _quality_check_sync, project_root, spec_name, run_compiler
    )


def _quality_check_sync(
    project_root: str,
    spec_name: str = "",
    run_compiler: bool = False,
) -> Dict[str, Any]:
    """Run CodeCop/AppSourceCop/UICop diagnostics if the AL tool is available.

    When ``run_compiler`` is True, the REAL alc compiler is invoked for authoritative
    semantic + analyzer diagnostics; otherwise the deterministic regex validator runs.

    Baseline behavior (GAP 4):
      - If no baseline exists: saves current diagnostics as baseline.
      - If a baseline exists: compares current vs baseline, reports regression.
    """
    root = Path(project_root).resolve()
    al_tool = discover_al_tool()
    allowed_files = _load_allowed_files(root, spec_name)
    allowed_al_files = sorted(p for p in allowed_files if p.lower().endswith(".al"))

    # Self-contained validation always runs — no external tool required.
    from bc_agentic_mcp.al_validator import validate_project

    diagnostics = validate_project(root, include_files=allowed_al_files or None)

    # Merge the authoritative compiler-integrated analyzers (CodeCop/AppSourceCop/UICop/
    # PerTenantExtensionCop/LinterCop) on top of the regex diagnostics, with provenance +
    # de-duplication. Falls back to regex-only when the AL toolchain is unavailable.
    from bc_agentic_mcp import analyzers as _an
    collected = _an.collect(root, al_tool, self_diags=diagnostics, use_compiler=run_compiler)
    diagnostics = collected["diagnostics"]
    mode = collected["mode"]
    if collected.get("analyzer_error"):
        diagnostics.append({
            "code": "AL-EXTERNAL",
            "message": f"Compiler analyzers failed; used self-contained validator only: {collected['analyzer_error']}",
            "severity": "info",
            "sourceLocation": {"file": "", "line": 0},
        })

    # Enforce local coding-guidelines policy (overrideable via .specs/policy/coding_guidelines.json)
    diagnostics.extend(scan_guidelines(root, spec_name=spec_name))
    diagnostics.extend(_traceability_grain_findings(root, spec_name))
    diagnostics = _filter_diagnostics_to_scope(diagnostics, allowed_files)

    errors = [d for d in diagnostics if d.get("severity") == "error"]
    warnings = [d for d in diagnostics if d.get("severity") == "warning"]

    result: Dict[str, Any] = {
        "spec_name": spec_name,
        "mode": mode,
        "available": mode != "self",
        # A bare false confused a weak model (GPT-5-mini run, 2026-07-06): say WHY
        # and what it means for trust in the diagnostics.
        **({} if mode != "self" else {
            "available_reason": (
                "AL compiler analyzers (CodeCop/UICop…) are not installed in this "
                "workspace — diagnostics below come from the self-contained validator "
                "only, which covers fewer rules. Install the AL extension or run in a "
                "prepared worktree for full analyzer coverage."
            ),
        }),
        "analyzers": collected.get("analyzers", []),
        "sources": collected.get("sources", {}),
        "errors": len(errors),
        "warnings": len(warnings),
        "diagnostics": diagnostics,
    }

    # Persist a quality snapshot so the commit gate (enforcement.py) can mechanically verify a
    # green analyzer/validator run happened for the CURRENT spec (spec_sha detects staleness).
    try:
        item_dir = specs_root(root) / spec_name
        item_dir.mkdir(parents=True, exist_ok=True)
        spec_file = item_dir / "spec.json"
        spec_sha = hashlib.sha256(spec_file.read_bytes()).hexdigest() if spec_file.exists() else None
        (item_dir / "quality.json").write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "errors": len(errors),
            "warnings": len(warnings),
            "sources": collected.get("sources", {}),
            "analyzers": collected.get("analyzers", []),
            "guidelines_policy": ".specs/policy/coding_guidelines.json",
            "spec_sha": spec_sha,
        }, indent=2), encoding="utf-8")
        result["quality_path"] = str(item_dir / "quality.json")
    except Exception:
        pass

    # --- Baseline comparison (GAP 4) ---
    baseline_dir = specs_root(root) / ".baselines"
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

        # Code review guideline: do not introduce new warnings.
        new_warnings = [d for d in comparison.get("new_errors", []) if d.get("severity") == "warning"]
        if new_warnings:
            diagnostics.append({
                "code": "GL-CR001",
                "message": f"Code review guideline: {len(new_warnings)} new warning(s) introduced since baseline.",
                "severity": "error",
                "sourceLocation": {"file": "", "line": 0},
            })
            errors = [d for d in diagnostics if d.get("severity") == "error"]
            warnings = [d for d in diagnostics if d.get("severity") == "warning"]
            result["errors"] = len(errors)
            result["warnings"] = len(warnings)
            result["diagnostics"] = diagnostics

        result["baseline"] = {
            "action": "compared",
            "baseline_timestamp": existing_baseline.get("timestamp"),
        }
        result["baseline"].update(comparison)

        # Auto-refresh baseline
        save_baseline(baseline_dir, diagnostics)

    # Layer 1: deterministic mistake detection auto-records checkpoints (triggers reflection).
    # No-op for ad-hoc specs (only runs when a Charter exists for spec_name).
    try:
        from bc_agentic_mcp import detectors
        auto = detectors.scan_and_record(root, spec_name, diagnostics=diagnostics)
        result["auto_reflection"] = {
            "findings": len(auto.get("findings", [])),
            "checkpoints_recorded": auto.get("recorded", 0),
        }
    except Exception:
        pass

    return result
