"""authorization — did a human approve implementation for this spec?

Reads the approval artifacts written by ``bc_request_approval`` / ``bc_submit_decision``
(``.specs/<spec>/approvals/<phase>.md`` with a ``**Status:** <decision>`` line).
Implementation is authorized once a gating phase (tasks / implement / complete) is approved.

Pure + deterministic: filesystem reads only, no side effects. Shared by the bc_implement
poka-yoke precondition, the deterministic detectors, and the mechanical commit gate.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from bc_agentic_mcp.workspace import external_base, specs_root

# Phases whose approval authorizes writing implementation code.
# `plan` and `code` are the canonical C1 gates; per-phase names are legacy aliases.
GATING_PHASES = ("plan", "code", "tasks", "implement", "complete")

_STATUS_RE = re.compile(r"(?im)^\*\*Status:\*\*\s*([A-Za-z_]+)")


def read_decision(project_root: Path, spec_name: str, phase: str) -> Optional[str]:
    """Return the recorded decision for a phase (``approve``/``reject``/…) or None."""
    path = specs_root(project_root) / spec_name / "approvals" / f"{phase}.md"
    if not path.exists():
        return None
    match = _STATUS_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).lower() if match else None


def implementation_authorized(project_root: Path, spec_name: str) -> bool:
    """True iff a gating phase (tasks/implement/complete) is approved for this spec."""
    return any(
        read_decision(project_root, spec_name, phase) == "approve"
        for phase in GATING_PHASES
    )


def review_is_fresh(project_root: Path, spec_name: str) -> Tuple[bool, str]:
    """True iff review artifacts are present and newer than planning inputs.

    Freshness contract:
    - REVIEW.md exists
    - quality_gate.json exists and passes
    - REVIEW.md timestamp is >= latest planning artifact timestamp
    - REVIEW.md does not contain obvious placeholders from stale packets
    """
    # Backward-compatible default: only enforce strict freshness when using
    # external specs storage (server mode with --specs-root / BC_AGENTIC_SPECS_ROOT).
    if external_base() is None:
        return True, "freshness gate disabled in colocated .specs mode"

    specs_dir = specs_root(project_root) / spec_name
    review_path = specs_dir / "REVIEW.md"
    quality_gate_path = specs_dir / "quality_gate.json"
    spec_path = specs_dir / "spec.json"
    design_path = specs_dir / "DESIGN.md"
    tasks_path = specs_dir / "TASKS.md"
    tdd_path = specs_dir / "TDD.md"

    if not review_path.exists():
        return False, "Missing review artifact: REVIEW.md"
    if not quality_gate_path.exists():
        return False, "Missing quality gate artifact: quality_gate.json"

    try:
        quality_gate = json.loads(quality_gate_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Unreadable quality gate artifact: {exc}"

    if not bool(quality_gate.get("pass", False)):
        return False, "quality_gate.json indicates review is not ready (pass=false)"

    review_text = review_path.read_text(encoding="utf-8", errors="replace")
    if (
        "NOT FOUND in repo" in review_text
        or "UNRESOLVED: confirm target path" in review_text
        or "(missing " in review_text
    ):
        return False, "REVIEW.md still contains stale placeholders (missing/not found markers)"

    review_mtime = review_path.stat().st_mtime
    # TASKS.md is deliberately NOT a freshness dependency: bc_implement_write ticks
    # task checkboxes in it after every file write, so including it makes the FIRST
    # write invalidate the very review that authorized it (observed live: T-001 ok,
    # T-002/T-003 blocked_needs_fresh_review). Plan-time task changes always come
    # from bc_breakdown_tasks, which regenerates REVIEW.md in the same run.
    dependency_paths = [spec_path, design_path, tdd_path]
    existing_dependencies = [p for p in dependency_paths if p.exists()]
    if not existing_dependencies:
        return False, "No planning artifacts found (spec/design/tasks/TDD)"

    newest_dependency_mtime = max(p.stat().st_mtime for p in existing_dependencies)
    if review_mtime < newest_dependency_mtime:
        return (
            False,
            "REVIEW.md is older than planning artifacts; regenerate via bc_prepare_review",
        )

    return True, "fresh"


def authorized_specs(project_root: Path) -> List[str]:
    """List spec names under .specs/ that currently authorize implementation."""
    specs_dir = specs_root(project_root)
    if not specs_dir.is_dir():
        return []
    out: List[str] = []
    for child in specs_dir.iterdir():
        if child.is_dir() and not child.name.startswith(".") and implementation_authorized(project_root, child.name):
            out.append(child.name)
    return out
