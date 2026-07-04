"""bc_root_cause — the bugfix lane's diagnosis step: symptom -> VERIFIED root cause.

A bug is never fixed from its ticket text. The model's diagnosis (judgment) must be
CONFRONTED with code reality before any spec or fix is written — the bug-lane twin of
bc_refine_item. Every evidence reference (AL file path or object reference) is verified
against the repo / persistent object index; anything unverifiable blocks fail-closed.

Artifacts: ROOT-CAUSE.md (human) + root_cause.json (machine, consumed by the
`root_cause` enforcement engine, the bugfix spec builder and the archive learning loop).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import object_index
from bc_agentic_mcp.workspace import specs_root

_OBJECT_REF_RE = re.compile(
    r"^(tableextension|table|pageextension|page|codeunit|reportextension|report|"
    r"enumextension|enum|query|xmlport|interface|permissionset)\s+(.+)$",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_evidence(root: Path, refs: List[str]) -> List[Dict[str, Any]]:
    """Deterministically verify each evidence reference against code reality.

    Three accepted shapes:
    - AL file path (relative to project root, or absolute inside it) -> must exist
    - "<kind> <number-or-name>" object reference -> must resolve in the object index
    - bare object name -> must resolve in the object index by name
    """
    index: Optional[Dict[str, Any]] = None
    verified: List[Dict[str, Any]] = []
    for raw in refs:
        ref = str(raw or "").strip()
        entry: Dict[str, Any] = {"ref": ref, "kind": "unknown", "verified": False}
        if not ref:
            verified.append(entry)
            continue
        if ".al" in ref.lower():
            entry["kind"] = "file"
            p = Path(ref)
            candidate = p if p.is_absolute() else (root / ref)
            try:
                resolved = candidate.resolve()
                if resolved.exists() and str(resolved).lower().startswith(str(root).lower()):
                    entry["verified"] = True
                    entry["file"] = str(resolved)
            except OSError:
                pass
            verified.append(entry)
            continue
        # Object reference — resolve via the persistent index (built once, stat-refreshed).
        if index is None:
            index = object_index.refresh(root)["objects"]
        m = _OBJECT_REF_RE.match(ref)
        key = f"{m.group(1).lower()} {m.group(2).strip()}" if m else ref.lower()
        hit = index.get(key) or index.get(ref.lower()) or (
            index.get(m.group(2).strip().lower()) if m else None
        )
        entry["kind"] = "object"
        if hit:
            entry["verified"] = True
            entry["object"] = f"{hit['kind']} {hit['number']} {hit['name']}"
            entry["file"] = hit.get("file", "")
        verified.append(entry)
    return verified


def _render_md(payload: Dict[str, Any]) -> str:
    lines = [
        f"# Root Cause — {payload['spec_name']}",
        "",
        f"_Item {payload.get('item_id', '?')} · recorded {payload['generated_at']}_",
        "",
        "## Symptom (observed wrong behavior)",
        payload["symptom"],
        "",
        "## Root cause (diagnosis, verified against code)",
        payload["root_cause"],
        "",
        "## Evidence (confronted with code reality)",
    ]
    for e in payload["evidence"]:
        mark = "VERIFIED" if e["verified"] else "UNVERIFIED"
        extra = e.get("object") or e.get("file") or ""
        lines.append(f"- [{mark}] {e['ref']}" + (f" -> {extra}" if extra else ""))
    lines += [
        "",
        "## Fix approach",
        payload["fix_approach"],
        "",
        "## Regression risk",
        payload.get("regression_risk") or "_not stated_",
        "",
        "> The fix spec MUST carry a regression requirement reproducing this symptom: "
        "a test that fails on the pre-fix code and passes after (layer='al-regression').",
    ]
    return "\n".join(lines) + "\n"


def handle_root_cause(
    project_root: str,
    spec_name: str,
    symptom: str,
    root_cause: str,
    evidence: List[str],
    fix_approach: str,
    regression_risk: Optional[str] = None,
) -> Dict[str, Any]:
    """Record the bug diagnosis with evidence verified against the repo. SYNC on
    purpose (blocking index I/O -> worker thread, timeout enforceable)."""
    root = Path(project_root).resolve()
    sdir = specs_root(root) / spec_name
    manifest_path = sdir / "context" / "manifest.json"
    if not manifest_path.exists():
        return {
            "status": "blocked_no_capture",
            "blocked": True,
            "reason": "No captured item context — run bc_capture_item_context first.",
            "next_action": {"tool": "bc_capture_item_context",
                            "params_hint": {"spec_name": spec_name}},
        }
    if not str(symptom or "").strip() or not str(root_cause or "").strip() \
            or not str(fix_approach or "").strip():
        return {
            "status": "blocked_incomplete",
            "blocked": True,
            "reason": "symptom, root_cause and fix_approach are all mandatory — "
                      "a bug without a stated diagnosis cannot enter the fix lifecycle.",
        }
    refs = [r for r in (evidence or []) if str(r or "").strip()]
    if not refs:
        return {
            "status": "blocked_no_evidence",
            "blocked": True,
            "reason": "At least one evidence reference (AL file path or object reference) "
                      "is required — a root cause without code evidence is a guess.",
        }
    checked = _verify_evidence(root, refs)
    unverified = [e["ref"] for e in checked if not e["verified"]]
    if unverified:
        return {
            "status": "blocked_evidence_unverified",
            "blocked": True,
            "reason": f"{len(unverified)} evidence reference(s) could not be verified "
                      "against the repo — fix the reference(s), never force through.",
            "unverified": unverified,
            "verified": [e["ref"] for e in checked if e["verified"]],
            "next_action": {
                "tool": "bc_root_cause",
                "reason": "Re-submit with corrected evidence references",
                "params_hint": {"spec_name": spec_name,
                                "evidence": ["<existing .al path or 'table 11024121'>"]},
            },
        }
    try:
        item_id = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("item_id", ""))
    except (OSError, json.JSONDecodeError):
        item_id = ""
    payload = {
        "spec_name": spec_name,
        "item_id": item_id,
        "lane": "bugfix",
        "symptom": str(symptom).strip(),
        "root_cause": str(root_cause).strip(),
        "fix_approach": str(fix_approach).strip(),
        "regression_risk": str(regression_risk).strip() if regression_risk else "",
        "evidence": checked,
        "generated_at": _now(),
    }
    sdir.mkdir(parents=True, exist_ok=True)
    json_path = sdir / "root_cause.json"
    md_path = sdir / "ROOT-CAUSE.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(payload), encoding="utf-8")
    return {
        "status": "root_cause_recorded",
        "root_cause_path": str(md_path),
        "root_cause_json": str(json_path),
        "evidence_verified": len(checked),
        "next_action": {
            "tool": "bc_write_spec",
            "reason": "Write the FIX spec from the verified diagnosis — it must include a "
                      "regression requirement reproducing the symptom (test red before fix, "
                      "green after).",
            "params_hint": {"spec_name": spec_name,
                            "human_bullets": "<fix requirements derived from ROOT-CAUSE.md>",
                            "idempotency_key": f"{spec_name}-fix-1"},
        },
    }
