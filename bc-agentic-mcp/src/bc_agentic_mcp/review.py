"""review — a SEPARATE reviewer's checklist + finding recorder (Layer 3).

Anthropic guardrail pattern: a *different* instance screens the work — an actor cannot
reliably grade its own output (same blind spots). This module gives a reviewer sub-agent a
deterministic packet (Charter + changed files + a BC first-principles checklist) to evaluate,
and turns its verdicts into ``mistake``/``correction`` checkpoints — which auto-trigger the
existing reflection loop. It catches the *semantic* mistakes deterministic rules cannot encode.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import checkpoints as memory

# BC first-principles review checklist — the questions a reviewer must answer for every diff.
FIRST_PRINCIPLES_CHECKLIST: List[Dict[str, str]] = [
    {"id": "upgrade_scope",
     "question": "Does each data-upgrade codeunit's scope match the target table's DataPerCompany? "
                 "(false = shared -> per-database, no company guard; true -> per-company)."},
    {"id": "field_length",
     "question": "Are all new table field names <= 30 characters (AL0468)?"},
    {"id": "permissions",
     "question": "Are permissions correct at the table level (no needless permission-set edits; "
                 "a write grant exists for any mutation)?"},
    {"id": "api_versioning",
     "question": "For API changes, is the current non-obsolete version extended in place "
                 "(no new version, not an ObsoleteState=Pending page)?"},
    {"id": "editable_semantics",
     "question": "Do Editable/NotEditable and other property choices match the item's intent "
                 "and the table's own semantics?"},
    {"id": "idempotent_upgrade",
     "question": "Is the upgrade idempotent/guarded (Get before Modify; no duplicate inserts) "
                 "and registered with a unique upgrade tag?"},
    {"id": "scope_creep",
     "question": "Do the changes stay within the Charter's stated scope (no unrelated objects "
                 "or fields touched)?"},
    {"id": "translations",
     "question": "Are required translations (e.g. NLD .xlf) handled, or explicitly deferred to "
                 "the build that regenerates the .g.xlf?"},
]


def _knowledge_worklist(
    root: Path,
    charter: Dict[str, Any],
    changed_files: List[str],
) -> List[Dict[str, Any]]:
    """Index-aware worklist (BCQuality contract): rank the knowledge corpus against
    the Charter + changed files and return lean discovery entries. The reviewer
    opens each worklisted article IN FULL for its ## Best Practice / ## Anti
    Pattern rule bodies — the index is never a substitute for them. Fail-open:
    no corpus or any error -> []."""
    try:
        from bc_agentic_mcp import knowledge
        query = " ".join(filter(None, [
            str(charter.get("purpose") or ""),
            " ".join(str(c) for c in charter.get("acceptance_criteria") or []),
            " ".join(Path(f).stem for f in changed_files),
        ]))
        worklist = knowledge.select_articles(root, query)
        return [{"path": a.get("path"), "layer": a.get("layer"), "domain": a.get("domain"),
                 "title": a.get("title"), "description": a.get("description"),
                 "file": a.get("file"), "score": a.get("score"), "parsed": a.get("parsed")}
                for a in worklist]
    except Exception:  # noqa: BLE001 — knowledge is advisory, never blocks review
        return []


def build_review_packet(
    project_root: Path,
    spec_name: str,
    changed_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble the deterministic packet a reviewer instance evaluates."""
    root = Path(project_root).resolve()
    charter = memory.load_charter(root, spec_name) or {}
    recent = memory.load_checkpoints(root, spec_name)[-8:]
    knowledge_articles = _knowledge_worklist(root, charter, changed_files or [])
    instructions = (
        "You are a SEPARATE reviewer (not the implementer). Answer each checklist item "
        "against the diff and the Charter. For every problem, return a finding "
        "{id, kind: 'mistake'|'correction', severity, summary}. Findings are recorded as "
        "checkpoints and will trigger the reflection loop."
    )
    if knowledge_articles:
        instructions += (
            " Additionally: 'knowledge' lists corpus articles matched to this change — "
            "read each listed file IN FULL and apply its ## Best Practice / ## Anti Pattern "
            "rules as extra checklist items (the index entry is only a discovery hint)."
        )
    return {
        "spec_name": spec_name,
        "charter": {
            "purpose": charter.get("purpose"),
            "operations": charter.get("operations", {}),
            "acceptance_criteria": charter.get("acceptance_criteria", []),
        },
        "changed_files": changed_files or [],
        "recent_checkpoints": [{"kind": c.get("kind"), "summary": c.get("summary")} for c in recent],
        "checklist": FIRST_PRINCIPLES_CHECKLIST,
        "knowledge": knowledge_articles,
        "instructions": instructions,
    }


