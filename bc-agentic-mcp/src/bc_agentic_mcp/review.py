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
from uuid import uuid4

from bc_agentic_mcp import checkpoints as memory

# BC first-principles review checklist — MCP-specific gates that are NOT covered by
# BCQuality knowledge articles (scope/process concerns, not AL language rules).
# AL language rules (field_length/api_versioning/idempotent_upgrade/upgrade_scope)
# are now owned by the BCQuality corpus surfaced in the review packet's 'knowledge' key.
FIRST_PRINCIPLES_CHECKLIST: List[Dict[str, str]] = [
    {"id": "permissions",
     "question": "Are permissions correct at the table level (no needless permission-set edits; "
                 "a write grant exists for any mutation)?"},
    {"id": "editable_semantics",
     "question": "Do Editable/NotEditable and other property choices match the item's intent "
                 "and the table's own semantics?"},
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
    calls ``bc_get_knowledge_article`` for each entry to read the full article
    (including ## Best Practice / ## Anti Pattern and .good.al/.bad.al companions)
    before submitting findings.  Fail-closed when vendor is present: any error
    surfaces as an empty list with a logged warning rather than being swallowed."""
    from bc_agentic_mcp import knowledge
    query = " ".join(filter(None, [
        str(charter.get("purpose") or ""),
        " ".join(str(c) for c in charter.get("acceptance_criteria") or []),
        " ".join(Path(f).stem for f in changed_files),
    ]))
    vendor_present = knowledge.vendor_root(root) is not None
    try:
        worklist = knowledge.select_articles(root, query)
        return [{"path": a.get("path"), "layer": a.get("layer"), "domain": a.get("domain"),
                 "title": a.get("title"), "description": a.get("description"),
                 "file": a.get("file"), "score": a.get("score"), "parsed": a.get("parsed"),
                 "companions": a.get("companions", [])}
                for a in worklist]
    except Exception:  # noqa: BLE001
        if vendor_present:
            # Vendor is present but retrieval failed — surface as empty with a sentinel
            # so the packet signals the problem rather than silently omitting knowledge.
            return [{"path": "__error__", "layer": "", "domain": "",
                     "title": "Knowledge retrieval failed",
                     "description": "BCQuality corpus error — call bc_get_knowledge_article with a known path or re-index.",
                     "file": "", "score": 0, "parsed": False, "companions": []}]
        return []


def _stored_knowledge_receipts(root: Path, spec_name: str) -> List[str]:
    path = memory.specs_root(root) / spec_name / "knowledge_reads.jsonl"
    if not path.exists():
        return []
    receipts: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("receipt"):
                receipts.append(str(row["receipt"]))
    except OSError:
        return []
    return receipts


def _knowledge_packet_coverage(root: Path, spec_name: str) -> Dict[str, Any]:
    """Validate reads recorded by bc_get_knowledge_article for the current packet."""
    from bc_agentic_mcp import knowledge
    meta_path = memory.specs_root(root) / spec_name / "review_packet_meta.json"
    if not meta_path.exists():
        return {"required": False, "ok": True, "reason": "no review packet metadata"}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"required": True, "ok": False, "reason": f"review packet metadata unreadable: {exc}"}
    paths = [str(p) for p in meta.get("packet_article_paths") or []]
    required = bool(paths) or bool(meta.get("knowledge_error"))
    if not required:
        return {"required": False, "ok": True, "reason": "packet had no knowledge worklist"}
    if meta.get("knowledge_error"):
        return {
            "required": True,
            "ok": False,
            "reason": "knowledge retrieval failed while building the review packet",
        }
    health = meta.get("vendor_health") or {}
    coverage = knowledge.validate_knowledge_receipts(
        root,
        spec_name,
        str(meta.get("packet_id") or ""),
        paths,
        str(health.get("commit") or ""),
        _stored_knowledge_receipts(root, spec_name),
    )
    coverage.update({"required": True, "packet_id": str(meta.get("packet_id") or ""), "packet_paths": paths})
    return coverage


def build_review_packet(
    project_root: Path,
    spec_name: str,
    changed_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble the deterministic packet a reviewer instance evaluates."""
    from datetime import datetime, timezone
    from bc_agentic_mcp import knowledge
    from bc_agentic_mcp.workspace import specs_root as _specs_root
    root = Path(project_root).resolve()
    charter = memory.load_charter(root, spec_name) or {}
    recent = memory.load_checkpoints(root, spec_name)[-8:]
    knowledge_articles = _knowledge_worklist(root, charter, changed_files or [])
    real_articles = [a for a in knowledge_articles if a.get("path") != "__error__"]
    packet_article_count = len(real_articles)
    packet_article_paths = [str(a["path"]) for a in real_articles if a.get("path")]
    packet_id = uuid4().hex
    vendor_health = knowledge.check_vendor_health(root)
    knowledge_error = any(a.get("path") == "__error__" for a in knowledge_articles) or bool(
        vendor_health.get("configured") and vendor_health.get("errors")
    )
    # Persist packet metadata so the verification gate can check coverage
    # without re-running BM25 (self-challenge failure mode 2 mitigation).
    try:
        meta_path = _specs_root(root) / spec_name / "review_packet_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        meta_path.write_text(
            _json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "packet_id": packet_id,
                "packet_article_count": packet_article_count,
                "packet_article_paths": packet_article_paths,
                "knowledge_error": knowledge_error,
                "vendor_health": vendor_health,
            }, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"review packet metadata could not be persisted: {exc}") from exc
    if knowledge_error:
        instructions = (
            "Knowledge retrieval failed while preparing this packet. Do not submit findings. "
            "Repair or re-index the BCQuality corpus and regenerate the review packet."
        )
    elif packet_article_count > 0:
        instructions = (
            "You are a SEPARATE reviewer (not the implementer). Answer each checklist item "
            "against the diff and the Charter. For every problem, return a finding "
            "{id, kind: 'mistake'|'correction', severity, summary, bcquality_refs: [path, ...]}. "
            "Findings are recorded as checkpoints and will trigger the reflection loop."
            " REQUIRED: for each article listed in 'knowledge', call bc_get_knowledge_article "
            f"with spec_name='{spec_name}' and packet_id='{packet_id}' "
            "to read its full ## Best Practice / ## Anti Pattern bodies and companion "
            ".good.al/.bad.al golden templates BEFORE submitting findings. "
            "Cite the article path in bcquality_refs for every finding it informed. "
            "When done, also pass knowledge_applied=[list of paths you read] to bc_review."
        )
    else:
        instructions = (
            "You are a SEPARATE reviewer (not the implementer). Answer each checklist item "
            "against the diff and the Charter. For every problem, return a finding "
            "{id, kind: 'mistake'|'correction', severity, summary}. Findings are recorded as "
            "checkpoints and will trigger the reflection loop."
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
        "packet_id": packet_id,
        "packet_article_paths": packet_article_paths,
        "packet_article_count": packet_article_count,
        "knowledge_error": knowledge_error,
        "vendor_health": vendor_health,
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
    knowledge_applied: Optional[List[str]] = None,
    knowledge_receipts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """bc_review: with findings -> record them (triggers reflection); without -> return the packet.

    ``bc_get_knowledge_article`` records signed reads for the current review packet.
    ``knowledge_applied`` is retained as an audit label, but the verify gate trusts
    only those server-issued receipts. Each finding may carry a ``bcquality_refs``
    list of article paths that informed it.

    An optional rubric ({grounding, coverage, conventions, risk} each 0.0-1.0) records a
    quality score so review outcomes become measurable across items and prompt versions.
    """
    from datetime import datetime, timezone
    from bc_agentic_mcp.workspace import specs_root as _specs_root
    root = Path(project_root).resolve()
    rubric_result: Optional[Dict[str, Any]] = None
    if rubric:
        rubric_result = record_rubric(root, spec_name, rubric, verdict=verdict)
        if rubric_result.get("status") == "error":
            return rubric_result
    knowledge_trace_written = False
    validated_knowledge: Optional[Dict[str, Any]] = None
    packet_knowledge = _knowledge_packet_coverage(root, spec_name)
    if packet_knowledge.get("required"):
        if knowledge_receipts:
            reads_path = _specs_root(root) / spec_name / "knowledge_reads.jsonl"
            reads_path.parent.mkdir(parents=True, exist_ok=True)
            with reads_path.open("a", encoding="utf-8") as handle:
                for receipt in knowledge_receipts:
                    handle.write(json.dumps({"receipt": receipt}) + "\n")
            packet_knowledge = _knowledge_packet_coverage(root, spec_name)
        if not packet_knowledge.get("ok"):
            return {
                "status": "blocked_knowledge_coverage",
                "blocked": True,
                "reason": packet_knowledge.get("reason") or "knowledge coverage is incomplete",
                "packet_id": packet_knowledge.get("packet_id"),
                "required_articles": packet_knowledge.get("packet_paths", []),
                "missing_articles": packet_knowledge.get("missing", []),
                "next_action": {
                    "tool": "bc_get_knowledge_article",
                    "reason": "Read every worklisted article with the current packet_id before review.",
                },
            }
        if knowledge_applied is not None and set(knowledge_applied) != set(packet_knowledge.get("paths", [])):
            return {
                "status": "blocked_knowledge_coverage",
                "blocked": True,
                "reason": "knowledge_applied does not exactly match the server-verified article reads",
                "required_articles": packet_knowledge.get("paths", []),
            }
        validated_knowledge = packet_knowledge
    elif knowledge_applied:
        return {
            "status": "blocked_knowledge_coverage",
            "blocked": True,
            "reason": "knowledge_applied was supplied but the current packet has no matching worklist",
        }

    # Write a trace only from server-verified article reads. Caller-provided paths
    # are an audit label, never the evidence of a read.
    if validated_knowledge is not None:
        try:
            trace_path = _specs_root(root) / spec_name / "knowledge_trace.json"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            from bc_agentic_mcp import knowledge as _k
            commit = _k.check_vendor_health(root).get("commit", "")
            # Collect all bcquality_refs cited in findings
            cited_refs: List[str] = []
            for f in findings or []:
                cited_refs.extend(f.get("bcquality_refs") or [])
            trace_path.write_text(
                json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "spec_name": spec_name,
                    "packet_id": validated_knowledge.get("packet_id"),
                    "vendor_commit": commit,
                    "articles_applied": validated_knowledge.get("paths", []),
                    "knowledge_receipts": validated_knowledge.get("receipts", []),
                    "articles_cited_in_findings": sorted(set(cited_refs)),
                }, indent=2),
                encoding="utf-8",
            )
            knowledge_trace_written = True
        except OSError as exc:
            return {
                "status": "blocked_knowledge_coverage",
                "blocked": True,
                "reason": f"knowledge trace could not be persisted: {exc}",
            }
    if findings:
        result = record_findings(root, spec_name, findings)
        # Return findings inline — the human gate REQUIRES the reviewer to surface them
        # to the human for approval/rejection.  Returning only a count forces the human
        # to explicitly ask for the findings, which is a workflow gap.
        findings_summary = [
            {
                "id": f.get("id"),
                "kind": f.get("kind"),
                "severity": f.get("severity"),
                "summary": f.get("summary"),
                "bcquality_refs": f.get("bcquality_refs") or [],
            }
            for f in (findings or [])
        ]
        blockers = [f for f in findings_summary if f["severity"] in ("error", "warning") and f["kind"] == "mistake"]
        out = {
            "spec_name": spec_name,
            "findings_recorded": result["recorded"],
            "findings": findings_summary,
            "blockers": blockers,
            "knowledge_trace_written": knowledge_trace_written,
            "knowledge_applied_count": len(validated_knowledge.get("paths", [])) if validated_knowledge else 0,
            # Human gate: the agent MUST present these findings to the human and ask
            # for an explicit decision before advancing the lifecycle.
            "human_gate_required": True,
            "next_action": (
                "PRESENT these findings to the human NOW and ask: "
                "'Do you approve this review, request changes, or reject? "
                "Blockers must be resolved before bc_reflect / bc_verify can pass.'"
                if blockers else
                "PRESENT these findings to the human NOW and ask: "
                "'Review complete with informational findings only — approve to continue?'"
            ),
        }
        if rubric_result:
            out["rubric"] = rubric_result
        return out
    if validated_knowledge is not None and not findings:
        return {
            "spec_name": spec_name,
            "knowledge_trace_written": knowledge_trace_written,
            "articles_applied": validated_knowledge.get("paths", []),
        }
    if rubric_result:
        return {"spec_name": spec_name, "rubric": rubric_result}
    return build_review_packet(root, spec_name, changed_files=changed_files)
