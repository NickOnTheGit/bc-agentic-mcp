"""bc_get_knowledge_article — read a BCQuality knowledge article in full.

This is the REQUIRED companion to the knowledge worklist returned by ``bc_review``
and ``bc_implement_context``.  The worklist is a discovery hint only; the normative
rule bodies (``## Best Practice``, ``## Anti Pattern``) and companion golden-template
files (``.good.al`` / ``.bad.al``) are NOT in the index.  Call this tool for EACH
listed article before submitting review findings.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def handle_get_knowledge_article(
    project_root: str,
    path: str,
    spec_name: Optional[str] = None,
    packet_id: str = "",
) -> Dict[str, Any]:
    """Return the full content of a BCQuality knowledge article + companion examples.

    ``path`` is the relative path from the knowledge worklist entry, e.g.
    ``performance/use-setloadfields-for-partial-records.md``.

    Returns::

        {
            "path": str,          # as requested
            "layer": str,         # microsoft | community | custom | repo
            "content": str,       # full markdown including Best Practice / Anti Pattern
            "companions": [       # golden-template AL files, empty when absent
                {"kind": "good"|"bad", "path": str, "content": str}
            ]
        }

    Raises a structured error dict (with key ``"error"``) when the article cannot be
    located — never raises an exception so the caller can handle it gracefully.
    """
    import os
    from bc_agentic_mcp import knowledge, security

    root = Path(project_root).resolve()

    # Use the knowledge index as the source of truth for absolute file paths.
    # The 'path' field in the index matches the worklist 'path' — no reconstruction needed.
    index = knowledge.load_knowledge_index(root)
    article_meta: Dict[str, str] = {}
    for art in (index.get("articles") or []):
        if art.get("path") == path:
            article_meta = art
            break

    if not article_meta:
        return {"error": f"article not found in index: {path}", "path": path}

    file_str = article_meta.get("file", "")
    if not file_str:
        return {"error": f"article has no file path in index: {path}", "path": path}

    candidate = Path(file_str)
    if not candidate.exists():
        return {"error": f"article file missing on disk: {file_str}", "path": path}

    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": f"could not read {path}: {exc}", "path": path}

    layer = str(article_meta.get("layer") or "repo")
    vroot = knowledge.vendor_root(root)

    # Companion example files: <slug>.good.al / <slug>.bad.al (golden templates)
    companions: List[Dict[str, str]] = []
    for kind, suffix in (("good", ".good.al"), ("bad", ".bad.al")):
        sibling = candidate.parent / (candidate.stem + suffix)
        if sibling.exists():
            try:
                sibling_rel = (
                    str(sibling.relative_to(vroot)).replace(os.sep, "/")
                    if vroot and sibling.is_relative_to(vroot)
                    else str(sibling)
                )
                companions.append({
                    "kind": kind,
                    "path": sibling_rel,
                    "content": sibling.read_text(encoding="utf-8", errors="replace"),
                })
            except OSError:
                pass

    result: Dict[str, Any] = {
        "path": path,
        "layer": layer,
        "content": content,
        "companions": companions,
    }
    if spec_name and packet_id:
        vendor_commit = knowledge.check_vendor_health(root).get("commit", "")
        receipt = security.issue_knowledge_read(
            project_root=root,
            spec_name=spec_name,
            packet_id=packet_id,
            path=path,
            article_sha256=security.digest_text(content),
            vendor_commit=vendor_commit,
        )
        reads_path = knowledge.specs_root(root) / spec_name / "knowledge_reads.jsonl"
        try:
            reads_path.parent.mkdir(parents=True, exist_ok=True)
            with reads_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"receipt": receipt, "path": path}) + "\n")
        except OSError as exc:
            return {
                "error": f"knowledge read receipt could not be persisted: {exc}",
                "path": path,
            }
        result["knowledge_receipt"] = receipt
        result["packet_id"] = packet_id
    else:
        result["receipt_required"] = True
    return result

