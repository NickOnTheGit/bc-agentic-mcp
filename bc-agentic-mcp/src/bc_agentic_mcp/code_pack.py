"""code_pack — ranked, signature-rendered code context (the Aider repo-map recipe, for AL).

Two deterministic pieces:
1. SIGNATURES: an object is rendered as its interface — header, decision-input
   properties (DataPerCompany, DataClassification, ...), field shapes, keys,
   procedure/trigger signatures — never full bodies. ~10x more context per token.
2. RANKING: personalized PageRank over the object reference graph, seeded by the
   item's VERIFIED target objects (from refinement). Importance is relative to
   THIS item, not global popularity. Fixed iterations + sorted tie-breaks = the
   same input always yields the same pack.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from bc_agentic_mcp import feature_refine

DEFAULT_CHAR_BUDGET = 12_000  # ~3k tokens of rendered signatures
_PAGERANK_ITERATIONS = 12
_DAMPING = 0.85

_PROC_RE = re.compile(
    r'^\s*((?:local\s+|internal\s+|protected\s+)?(?:procedure|trigger)\s+"?[A-Za-z0-9_]+"?\s*\([^)]*\)(?:\s*:?\s*[A-Za-z0-9_\[\]" ]+)?)',
    re.MULTILINE)
_KEY_RE = re.compile(r'^\s*(key\(\s*[A-Za-z0-9_]+\s*;[^)]+\))', re.MULTILINE)
_PROP_RE = re.compile(
    r'^\s*((?:DataPerCompany|DataClassification|TableType|LookupPageId|DrillDownPageId|'
    r'SourceTable|PageType|ApplicationArea|Extensible|ObsoleteState)\s*=\s*[^;]+);',
    re.MULTILINE)


def render_signature(entry: Dict[str, Any], *, max_lines: int = 60) -> str:
    """Compact interface view of one AL object (header, props, fields, keys, procedures).

    Fast path: rendered ENTIRELY from the index's precomputed ``detail`` (schema 3) —
    zero file I/O. Fallback (older cache entries): parse the source file once.
    """
    header = f"{entry['kind']} {entry['number']} {entry['name']}  [{entry.get('rel', entry.get('file', ''))}]"
    detail = entry.get("detail")
    if detail is not None:
        lines: List[str] = [header]
        if detail.get("caption"):
            lines.append(f"  Caption = '{detail['caption']}'")
        lines.extend(f"  {p}" for p in detail.get("props", []))
        for f in detail.get("fields", []) or []:
            marks = []
            if f.get("flowfield"):
                marks.append("FlowField")
            if not f.get("editable", True):
                marks.append("NotEditable")
            if f.get("has_relation"):
                marks.append("TableRelation")
            suffix = f"  [{', '.join(marks)}]" if marks else ""
            lines.append(f"  field({f['id']}; {f['name']}; {f['type']}){suffix}")
        lines.extend(f"  {k}" for k in detail.get("keys", []) or [])
        lines.extend(f"  {p}" for p in detail.get("procedures", []) or [])
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"  ... ({len(lines) - max_lines} more lines omitted)"]
        return "\n".join(lines) + "\n"

    # Fallback: no precomputed detail — read the source once.
    lines = [header]
    try:
        text = Path(entry["file"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return header + "\n  (source unreadable)\n"

    for m in _PROP_RE.finditer(text):
        lines.append(f"  {m.group(1).strip()}")

    if entry["kind"] in ("table", "tableextension"):
        for f in feature_refine.parse_table_fields(entry["file"]):
            marks = []
            if f["flowfield"]:
                marks.append("FlowField")
            if not f["editable"]:
                marks.append("NotEditable")
            if f["has_relation"]:
                marks.append("TableRelation")
            suffix = f"  [{', '.join(marks)}]" if marks else ""
            lines.append(f"  field({f['id']}; {f['name']}; {f['type']}){suffix}")
        for m in _KEY_RE.finditer(text):
            lines.append(f"  {m.group(1).strip()}")

    for m in _PROC_RE.finditer(text):
        lines.append(f"  {' '.join(m.group(1).split())}")

    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"  ... ({len(lines) - max_lines} more lines omitted)"]
    return "\n".join(lines) + "\n"


def pagerank(edges: Dict[str, Set[str]], seeds: List[str]) -> Dict[str, float]:
    """Personalized PageRank: probability mass restarts at the SEED objects.

    Deterministic: fixed iteration count, no randomness, stable arithmetic.
    """
    nodes = sorted(set(edges) | {t for ts in edges.values() for t in ts} | set(seeds))
    if not nodes:
        return {}
    seed_set = [s for s in seeds if s in nodes] or nodes
    personalization = {n: (1.0 / len(seed_set) if n in seed_set else 0.0) for n in nodes}
    scores = dict(personalization)
    for _ in range(_PAGERANK_ITERATIONS):
        nxt = {n: (1.0 - _DAMPING) * personalization[n] for n in nodes}
        for src in nodes:
            targets = edges.get(src) or set()
            if not targets:
                continue
            share = _DAMPING * scores[src] / len(targets)
            for t in targets:
                if t in nxt:
                    nxt[t] += share
        scores = nxt
    return scores


def ranked_pack(
    index_data: Dict[str, Any],
    seed_keys: List[str],
    *,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> Dict[str, Any]:
    """Budgeted, ranked, signature-rendered context pack.

    Seeds (the item's verified targets) are ALWAYS included first at full detail;
    the remaining budget fills with the highest personalized-PageRank neighbors.
    """
    objects: Dict[str, Dict[str, Any]] = index_data["objects"]
    edges = index_data.get("edges")
    if edges is None:
        from bc_agentic_mcp import object_index
        # memoized per cache generation — rebuilt only when the repo actually changed
        edges = object_index.edges_for(index_data.get("root", "."), index_data) \
            if index_data.get("root") else object_index.build_edges(index_data.get("files", {}), objects)

    scores = pagerank(edges, seed_keys)
    ordered = sorted(
        (k for k in scores if k in objects and " " in k),
        key=lambda k: (-scores[k], k),
    )

    sections: List[Dict[str, Any]] = []
    used = 0
    included: Set[str] = set()

    def _try_add(key: str, role: str) -> None:
        nonlocal used
        entry = objects.get(key)
        if entry is None or key in included:
            return
        sig = render_signature(entry)
        if used + len(sig) > char_budget and included:
            return
        included.add(key)
        used += len(sig)
        sections.append({"object": key, "role": role,
                         "score": round(scores.get(key, 0.0), 6), "signature": sig})

    for seed in seed_keys:
        _try_add(seed, "target")
    for key in ordered:
        if used >= char_budget:
            break
        _try_add(key, "ranked")

    return {
        "sections": sections,
        "rendered": "\n".join(s["signature"] for s in sections),
        "chars": used,
        "budget": char_budget,
        "graph_nodes": len({k for k in objects if " " in k}),
        "graph_edges": sum(len(v) for v in edges.values()),
    }


def persist_pack(spec_dir: Path, pack: Dict[str, Any]) -> str:
    """Write the rendered pack where code-context grounding picks it up."""
    out_dir = spec_dir / "context" / "code"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "context_pack.md"
    header = (
        "# Ranked code-context pack (signatures, not bodies)\n\n"
        f"_{len(pack['sections'])} objects · {pack['chars']}/{pack['budget']} chars · "
        f"graph {pack['graph_nodes']} nodes / {pack['graph_edges']} edges · "
        "seeded by the item's VERIFIED targets (personalized PageRank)._\n\n```\n"
    )
    path.write_text(header + pack["rendered"] + "```\n", encoding="utf-8")
    return str(path)
