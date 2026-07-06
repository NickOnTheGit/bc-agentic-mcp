"""detectors — deterministic mistake DETECTION that auto-triggers reflection (Layer 1).

Closes the *detect* gap. The reflection loop (reflection.py) only starts once a
reflectable checkpoint exists — and recording that depended on a human/agent noticing the
mistake. These detectors run automatically (wired into ``bc_quality_check`` and exposed as
``bc_detect``) and append a ``mistake``/``correction`` checkpoint the moment a codifiable
rule trips, which the server's existing reflection nudge then surfaces until ``bc_reflect``.

Each detector is a pure function; a *finding* = ``{detector, code, kind, severity, summary}``.
Findings are de-duplicated by ``detector`` id within the current (un-reflected) window so the
same issue is not re-checkpointed on every call.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import al_validator
from bc_agentic_mcp import authorization
from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import verification

Finding = Dict[str, Any]


# ---------------------------------------------------------------------------
# Individual detectors (pure)
# ---------------------------------------------------------------------------

def _detect_upgrade_scope(diagnostics: List[Dict[str, Any]]) -> List[Finding]:
    """Upgrade scope vs table DataPerCompany mismatch (al_validator V0100/V0101)."""
    hits = [d for d in diagnostics if d.get("code") in ("V0100", "V0101")]
    if not hits:
        return []
    files = sorted({d.get("sourceLocation", {}).get("file", "") for d in hits})
    return [{
        "detector": "upgrade_scope",
        "code": hits[0].get("code"),
        "kind": "mistake",
        "severity": "warning",
        "summary": (f"Upgrade scope does not match the table's DataPerCompany "
                    f"({len(hits)} finding(s)): {', '.join(files[:3])}"),
    }]


def _detect_unapproved_implementation(project_root: Path, spec_name: str) -> List[Finding]:
    """Implementation started without an approved decision (the process-bypass class)."""
    if authorization.implementation_authorized(project_root, spec_name):
        return []
    started = False
    tasks = specs_root(project_root) / spec_name / "TASKS.md"
    if tasks.exists() and re.search(r"(?m)^\s*-\s*\[x\]", tasks.read_text(encoding="utf-8", errors="replace")):
        started = True
    if not started:
        for cp in memory.load_checkpoints(project_root, spec_name):
            summary = (cp.get("summary") or "").lower()
            if cp.get("kind") == "milestone" or "implement" in summary:
                started = True
                break
    if not started:
        return []
    return [{
        "detector": "unapproved_implementation",
        "code": "GATE",
        "kind": "mistake",
        "severity": "error",
        "summary": ("Implementation appears to have started without an approved decision "
                    "(no tasks/implement/complete approval). Route writes through bc_implement "
                    "after human approval."),
    }]


def _detect_stale_spec(project_root: Path, spec_name: str) -> List[Finding]:
    """The generated spec is stale vs the current planner version or the item context bundle."""
    spec_path = specs_root(project_root) / spec_name / "spec.json"
    if not spec_path.exists():
        return []
    try:
        spec_json = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    from bc_agentic_mcp import item_context, provenance
    ctx = item_context.context_source(str(project_root), spec_name)
    reason = provenance.staleness(spec_json, ctx["sha"] if ctx else None)
    if not reason:
        return []
    return [{
        "detector": "stale_spec", "code": "STALE", "kind": "correction", "severity": "warning",
        "summary": f"Generated spec is stale: {reason}. Regenerate the plan (bc_prepare_review) "
                   f"before relying on REVIEW/DESIGN/RATIONALE.",
    }]


def _detect_stale_code_context(project_root: Path, spec_name: str) -> List[Finding]:
    """Code read-context was built against a now-changed repo (files added/removed)."""
    cc_path = specs_root(project_root) / spec_name / "context" / "code" / "code_context.json"
    if not cc_path.exists():
        return []
    try:
        cc = json.loads(cc_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    stored = cc.get("repo_index_sha")
    if not stored:
        return []
    from bc_agentic_mcp import object_resolver, code_context
    try:
        object_resolver.clear_cache()
        idx = object_resolver._filename_index(Path(project_root).resolve())
        current = code_context.repo_index_sha(idx)
    except Exception:
        return []
    if current == stored:
        return []
    return [{
        "detector": "stale_code_context", "code": "STALE_CODE", "kind": "correction",
        "severity": "warning",
        "summary": "Code read-context is stale (repo files changed since it was built). "
                   "Re-run bc_read_code_context before relying on precedents.",
    }]


def _detect_evidence_gap(project_root: Path, spec_name: str) -> List[Finding]:
    """A mutation/high-tier item whose criteria lack evidence at the required tier."""
    charter = memory.load_charter(project_root, spec_name)
    if not charter:
        return []
    try:
        digest = verification.build_verification(project_root, spec_name)
    except Exception:
        return []
    gaps = digest.get("evidence_gaps") or []
    if not gaps:
        return []
    needs = bool((charter.get("operations") or {}).get("update")) or digest.get("required_strength", 1) >= 3
    if not needs:
        return []
    return [{
        "detector": "evidence_gap",
        "code": "EVID",
        "kind": "mistake",
        "severity": "warning",
        "summary": (f"{len(gaps)} acceptance criterion(s) lack evidence at the required tier "
                    f"('{digest.get('required_strength_label')}')."),
    }]


# ---------------------------------------------------------------------------
# Aggregation + auto-recording
# ---------------------------------------------------------------------------

_ACTIVATE_RE = re.compile(r"(\w+)\.ActivateFeature\(\)")


def _detect_unpaired_bootstrap(project_root: Path, spec_name: str) -> List[Finding]:
    """Fresh-container hazard: <lib>.ActivateFeature() without the sibling-consensus
    bootstrap <lib>.CreateFeatureIfNotFound() in the same file.

    Pipeline build 257447 failed EXACTLY here: the long-lived dev container carried
    the FeatureSAN row from history so the hard Get() inside ActivateFeature worked
    locally — the pipeline's FRESH container had no row and both page tests died.
    The pairing convention is MINED from sibling files (same folder), never hardcoded:
    the finding fires only when ≥2 siblings pair the calls and the majority agrees.
    """
    spec_path = specs_root(project_root) / spec_name / "spec.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    allowed = ((spec.get("scope_boundaries") or {}).get("allowed_files") or [])
    findings: List[Finding] = []
    for rel in allowed:
        p = (project_root / str(rel)).resolve()
        if not p.exists() or not p.name.lower().endswith(".al"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        activators = set(_ACTIVATE_RE.findall(text))
        unpaired = [v for v in activators if f"{v}.CreateFeatureIfNotFound()" not in text]
        if not unpaired:
            continue
        paired_siblings = bare_siblings = 0
        for sib in sorted(p.parent.glob("*.al")):
            if sib == p:
                continue
            try:
                stext = sib.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sacts = set(_ACTIVATE_RE.findall(stext))
            if not sacts:
                continue
            if all(f"{v}.CreateFeatureIfNotFound()" in stext for v in sacts):
                paired_siblings += 1
            else:
                bare_siblings += 1
        if paired_siblings >= 2 and paired_siblings > bare_siblings:
            findings.append({
                "detector": "unpaired_bootstrap",
                "code": "FRESH-ENV",
                "kind": "mistake",
                "severity": "warning",
                "summary": (
                    f"{p.name} calls {', '.join(sorted(unpaired))}.ActivateFeature() without "
                    f"CreateFeatureIfNotFound() — {paired_siblings} sibling suite(s) pair these calls. "
                    "A fresh container has no FeatureSAN row: the hard Get() inside "
                    "ActivateFeature will fail in the pipeline while long-lived local "
                    "containers mask it (observed live: build 257447)."
                ),
            })
    return findings

def _spec_allowed_al_files(project_root: Path, spec_name: str) -> List[Path]:
    """The spec's in-scope .al files that exist on disk (shared by file-level detectors)."""
    spec_path = specs_root(project_root) / spec_name / "spec.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    allowed = ((spec.get("scope_boundaries") or {}).get("allowed_files") or [])
    out: List[Path] = []
    for rel in allowed:
        p = (project_root / str(rel)).resolve()
        if p.exists() and p.name.lower().endswith(".al"):
            out.append(p)
    return out


