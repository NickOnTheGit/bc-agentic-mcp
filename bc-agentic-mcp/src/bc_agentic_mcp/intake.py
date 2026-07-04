"""intake — the refinement lab: raw material in, delivery-ready work item out.

Free-form input (pasted email, requirement notes, uploaded docs) becomes an
INTAKE workspace that the deterministic engines mine for evidence:

  * quarantine     — every pasted/uploaded doc is untrusted input (fenced + scanned)
  * precedents     — BM25 over every past/active spec's Charter + ITEM + diagnosis,
                     plus promoted lessons: "we built something like this before"
  * code reality   — object mentions resolved against the persistent object index:
                     what exists, where, how big
  * open questions — the ambiguity heuristics that already power bc_clarify
  * lane signals   — deterministic hints for bug / pbi / feature classification

The MODEL (bc-refiner agent) drives the conversation — asks the high-ROI
questions, proposes examples and options; THIS module only computes evidence
and materializes the outcome. Graduation hands the intake to the standard
lifecycle: bug (diagnosis-first), single PBI, or feature with child PBIs.
Epics deliberately stay a ROLL-UP view, not a lifecycle (ancestry capture
already records them; no gate exists at epic altitude).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import item_references, lessons, object_index, quarantine
from bc_agentic_mcp.workspace import specs_root

INTAKE_PREFIX = "intake-"
LANES = ("bug", "pbi", "feature")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BUG_RE = re.compile(
    r"\b(bug|defect|incorrect|wrong|broken|fails?|error|exception|crash|repro(duce)?|regression)\b",
    re.IGNORECASE)
_DELIVERABLE_RE = re.compile(r"^\s*[-*\d.]+\s*.*\b(add|create|extend|change|modify|implement|remove|show|hide|validate)\b",
                             re.IGNORECASE | re.MULTILINE)
_OBJECT_MENTION_RE = re.compile(
    r"\b(table|tableextension|page|pageextension|codeunit|enum|enumextension|report|query|xmlport)\s+"
    r"(\d{1,10}|[A-Z][A-Za-z0-9]{3,40})", re.IGNORECASE)
_CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){2,})\b")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def intake_name(name: str) -> str:
    return name if name.startswith(INTAKE_PREFIX) else f"{INTAKE_PREFIX}{name}"


def intake_dir(root: Path, name: str) -> Path:
    return specs_root(root) / intake_name(name)


def _strip_fences(text: str) -> str:
    return "\n".join(
        line for line in str(text or "").splitlines()
        if not line.startswith("<<<UNTRUSTED-CONTENT") and not line.startswith("<<<END-UNTRUSTED")
    )


def _read_sources(idir: Path) -> str:
    src = idir / "sources"
    if not src.is_dir():
        return ""
    return "\n\n".join(
        _strip_fences(p.read_text(encoding="utf-8", errors="replace"))
        for p in sorted(src.glob("*")) if p.is_file()
    )


def _save_doc(idir: Path, filename: str, content: str) -> Dict[str, Any]:
    src = idir / "sources"
    src.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", filename or "doc.md")[:80] or "doc.md"
    path = src / safe
    counter = 1
    while path.exists():
        path = src / f"{counter}-{safe}"
        counter += 1
    q = quarantine.apply(content, f"intake-upload {safe}")
    path.write_text(q["text"], encoding="utf-8")
    return {"file": f"sources/{path.name}", "chars": len(content or ""),
            "quarantine_risk": q["risk"],
            "flags": [f["flag"] for f in q["flags"]]}


# ---------------------------------------------------------------- precedents
def _spec_corpus(root: Path, exclude: str) -> List[Dict[str, str]]:
    """Every sibling spec folder as one searchable document (charter + item + diagnosis)."""
    base = specs_root(root)
    corpus: List[Dict[str, str]] = []
    if not base.is_dir():
        return corpus
    for child in sorted(base.iterdir()):
        if (not child.is_dir() or child.name.startswith(".")
                or child.name == exclude or child.name.startswith(INTAKE_PREFIX)):
            continue
        parts: List[str] = []
        charter = child / "charter.json"
        if charter.exists():
            try:
                data = json.loads(charter.read_text(encoding="utf-8"))
                parts.append(str(data.get("purpose", "")))
                parts.extend(str(c) for c in data.get("acceptance_criteria") or [])
            except (OSError, json.JSONDecodeError):
                pass
        for extra, cap in (("ITEM.md", 600), ("root_cause.json", 800)):
            path = child / extra
            if path.exists():
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="replace")[:cap])
                except OSError:
                    pass
        text = " ".join(p for p in parts if p).strip()
        if text:
            corpus.append({"spec": child.name, "text": text})
    return corpus


def find_precedents(root: Path, query: str, exclude: str, limit: int = 5) -> List[Dict[str, Any]]:
    corpus = _spec_corpus(root, exclude)
    if not corpus:
        return []
    scores = lessons.bm25_scores(query, [c["text"] for c in corpus])
    top = max(scores) if scores else 0.0
    ranked = sorted(zip(corpus, scores), key=lambda pair: pair[1], reverse=True)
    return [
        {"spec": c["spec"], "score": round(s / top, 3) if top else 0.0,
         "preview": c["text"][:220]}
        for c, s in ranked[:limit] if s > 0
    ]


# ---------------------------------------------------------------- code reality
def resolve_code_mentions(root: Path, text: str, limit: int = 12) -> List[Dict[str, Any]]:
    """Resolve object mentions in the intake text against the persistent object index."""
    try:
        index = object_index.refresh(root)
        objects = index.get("objects") or {}
    except Exception:
        return []
    queries: List[str] = []
    for kind, ident in _OBJECT_MENTION_RE.findall(text):
        queries.append(ident)
    queries.extend(_CAMEL_RE.findall(text))
    seen: set = set()
    hits: List[Dict[str, Any]] = []
    for q in queries:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        for row in object_index.toc_search(objects, q, limit=1):
            hits.append({"mention": q, **row})
            break
        if len(hits) >= limit:
            break
    return hits


# ---------------------------------------------------------------- lane signals
def lane_signals(text: str) -> Dict[str, Any]:
    bug_hits = _BUG_RE.findall(text)
    deliverables = _DELIVERABLE_RE.findall(text)
    objects = {m[1].lower() for m in _OBJECT_MENTION_RE.findall(text)}
    if bug_hits and len(deliverables) <= 3:
        suggestion = "bug"
    elif len(deliverables) >= 6 or len(objects) >= 4:
        suggestion = "feature"
    else:
        suggestion = "pbi"
    return {
        "suggested_lane": suggestion,
        "bug_signals": len(bug_hits),
        "deliverable_bullets": len(deliverables),
        "distinct_objects": len(objects),
        "note": ("Deterministic HINT only — the refiner agent + human make the call. "
                 "Epic-sized input should be split into features first (epics are a "
                 "roll-up, not a lifecycle)."),
    }


# ---------------------------------------------------------------- dossier
def build_dossier(root: Path, name: str) -> Dict[str, Any]:
    from bc_agentic_mcp.tools.clarify import _detect_ambiguities
    idir = intake_dir(root, name)
    text = _read_sources(idir)
    if not text.strip():
        return {"status": "blocked_empty", "blocked": True,
                "reason": "No source material yet — bc_intake_add a document first."}
    refs = item_references.extract_references(text)
    dossier = {
        "intake": intake_name(name),
        "generated_at": _now(),
        "source_chars": len(text),
        "references": refs,
        "open_questions": _detect_ambiguities(text),
        "precedents": find_precedents(root, text, exclude=intake_name(name)),
        "code_reality": resolve_code_mentions(root, text),
        "lane": lane_signals(text),
    }
    (idir / "dossier.json").write_text(json.dumps(dossier, indent=2), encoding="utf-8")
    (idir / "DOSSIER.md").write_text(_render_dossier_md(dossier), encoding="utf-8")
    return dossier


def _render_dossier_md(d: Dict[str, Any]) -> str:
    lines = [
        f"# Refinement dossier: {d['intake']}",
        "",
        f"_Generated {d['generated_at']} from {d['source_chars']} chars of source material._",
        "",
        f"## Lane suggestion: **{d['lane']['suggested_lane']}**",
        f"- bug signals: {d['lane']['bug_signals']} · deliverable bullets: "
        f"{d['lane']['deliverable_bullets']} · distinct objects: {d['lane']['distinct_objects']}",
        "",
        "## Similar past work (BM25 over every spec's charter/item/diagnosis)",
    ]
    for p in d["precedents"] or []:
        lines.append(f"- **{p['spec']}** (score {p['score']}): {p['preview'][:160]}…")
    if not d["precedents"]:
        lines.append("- none found")
    lines += ["", "## Code reality (object index)"]
    for h in d["code_reality"] or []:
        lines.append(f"- `{h['mention']}` → {h['object']} — {h['file']} "
                     f"({h['fields']} fields, {h['procedures']} procedures)")
    if not d["code_reality"]:
        lines.append("- no object mentions resolved")
    lines += ["", "## Open questions (answer these before graduation)"]
    for q in d["open_questions"] or []:
        lines.append(f"- **{q['id']}**: {q['question']}")
    lines += ["", "## Referenced artifacts",
              f"- wiki links: {len(d['references'].get('wiki_links', []))}"
              f" · related work items: {len(d['references'].get('related_work_items', []))}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- handlers
def handle_intake_start(project_root: str, intake_name_arg: Optional[str] = None,
                        text: Optional[str] = None, source: str = "pasted",
                        **legacy: Any) -> Dict[str, Any]:
    """Open a refinement-lab workspace; optionally seed it with the first document."""
    name = str(intake_name_arg or legacy.get("name") or "")
    if not _NAME_RE.match(name):
        return {"status": "error", "reason": "name must be alphanumeric/dash/underscore."}
    root = Path(project_root).resolve()
    idir = intake_dir(root, name)
    idir.mkdir(parents=True, exist_ok=True)
    docs: List[Dict[str, Any]] = []
    if text and text.strip():
        docs.append(_save_doc(idir, f"{source}.md", text))
    manifest_path = idir / "sources.json"
    existing = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = []
    existing.extend(docs)
    manifest_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return {"status": "intake_started", "intake": intake_name(name),
            "dir": str(idir), "documents": existing,
            "next_action": {"tool": "bc_intake_analyze",
                            "reason": "Mine the material for precedents, code reality and questions.",
                            "params_hint": {"name": intake_name(name)}}}


def handle_intake_add(project_root: str, intake_name_arg: Optional[str] = None,
                      filename: str = "doc.md", content: str = "",
                      **legacy: Any) -> Dict[str, Any]:
    """Add one pasted/uploaded document to the intake (quarantined like all untrusted input)."""
    name = str(intake_name_arg or legacy.get("name") or "")
    root = Path(project_root).resolve()
    idir = intake_dir(root, name)
    if not idir.is_dir():
        return {"status": "error", "reason": "intake not found — bc_intake_start first."}
    if not str(content or "").strip():
        return {"status": "error", "reason": "content is empty."}
    doc = _save_doc(idir, filename, content)
    manifest_path = idir / "sources.json"
    docs = []
    if manifest_path.exists():
        try:
            docs = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            docs = []
    docs.append(doc)
    manifest_path.write_text(json.dumps(docs, indent=2), encoding="utf-8")
    return {"status": "document_added", **doc, "documents": len(docs)}


def handle_intake_analyze(project_root: str, intake_name_arg: Optional[str] = None,
                          **legacy: Any) -> Dict[str, Any]:
    """Rebuild the evidence dossier (precedents, code reality, questions, lane hint)."""
    name = str(intake_name_arg or legacy.get("name") or "")
    root = Path(project_root).resolve()
    if not intake_dir(root, name).is_dir():
        return {"status": "error", "reason": "intake not found — bc_intake_start first."}
    dossier = build_dossier(root, name)
    if dossier.get("blocked"):
        return dossier
    return {"status": "intake_analyzed", "dossier_path": str(intake_dir(root, name) / "DOSSIER.md"),
            **{k: dossier[k] for k in ("precedents", "code_reality", "open_questions", "lane")},
            "next_action": {
                "tool": "bc_intake_graduate",
                "reason": "Once the open questions are answered with the human, graduate "
                          "into the delivery lane (bug / pbi / feature).",
                "params_hint": {"name": intake_name(name), "lane": dossier["lane"]["suggested_lane"],
                                "spec_name": "<wiNNNNN-slug>"}}}


def handle_intake_graduate(project_root: str, intake_name_arg: Optional[str] = None,
                           lane: str = "", spec_name: str = "",
                           work_item_id: Optional[str] = None,
                           children: Optional[List[str]] = None,
                           **legacy: Any) -> Dict[str, Any]:
    """Materialize the refined intake as a real lifecycle item (bug/pbi/feature)."""
    name = str(intake_name_arg or legacy.get("name") or "")
    # The MCP pipeline reserves spec_name for the INTAKE folder (timeline routing);
    # the graduation TARGET arrives as spec_name_target.
    spec_name = str(legacy.get("spec_name_target") or spec_name or "")
    root = Path(project_root).resolve()
    idir = intake_dir(root, name)
    if not idir.is_dir():
        return {"status": "error", "reason": "intake not found."}
    if lane not in LANES:
        return {"status": "error",
                "reason": f"lane must be one of {LANES} — epics are a roll-up, not a "
                          "lifecycle: split epic-sized work into features first."}
    if not _NAME_RE.match(spec_name or "") or spec_name.startswith(INTAKE_PREFIX):
        return {"status": "error", "reason": "invalid spec_name."}
    if not (idir / "dossier.json").exists():
        return {"status": "blocked", "reason": "analyze before graduating (bc_intake_analyze)."}

    target = specs_root(root) / spec_name
    ctx = target / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    copied = []
    for src_file in sorted((idir / "sources").glob("*")):
        dest = ctx / f"intake-{src_file.name}"
        dest.write_text(src_file.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        copied.append(dest.name)
    for extra in ("DOSSIER.md", "dossier.json"):
        src_path = idir / extra
        if src_path.exists():
            (target / extra).write_text(src_path.read_text(encoding="utf-8", errors="replace"),
                                        encoding="utf-8")
    identity: Dict[str, Any] = {"source": "intake", "type": lane}
    if lane == "bug":
        identity["lane"] = "bugfix"
    manifest = {
        "spec_name": spec_name,
        "item_id": str(work_item_id or ""),
        "identity": identity,
        "captured_at": _now(),
        "files": [{"kind": "intake", "path": f"intake-{n}", "source": "refinement-lab"} for n in copied],
        "unresolved": [] if work_item_id else [
            {"kind": "ado", "reason": "no work item id yet — create it in ADO, then "
                                      "bc_capture_item_context to fetch the official record"}],
        "complete": bool(work_item_id),
    }
    (ctx / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    graduation = {"graduated_at": _now(), "lane": lane, "spec_name": spec_name,
                  "work_item_id": work_item_id, "children": children or []}
    (idir / "graduation.json").write_text(json.dumps(graduation, indent=2), encoding="utf-8")

    if lane == "bug":
        next_action = {"tool": "bc_root_cause",
                       "reason": "Bug lane: record the verified diagnosis before any planning.",
                       "params_hint": {"spec_name": spec_name}}
    elif lane == "feature":
        next_action = ({"tool": "bc_capture_feature",
                        "reason": "Feature lane: capture the ADO feature tree fresh.",
                        "params_hint": {"spec_name": spec_name, "work_item_id": work_item_id}}
                       if work_item_id else
                       {"tool": "bc_intake_graduate",
                        "reason": "Create the Feature + child PBIs in ADO first (children "
                                  "suggestions recorded), then re-graduate with work_item_id "
                                  "or run bc_capture_feature directly.",
                        "params_hint": {"name": intake_name(name)}})
    else:
        next_action = ({"tool": "bc_capture_item_context",
                        "reason": "Fetch the official ADO record fresh over the pasted material.",
                        "params_hint": {"spec_name": spec_name, "work_item_id": work_item_id}}
                       if work_item_id else
                       {"tool": "bc_prepare_review",
                        "reason": "No ADO id yet — the intake material is the requirement "
                                  "source; plan from it and sync the id later.",
                        "params_hint": {"spec_name": spec_name}})
    return {"status": "intake_graduated", "lane": lane, "spec_name": spec_name,
            "target_dir": str(target), "copied_documents": copied,
            "children_suggested": children or [], "next_action": next_action}