def record_findings(
    project_root: Path,
    spec_name: str,
    findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Record reviewer findings as checkpoints (which auto-trigger reflection)."""
    root = Path(project_root).resolve()
    recorded = 0
    for finding in findings or []:
        summary = (finding.get("summary") or "").strip()
        if not summary:
            continue
        memory.append_checkpoint(
            root, spec_name,
            kind=finding.get("kind", "correction"),
            summary=f"[reviewer:{finding.get('id', 'general')}] {summary}",
            details={"source": "reviewer", "id": finding.get("id"),
                     "severity": finding.get("severity", "warning")},
        )
        recorded += 1
    return {"recorded": recorded}


RUBRIC_DIMENSIONS = ("grounding", "coverage", "conventions", "risk")


def record_rubric(
    project_root: Path,
    spec_name: str,
    rubric: Dict[str, Any],
    verdict: str = "",
) -> Dict[str, Any]:
    """Record an LLM-as-judge quality rubric (Anthropic pattern: one judge call,
    0.0-1.0 per dimension + overall pass/fail) so prompt/process changes become
    MEASURABLE over time instead of vibes. Appends to review_rubric.json."""
    from datetime import datetime, timezone
    from bc_agentic_mcp.workspace import specs_root as _specs_root
    scores: Dict[str, float] = {}
    problems = []
    for dim in RUBRIC_DIMENSIONS:
        raw = rubric.get(dim)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{dim}: missing or non-numeric")
            continue
        if not 0.0 <= val <= 1.0:
            problems.append(f"{dim}: {val} outside 0.0-1.0")
            continue
        scores[dim] = round(val, 3)
    if problems:
        return {"status": "error",
                "reason": "invalid rubric: " + "; ".join(problems),
                "expected": {d: "0.0-1.0" for d in RUBRIC_DIMENSIONS}}
    overall = round(sum(scores.values()) / len(scores), 3)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "scores": scores,
        "overall": overall,
        "passed": overall >= 0.7 and min(scores.values()) >= 0.5,
        "verdict": str(verdict or ""),
        "note": str(rubric.get("note") or "")[:400],
    }
    path = _specs_root(project_root) / spec_name / "review_rubric.json"
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = []
    history.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return {"status": "rubric_recorded", "overall": overall, "passed": entry["passed"],
            "history_length": len(history)}


def handle_review(
    project_root: str,
    spec_name: str,
    findings: Optional[List[Dict[str, Any]]] = None,
    changed_files: Optional[List[str]] = None,
    rubric: Optional[Dict[str, Any]] = None,
    verdict: str = "",
) -> Dict[str, Any]:
    """bc_review: with findings -> record them (triggers reflection); without -> return the packet.
    An optional rubric ({grounding, coverage, conventions, risk} each 0.0-1.0) records a
    quality score so review outcomes become measurable across items and prompt versions."""
    root = Path(project_root).resolve()
    rubric_result: Optional[Dict[str, Any]] = None
    if rubric:
        rubric_result = record_rubric(root, spec_name, rubric, verdict=verdict)
        if rubric_result.get("status") == "error":
            return rubric_result
    if findings:
        result = record_findings(root, spec_name, findings)
        out = {
            "spec_name": spec_name,
            "findings_recorded": result["recorded"],
            "note": "Findings recorded as checkpoints; reflection_due will nudge until bc_reflect.",
        }
        if rubric_result:
            out["rubric"] = rubric_result
        return out
    if rubric_result:
        return {"spec_name": spec_name, "rubric": rubric_result}
    return build_review_packet(root, spec_name, changed_files=changed_files)