def _detect_crlf_corruption(project_root: Path, spec_name: str) -> List[Finding]:
    """Line-ending corruption: \\r\\r\\n in an in-scope file (PR 41670 review, 2026-07-06).

    CRLF content written through a newline-translating layer becomes \\r\\r\\n on every
    line: ADO renders phantom blank lines (review noise: 'too many spaces') and an
    untouched file shows as fully rewritten (91+/91-), hiding the real diff. The write
    path now normalizes — this detector catches PRE-EXISTING corruption in scope.
    """
    findings: List[Finding] = []
    for p in _spec_allowed_al_files(project_root, spec_name):
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        n = raw.count(b"\r\r\n")
        if n:
            findings.append({
                "detector": "crlf_corruption",
                "code": "CRLF-DOUBLE",
                "kind": "mistake",
                "severity": "error",
                "summary": (
                    f"{p.name} has {n} corrupted line ending(s) (\\r\\r\\n): the diff shows "
                    "phantom blank lines and the file appears fully rewritten. Rewrite the "
                    "file with clean CRLF (bc_implement_write normalizes) — or restore it "
                    "from master if it carries no semantic change."
                ),
            })
    return findings


_VAR_RECORD_PARAM_RE = re.compile(
    r"procedure\s+\w+\s*\(([^)]*\bvar\s+(\w+)\s*:\s*Record\b[^)]*)\)", re.IGNORECASE)


