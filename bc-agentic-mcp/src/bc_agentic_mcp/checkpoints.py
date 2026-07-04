"""Durable planning memory for a spec: an immutable **Charter** (core memory) plus an
append-only **checkpoint log** (episodic memory), persisted under ``.specs/<spec>/``.

Motivation: over long, multi-turn work an agent's recall of the original goal degrades
("context rot"). Industry practice (Anthropic context-engineering: structured
note-taking; MemGPT/Letta: always-in-context core memory) is to persist the high-signal
facts to disk and re-anchor on them. The Charter pins WHAT the item is for (purpose +
operations + acceptance criteria); the checkpoint log records key decisions over time.
Both survive context resets and are cheap to re-read via :func:`recall_digest`.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root

_lock = threading.Lock()


def _spec_dir(project_root: Path, spec_name: str) -> Path:
    return specs_root(project_root) / spec_name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_charter_md(charter: Dict[str, Any]) -> str:
    ops = charter.get("operations", {})
    ops_line = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in ops.items()) or "(unspecified)"
    lines = [
        f"# Charter: {charter.get('spec_name', '')}",
        "",
        "> Immutable statement of intent. Re-read this before making changes — do not let the",
        "> goal drift over a long session. Update only with an explicit scope change.",
        "",
        "## Purpose",
        charter.get("purpose", "").strip() or "(none captured)",
        "",
        f"## Operations in scope",
        ops_line,
        "",
        "## Acceptance criteria",
    ]
    criteria = charter.get("acceptance_criteria", [])
    lines.extend([f"- {c}" for c in criteria] or ["- (none captured)"])
    non_goals = charter.get("non_goals", [])
    if non_goals:
        lines += ["", "## Non-goals (out of scope)"]
        lines.extend(f"- {n}" for n in non_goals)
    lines += ["", f"_Created: {charter.get('created', '')}_", ""]
    return "\n".join(lines)


def write_charter(
    project_root: Path,
    spec_name: str,
    *,
    purpose: str,
    operations: Optional[Dict[str, bool]] = None,
    acceptance_criteria: Optional[List[str]] = None,
    non_goals: Optional[List[str]] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Write the immutable Charter (core memory) for a spec.

    Create-once by default: if a charter already exists it is returned unchanged, so the
    original intent cannot silently drift. Pass ``overwrite=True`` for a deliberate
    scope change.
    """
    directory = _spec_dir(project_root, spec_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "charter.json"
    if path.exists() and not overwrite:
        existing = load_charter(project_root, spec_name)
        if existing is not None:
            return existing
    charter = {
        "spec_name": spec_name,
        "purpose": (purpose or "").strip(),
        "operations": {str(k): bool(v) for k, v in (operations or {}).items()},
        "acceptance_criteria": [str(c) for c in (acceptance_criteria or []) if c],
        "non_goals": [str(n) for n in (non_goals or []) if n],
        "created": _now(),
    }
    with _lock:
        path.write_text(json.dumps(charter, indent=2), encoding="utf-8")
        (directory / "CHARTER.md").write_text(_render_charter_md(charter), encoding="utf-8")
    return charter


def load_charter(project_root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    path = _spec_dir(project_root, spec_name) / "charter.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def append_checkpoint(
    project_root: Path,
    spec_name: str,
    *,
    kind: str,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append a durable, timestamped checkpoint (decision / milestone) to the log."""
    directory = _spec_dir(project_root, spec_name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "checkpoints.jsonl"
    with _lock:
        existing = load_checkpoints(project_root, spec_name)
        entry = {
            "seq": len(existing) + 1,
            "ts": _now(),
            "kind": str(kind),
            "summary": str(summary),
            "details": details or {},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    return entry


def load_checkpoints(
    project_root: Path, spec_name: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    path = _spec_dir(project_root, spec_name) / "checkpoints.jsonl"
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def recall_digest(
    project_root: Path, spec_name: str, checkpoint_limit: int = 8
) -> Dict[str, Any]:
    """Re-anchoring digest: the charter (purpose/operations/criteria) + recent checkpoints.

    This is the primitive an agent reads to recover the item's intent after a long session
    instead of relying on (rot-prone) conversational recall.
    """
    charter = load_charter(project_root, spec_name)
    checkpoints = load_checkpoints(project_root, spec_name, limit=checkpoint_limit)
    return {
        "spec_name": spec_name,
        "found": charter is not None,
        "charter": charter,
        "recent_checkpoints": checkpoints,
    }

def plain_language_decisions(
    project_root: Path,
    spec_name: str,
    *,
    narrative: str = "",
) -> str:
    """One human story of every logical decision, reused by ALL lanes (feature review,
    PBI/bugfix review packet, refinement reports — human ask 2026-07-04).

    Deterministic sources, each rendered as plain sentences with the concrete example:
    - checkpoint log entries of kinds decision / scope_change / override / correction
      (recorded the moment the decision was made);
    - the item's own refinement corrections (ticket claims that LOST against code
      reality — e.g. "ticket says field id 14, the code says 15, the spec uses 15");
    - the bugfix root cause (symptom -> root cause -> fix), when recorded.
    The caller may prepend its own free-text narrative.
    """
    root = Path(project_root).resolve()
    sdir = specs_root(root) / spec_name
    parts: List[str] = []
    if narrative.strip():
        parts.append(narrative.strip())
        parts.append("")

    picked = [c for c in load_checkpoints(root, spec_name)
              if c.get("kind") in ("decision", "scope_change", "override", "correction")]
    if picked:
        parts.append("**Decisions recorded while working** (each was written down the "
                     "moment it was made):")
        for c in picked:
            ts = str(c.get("ts", ""))[:10]
            parts.append(f"- [{c.get('kind')}] {ts}: {str(c.get('summary', '')).strip()}")
        parts.append("")

    try:
        findings = json.loads((sdir / "item_refinement.json").read_text(encoding="utf-8")) \
            .get("findings", {})
    except (OSError, json.JSONDecodeError):
        findings = {}
    mismatches = findings.get("mismatches", []) or []
    if mismatches:
        parts.append("**Where the ticket was wrong and the code won** (we verify every "
                     "claim against real source before planning; when they disagree, the "
                     "spec follows the code):")
        parts.extend(f"- {m}" for m in mismatches)
        parts.append("")

    try:
        rc = json.loads((sdir / "root_cause.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rc = {}
    if rc.get("root_cause") or rc.get("symptom"):
        parts.append("**Why the fix looks like this** (bug diagnosis, evidence-checked):")
        if rc.get("symptom"):
            parts.append(f"- What users saw: {str(rc['symptom']).strip()}")
        if rc.get("root_cause"):
            parts.append(f"- The actual cause in the code: {str(rc['root_cause']).strip()}")
        if rc.get("fix"):
            parts.append(f"- Therefore the fix: {str(rc['fix']).strip()}")
        parts.append("")

    return "\n".join(parts).strip() or "(no recorded decisions yet)"
