"""enforcement — deterministic proof that each engine actually ran and passed for an item.

The four engines (timeline, traceability, code-context grounding, quality/analyzers) are only
trustworthy if their execution is *mechanically verifiable*, not assumed. This module inspects
the on-disk artifacts in the item's folder and returns a per-engine ``{ran, ok, reason}`` status,
recomputing the cheap/authoritative parts (traceability from spec.json; quality freshness from
the spec hash) rather than trusting a stale flag.

It is consumed by two hooks:
  * the git **pre-commit gate** (gate.py) — blocks AL commits when an engine did not run/pass;
  * the planner **quality gate** (prepare_review) and detectors — surface the same status.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import traceability
from bc_agentic_mcp.workspace import specs_root

# Engines that must have run + passed before spec-scoped AL code may be committed.
# `refinement` (claims x code reality) sits right after timeline: nothing downstream
# (spec, design, code context) may be built on unverified ticket claims.
# `root_cause` is the bugfix lane's twin: a Bug may not be planned/fixed before its
# diagnosis is recorded with verified code evidence (non-bug lanes pass trivially).
REQUIRED_ENGINES = ("timeline", "refinement", "root_cause", "traceability", "code_context", "quality", "clarifications")

_QUESTION_RE = re.compile(r"^##\s+(Q-\d{3}):\s+(.+)$")
_ANSWER_RE = re.compile(r"^_Answer:_\s*(.*)$")
_UNSURE_RE = re.compile(r"\b(tbd|unknown|unsure|not sure|maybe|n/?a)\b", re.IGNORECASE)
_AL_EVIDENCE_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.al\b", re.IGNORECASE)


def _spec_sha(spec_path: Path) -> str:
    try:
        return hashlib.sha256(spec_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _timeline_status(root: Path, spec: str) -> Dict[str, Any]:
    phases = [c for c in memory.load_checkpoints(root, spec) if c.get("kind") == "phase"]
    ok = bool(phases)
    return {
        "ran": ok,
        "ok": ok,
        "reason": "" if ok else "no lifecycle phase recorded — the item was not driven through the timeline",
        "detail": {"phase_events": len(phases)},
        **(
            {}
            if ok
            else {
                "next_action": {
                    "tool": "bc_capture_item_context",
                    "reason": "Start the lifecycle by capturing the work item context",
                    "params_hint": {"spec_name": spec},
                }
            }
        ),
    }


def _traceability_status(sdir: Path) -> Dict[str, Any]:
    spec_path = sdir / "spec.json"
    if not spec_path.exists():
        return {"ran": False, "ok": False, "reason": "no spec.json (write the spec first)"}
    try:
        spec_json = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ran": False, "ok": False, "reason": f"spec.json unreadable: {exc}"}
    tr = traceability.trace_spec(spec_json)
    reason = ""
    if not tr["ok"]:
        bits = []
        if tr["uncovered"]:
            bits.append(f"uncovered={tr['uncovered'][:5]}")
        if tr["orphaned"]:
            bits.append(f"orphaned={tr['orphaned'][:5]}")
        if tr["total_requirements"] == 0:
            bits.append("no requirements")
        reason = "traceability gaps: " + ", ".join(bits)
    result: Dict[str, Any] = {"ran": True, "ok": tr["ok"], "reason": reason,
                              "detail": {"coverage_pct": tr["coverage_pct"]}}
    if not tr["ok"]:
        result["next_action"] = {
            "tool": "bc_write_spec",
            "reason": "Re-generate the spec to rebuild traceability coverage",
            "params_hint": {"spec_name": sdir.name, "human_bullets": "<requirement bullets>",
                            "idempotency_key": "<unique-key>"},
        }
    return result


def _code_context_status(sdir: Path) -> Dict[str, Any]:
    cc = sdir / "context" / "code" / "code_context.json"
    if not cc.exists():
        return {
            "ran": False,
            "ok": False,
            "reason": "no code read-context (run bc_read_code_context on clean, latest source)",
            "next_action": {
                "tool": "bc_read_code_context",
                "reason": "Build code context from the latest clean source checkout",
                "params_hint": {"spec_name": sdir.name, "require_clean_latest": True},
            },
        }
    return {"ran": True, "ok": True, "reason": "", "detail": {"path": str(cc)}}


def _quality_status(sdir: Path) -> Dict[str, Any]:
    q = sdir / "quality.json"
    if not q.exists():
        return {
            "ran": False,
            "ok": False,
            "reason": "no quality run (run bc_quality_check — analyzers/validator)",
            "next_action": {
                "tool": "bc_quality_check",
                "reason": "Run AL analyzers to establish quality baseline",
                "params_hint": {"spec_name": sdir.name},
            },
        }
    try:
        data = json.loads(q.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ran": False, "ok": False, "reason": f"quality.json unreadable: {exc}"}
    errors = int(data.get("errors", 0))
    spec_path = sdir / "spec.json"
    stale = spec_path.exists() and data.get("spec_sha") not in (None, "", _spec_sha(spec_path))
    if stale:
        return {
            "ran": True,
            "ok": False,
            "reason": "quality run is stale (spec changed since) — re-run bc_quality_check",
            "detail": {"errors": errors},
            "next_action": {
                "tool": "bc_quality_check",
                "reason": "Spec changed since last quality run — re-run to refresh",
                "params_hint": {"spec_name": sdir.name},
            },
        }
    ok = errors == 0
    result: Dict[str, Any] = {
        "ran": True,
        "ok": ok,
        "reason": "" if ok else f"{errors} error diagnostic(s) — fix before commit",
        "detail": {"errors": errors, "mode": data.get("mode")},
    }
    if not ok:
        result["next_action"] = {
            "tool": "bc_quality_check",
            "reason": f"{errors} diagnostic error(s) must be fixed before commit",
            "params_hint": {"spec_name": sdir.name},
        }
    return result


def _clarifications_status(sdir: Path) -> Dict[str, Any]:
    """Require answered + evidence-grounded clarifications when file exists.

    The planner writes clarifications as markdown with a stable shape:
      ## Q-001: ...
      _Answer:_ ...

    If clarifications.md exists, each question must have a non-empty answer and at least one
    AL file reference token (e.g. path/to/File.al) to prove code search grounding.
    """
    if not sdir.exists():
        return {
            "ran": False,
            "ok": False,
            "reason": "no spec folder (clarifications engine not run)",
            "detail": {"questions": 0, "validated": 0, "path": str(sdir / "clarifications.md"), "required": True},
        }

    clar_path = sdir / "clarifications.md"
    if not clar_path.exists():
        return {
            "ran": True,
            "ok": True,
            "reason": "",
            "detail": {"questions": 0, "validated": 0, "path": str(clar_path), "required": False},
        }

    try:
        lines = clar_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"ran": False, "ok": False, "reason": f"clarifications unreadable: {exc}"}

    issues: List[str] = []
    q_count = 0
    qid: str | None = None
    answer: str | None = None

    def _flush_question() -> None:
        nonlocal qid, answer, q_count
        if not qid:
            return
        q_count += 1
        text = (answer or "").strip()
        if not text:
            issues.append(f"{qid} unanswered")
            return
        if _UNSURE_RE.search(text):
            issues.append(f"{qid} answer is uncertain")
            return
        if not _AL_EVIDENCE_RE.search(text):
            issues.append(f"{qid} answer lacks AL file evidence")

    for line in lines:
        q_match = _QUESTION_RE.match(line.strip())
        if q_match:
            _flush_question()
            qid = q_match.group(1)
            answer = None
            continue
        if qid:
            a_match = _ANSWER_RE.match(line.strip())
            if a_match:
                answer = a_match.group(1)

    _flush_question()

    if issues:
        # Extract unanswered question IDs for the next_action hint
        unanswered = [i.split(" ")[0] for i in issues]
        return {
            "ran": True,
            "ok": False,
            "reason": "clarification enforcement failed: " + "; ".join(issues),
            "detail": {"questions": q_count, "issues": issues, "path": str(clar_path), "required": True},
            "next_action": {
                "tool": "bc_answer_clarification",
                "reason": "Answer the outstanding clarification questions through the MCP-fenced path. "
                          "Do NOT edit clarifications.md directly with generic file tools.",
                "params_hint": {
                    "spec_name": sdir.name,
                    "answers": {qid: "<answer text + src/Path/To/File.al evidence>" for qid in unanswered},
                },
            },
        }
    return {
        "ran": True,
        "ok": True,
        "reason": "",
        "detail": {"questions": q_count, "validated": q_count, "path": str(clar_path), "required": True},
    }


def _refinement_status(sdir: Path) -> Dict[str, Any]:
    """Heuristic+empiric understanding is MANDATORY: the item's claims must have been
    confronted with code reality (bc_refine_item) before anything is built on them.

    Deterministic rules:
    - missing item_refinement.json             -> not ran (blocked)
    - refinement older than captured context   -> stale (re-refine)
    - mismatches/conflicts with EMPTY critique -> blocked (facts found problems;
      a recorded judgment is required to proceed — silence is not acceptance)
    - otherwise ok
    """
    ref_path = sdir / "item_refinement.json"
    if not ref_path.exists():
        return {
            "ran": False,
            "ok": False,
            "reason": "no item refinement — claims were never confronted with code reality",
            "next_action": {
                "tool": "bc_refine_item",
                "reason": "Verify the item's claims (field ids, tables, redundancies) against the source",
                "params_hint": {"spec_name": sdir.name},
            },
        }
    try:
        data = json.loads(ref_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ran": False, "ok": False, "reason": f"item_refinement.json unreadable: {exc}"}

    manifest = sdir / "context" / "manifest.json"
    if manifest.exists():
        try:
            captured_at = str(json.loads(manifest.read_text(encoding="utf-8")).get("captured_at", ""))
            if captured_at and str(data.get("generated_at", "")) < captured_at:
                return {
                    "ran": True, "ok": False,
                    "reason": "refinement is stale — context was re-captured after it ran",
                    "next_action": {"tool": "bc_refine_item",
                                    "params_hint": {"spec_name": sdir.name},
                                    "reason": "Re-confront the fresh context with code reality"},
                }
        except (OSError, json.JSONDecodeError):
            pass

    counts = (data.get("findings") or {}).get("counts") or {}
    problems = int(counts.get("mismatches", 0)) + int(counts.get("conflicts", 0))
    critique = str(data.get("critique", "")).strip()
    if problems and not critique:
        return {
            "ran": True, "ok": False,
            "reason": (f"{problems} mismatch(es)/conflict(s) found and NO recorded judgment — "
                       "re-run bc_refine_item with `critique` addressing each finding"),
            "detail": counts,
            "next_action": {
                "tool": "bc_refine_item",
                "reason": "Facts found problems; record the first-principles judgment (critique)",
                "params_hint": {"spec_name": sdir.name,
                                "critique": "<address each mismatch/conflict explicitly>"},
            },
        }
    return {"ran": True, "ok": True, "reason": "",
            "detail": {**counts, "critique_recorded": bool(critique)}}


def _item_lane(sdir: Path) -> str:
    """Delivery lane from captured identity: 'bugfix' for Bug work items, else 'pbi'."""
    manifest = sdir / "context" / "manifest.json"
    try:
        identity = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("identity") or {}
    except (OSError, json.JSONDecodeError):
        return "pbi"
    explicit = str(identity.get("lane", "")).strip().lower()
    if explicit:
        return explicit
    return "bugfix" if str(identity.get("type", "")).strip().lower() == "bug" else "pbi"


def _root_cause_status(sdir: Path) -> Dict[str, Any]:
    """Bugfix lane only: the diagnosis engine. A bug's fix may not be planned before
    its root cause is recorded with VERIFIED code evidence (bc_root_cause) — the
    bug-lane twin of refinement. Non-bug lanes pass trivially (required=False).
    """
    if _item_lane(sdir) != "bugfix":
        return {"ran": True, "ok": True, "reason": "",
                "detail": {"required": False, "lane": "pbi"}}
    rc_path = sdir / "root_cause.json"
    if not rc_path.exists():
        return {
            "ran": False,
            "ok": False,
            "reason": "no root cause recorded — a bug may not be fixed before diagnosis (bc_root_cause)",
            "next_action": {
                "tool": "bc_root_cause",
                "reason": "Record symptom + root cause + verified code evidence for this bug",
                "params_hint": {"spec_name": sdir.name,
                                "symptom": "<observed wrong behavior>",
                                "root_cause": "<diagnosis grounded in code>",
                                "evidence": ["<path/to/File.al or 'table 11024121'>"],
                                "fix_approach": "<planned fix>"},
            },
        }
    try:
        data = json.loads(rc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ran": False, "ok": False, "reason": f"root_cause.json unreadable: {exc}"}
    manifest = sdir / "context" / "manifest.json"
    if manifest.exists():
        try:
            captured_at = str(json.loads(manifest.read_text(encoding="utf-8")).get("captured_at", ""))
            if captured_at and str(data.get("generated_at", "")) < captured_at:
                return {
                    "ran": True, "ok": False,
                    "reason": "root cause is stale — context was re-captured after it was recorded",
                    "next_action": {"tool": "bc_root_cause",
                                    "params_hint": {"spec_name": sdir.name},
                                    "reason": "Re-diagnose against the fresh context"},
                }
        except (OSError, json.JSONDecodeError):
            pass
    evidence = data.get("evidence") or []
    unverified = [e.get("ref") for e in evidence if not e.get("verified")]
    if not evidence or unverified:
        return {
            "ran": True, "ok": False,
            "reason": ("root cause has no code evidence" if not evidence
                       else f"root cause evidence unverified: {unverified[:3]}"),
            "next_action": {"tool": "bc_root_cause",
                            "params_hint": {"spec_name": sdir.name},
                            "reason": "Re-record the diagnosis with verifiable code evidence"},
        }
    return {"ran": True, "ok": True, "reason": "",
            "detail": {"required": True, "lane": "bugfix", "evidence": len(evidence)}}


def engine_status(project_root: Any, spec_name: str) -> Dict[str, Any]:
    """Per-engine {ran, ok, reason, next_action} + all_ok, computed deterministically from disk artifacts."""
    root = Path(str(project_root)).resolve()
    sdir = specs_root(root) / spec_name
    engines = {
        "timeline": _timeline_status(root, spec_name),
        "refinement": _refinement_status(sdir),
        "root_cause": _root_cause_status(sdir),
        "traceability": _traceability_status(sdir),
        "code_context": _code_context_status(sdir),
        "quality": _quality_status(sdir),
        "clarifications": _clarifications_status(sdir),
    }
    all_ok = all(engines[name]["ok"] for name in REQUIRED_ENGINES)
    blocking: List[str] = [
        f"{name}: {engines[name]['reason']}"
        for name in REQUIRED_ENGINES
        if not engines[name]["ok"]
    ]
    # Structured next_actions: one entry per blocking engine, ordered by REQUIRED_ENGINES priority.
    # Agents MUST call the named tool instead of using generic file/terminal tools.
    next_actions: List[Dict[str, Any]] = [
        {"engine": name, **engines[name]["next_action"]}
        for name in REQUIRED_ENGINES
        if not engines[name]["ok"] and "next_action" in engines[name]
    ]
    return {
        "spec_name": spec_name,
        "engines": engines,
        "all_ok": all_ok,
        "blocking": blocking,
        "next_actions": next_actions,
    }