def _detect_escaping_partial_record(project_root: Path, spec_name: str) -> List[Finding]:
    """A `var Record` out-parameter that gets SetLoadFields inside the same procedure:
    a PARTIAL record escaping the scope that knows what was loaded (reviewer lesson,
    PR 41674 thread 316421). Any future caller reading an unloaded field hits the
    partial-record trap, and the record carries invisible filters. Expose the
    DECISION, not the record.
    """
    findings: List[Finding] = []
    for p in _spec_allowed_al_files(project_root, spec_name):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Split into procedure bodies (next 'procedure' keyword bounds the body).
        for m in _VAR_RECORD_PARAM_RE.finditer(text):
            var_name = m.group(2)
            body_start = m.end()
            nxt = text.find("procedure", body_start)
            body = text[body_start:nxt if nxt != -1 else len(text)]
            if re.search(rf"\b{re.escape(var_name)}\s*\.\s*SetLoadFields\s*\(", body, re.IGNORECASE):
                findings.append({
                    "detector": "escaping_partial_record",
                    "code": "VAR-PARTIAL-REC",
                    "kind": "mistake",
                    "severity": "warning",
                    "summary": (
                        f"{p.name}: '{var_name}' is a var Record OUT-parameter that gets "
                        "SetLoadFields inside the procedure — a partial record (with live "
                        "filters) escapes the scope that knows what was loaded. Keep the "
                        "record local and return the DECISION instead "
                        "(reviewer lesson, PR 41674)."
                    ),
                })
    return findings


def detect(
    project_root: Path,
    spec_name: str,
    diagnostics: Optional[List[Dict[str, Any]]] = None,
) -> List[Finding]:
    """Run all detectors and return findings (deterministic)."""
    root = Path(project_root).resolve()
    if diagnostics is None:
        try:
            diagnostics = al_validator.validate_project(root)
        except Exception:
            diagnostics = []
    findings: List[Finding] = []
    findings += _detect_upgrade_scope(diagnostics)
    findings += _detect_unapproved_implementation(root, spec_name)
    findings += _detect_stale_spec(root, spec_name)
    findings += _detect_stale_code_context(root, spec_name)
    findings += _detect_evidence_gap(root, spec_name)
    findings += _detect_unpaired_bootstrap(root, spec_name)
    findings += _detect_crlf_corruption(root, spec_name)
    findings += _detect_escaping_partial_record(root, spec_name)
    return findings


def scan_and_record(
    project_root: Path,
    spec_name: str,
    diagnostics: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Detect mistakes and auto-append checkpoints for new ones (triggers reflection).

    Only runs for a real spec (one with a Charter), so ad-hoc/test projects are untouched.
    De-dupes by detector id within the current un-reflected window.
    """
    root = Path(project_root).resolve()
    if not memory.load_charter(root, spec_name):
        return {"findings": [], "recorded": 0, "skipped": "no-charter"}

    findings = detect(root, spec_name, diagnostics=diagnostics)

    cps = memory.load_checkpoints(root, spec_name)
    last_reflection = -1
    for i, cp in enumerate(cps):
        if cp.get("kind") == "reflection":
            last_reflection = i
    already = {
        (cp.get("details") or {}).get("detector")
        for cp in cps[last_reflection + 1:]
    }

    recorded = 0
    for finding in findings:
        if finding["detector"] in already:
            continue
        memory.append_checkpoint(
            root, spec_name,
            kind=finding.get("kind", "mistake"),
            summary=f"[auto-detected] {finding['summary']}",
            details={
                "detector": finding["detector"],
                "code": finding.get("code"),
                "severity": finding.get("severity"),
                "auto": True,
            },
        )
        recorded += 1
    return {"findings": findings, "recorded": recorded}


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def handle_detect(project_root: str, spec_name: str) -> Dict[str, Any]:
    """bc_detect: run deterministic mistake detectors; auto-record + trigger reflection."""
    result = scan_and_record(Path(project_root).resolve(), spec_name)
    return {
        "spec_name": spec_name,
        "findings": result["findings"],
        "checkpoints_recorded": result["recorded"],
        "note": ("Recorded findings become 'mistake' checkpoints; the server will nudge "
                 "reflection_due until you call bc_reflect."),
    }
