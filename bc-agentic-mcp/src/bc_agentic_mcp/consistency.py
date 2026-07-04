"""consistency — cross-artifact story check: spec ↔ DESIGN ↔ TASKS ↔ charter.

Traceability (spec-internal) already proves every acceptance criterion maps to a
requirement. THIS engine proves the artifacts agree with each other:

  * every requirement is addressed by the DESIGN and carried by at least one TASK
  * every in-scope file is actually referenced by the plan (and no task touches
    a file the spec forbids — the classic silent scope drift)
  * every charter acceptance criterion is represented by a requirement

Plus a deterministic CHECKLIST ("unit tests for English"): per-requirement
quality probes a human can answer in seconds before the plan gate.

Pure disk reads; writes CONSISTENCY.md + consistency.json into the item folder.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from bc_agentic_mcp.workspace import specs_root

_AL_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.al\b")
_MEASURABLE_RE = re.compile(
    r"\b(shall|must|returns?|raises?|shows?|hides?|blocks?|validates?|creates?|sets?|equals?)\b",
    re.IGNORECASE)
_OBJECT_RE = re.compile(
    r"\b(table|tableextension|page|pageextension|codeunit|enum|enumextension|report|query)\b",
    re.IGNORECASE)
_NEGATIVE_RE = re.compile(r"\b(not|never|error|refuse|reject|block|invalid|fail)\b", re.IGNORECASE)

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "for", "on", "with",
         "that", "this", "be", "are", "it", "as", "at", "by", "when", "then", "shall",
         "must", "system", "should", "new", "existing"}


def _tokens(text: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", str(text or "").lower())
            if len(w) > 2 and w not in _STOP}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


def _norm_path(p: str) -> str:
    return str(p or "").replace("\\", "/").lstrip("./").lower()


def _requirement_checklist(req: Dict[str, Any]) -> List[Dict[str, str]]:
    statement = str(req.get("statement") or "")
    rid = str(req.get("id") or "REQ-???")
    checks = [
        ("names_target",
         f"{rid}: does it name the concrete AL object (table/page/codeunit) it changes?",
         "pass" if _OBJECT_RE.search(statement) else "review"),
        ("measurable",
         f"{rid}: is the outcome verifiable (shall/returns/shows/raises...)?",
         "pass" if _MEASURABLE_RE.search(statement) else "review"),
        ("atomic",
         f"{rid}: is it ONE requirement (no 'and also' bundling)?",
         "review" if len(statement) > 220 or statement.lower().count(" and ") >= 2 else "pass"),
    ]
    return [{"id": f"{rid}:{cid}", "question": q, "verdict": v} for cid, q, v in checks]


def analyze(project_root: Path, spec_name: str) -> Dict[str, Any]:
    sdir = specs_root(Path(project_root).resolve()) / spec_name
    spec_path = sdir / "spec.json"
    if not spec_path.exists():
        return {"status": "blocked_no_spec", "blocked": True,
                "reason": "No spec.json yet — bc_write_spec first."}
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "reason": f"spec.json unreadable: {exc}"}

    requirements = [r for r in (spec.get("requirements") or []) if isinstance(r, dict)]
    allowed = [f for f in ((spec.get("scope_boundaries") or {}).get("allowed_files") or [])
               if isinstance(f, str) and not f.startswith("-")]
    design = (sdir / "DESIGN.md").read_text(encoding="utf-8", errors="replace") \
        if (sdir / "DESIGN.md").exists() else ""
    tasks = (sdir / "TASKS.md").read_text(encoding="utf-8", errors="replace") \
        if (sdir / "TASKS.md").exists() else ""
    charter: Dict[str, Any] = {}
    if (sdir / "charter.json").exists():
        try:
            charter = json.loads((sdir / "charter.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            charter = {}

    critical: List[str] = []
    warnings: List[str] = []

    # 1) requirement -> DESIGN and requirement -> TASKS
    for req in requirements:
        rid = str(req.get("id") or "")
        statement = str(req.get("statement") or "")
        in_design = bool(rid and rid in design) or _overlap(statement, design) >= 0.4
        in_tasks = bool(rid and rid in tasks) or _overlap(statement, tasks) >= 0.35
        if design and not in_design:
            warnings.append(f"{rid or statement[:40]}: not addressed in DESIGN.md")
        if tasks and not in_tasks:
            critical.append(f"{rid or statement[:40]}: no task carries this requirement")

    # 2) scope files <-> plan text
    plan_text = f"{design}\n{tasks}"
    plan_norm = _norm_path(plan_text)
    for path in allowed:
        norm = _norm_path(path)
        stem = norm.rsplit("/", 1)[-1]
        if stem and stem not in plan_norm:
            warnings.append(f"in-scope file never mentioned by DESIGN/TASKS: {path}")
    if allowed:
        allowed_stems = {_norm_path(p).rsplit("/", 1)[-1] for p in allowed}
        for mention in set(_AL_PATH_RE.findall(tasks)):
            stem = _norm_path(mention).rsplit("/", 1)[-1]
            if stem and stem not in allowed_stems:
                critical.append(
                    f"TASKS references a file OUTSIDE the spec's scope: {mention} "
                    "(scope drift — extend scope_boundaries deliberately or fix the task)")

    # 3) charter criteria -> requirements
    req_blob = " ".join(str(r.get("statement") or "") for r in requirements)
    for criterion in charter.get("acceptance_criteria") or []:
        if _overlap(str(criterion), req_blob) < 0.3:
            warnings.append(f"charter criterion not represented by any requirement: "
                            f"{str(criterion)[:80]}")

    checklist: List[Dict[str, str]] = []
    for req in requirements:
        checklist.extend(_requirement_checklist(req))

    ok = not critical
    result = {
        "status": "consistent" if ok and not warnings else ("blocked_inconsistent" if critical else "warnings"),
        "ok": ok,
        "critical": critical,
        "warnings": warnings,
        "checklist": checklist,
        "requirements": len(requirements),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (sdir / "consistency.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (sdir / "CONSISTENCY.md").write_text(_render_md(spec_name, result), encoding="utf-8")
    if critical:
        result["next_action"] = {
            "tool": "bc_breakdown_tasks" if any("no task" in c for c in critical) else "bc_write_spec",
            "reason": "Fix the critical cross-artifact gaps, then re-run bc_analyze_consistency.",
            "params_hint": {"spec_name": spec_name},
        }
    return result


def _render_md(spec_name: str, r: Dict[str, Any]) -> str:
    lines = [
        f"# Consistency check: {spec_name}",
        "",
        f"_Generated {r['generated_at']} — {r['requirements']} requirement(s)._",
        "",
        f"## Verdict: **{r['status']}**",
        "",
        "## Critical (block the plan gate)",
    ]
    lines += [f"- ❌ {c}" for c in r["critical"]] or ["- none 🎉"]
    lines += ["", "## Warnings"]
    lines += [f"- ⚠ {w}" for w in r["warnings"]] or ["- none"]
    lines += ["", "## Requirement checklist (answer before requesting approval)"]
    for item in r["checklist"]:
        mark = "✅" if item["verdict"] == "pass" else "🔎"
        lines.append(f"- {mark} {item['question']}")
    lines.append("")
    return "\n".join(lines)


def handle_analyze_consistency(project_root: str, spec_name: str) -> Dict[str, Any]:
    """Tool handler: cross-artifact consistency + requirement checklist."""
    return analyze(Path(project_root), spec_name)
