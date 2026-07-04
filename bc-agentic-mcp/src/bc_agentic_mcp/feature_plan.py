"""feature_plan — deterministic cross-item analysis of a captured feature tree (H2).

Separation of powers (the paradigm): this module produces FACTS ONLY —
object references, cross-item mentions, shared-object collisions, a
foundation-first suggested order. The MODEL contributes judgment (final wave
narrative via ``notes``); the HUMAN approves the plan (C1 `plan` gate on the
feature folder). Nothing here guesses.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root

# AL object references: `table 11024121` / `page (11030034)` / bare 7-8 digit ids after keywords.
_OBJ_NUM_RE = re.compile(
    r"\b(table|page|codeunit|report|query|xmlport|enum)\s*\(?\s*(\d{5,8})\b", re.IGNORECASE)
# Zig/Empire object names: CamelCase ending in module suffixes.
_OBJ_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{4,}(?:FDN|HSG|CRM|FIN)\b")
_EXCLUDED_STATES = {"removed"}
_TERMINAL_STATES = {"done", "closed"}


def extract_object_refs(text: str) -> Dict[str, Any]:
    numbered = sorted({f"{m.group(1).lower()} {m.group(2)}" for m in _OBJ_NUM_RE.finditer(text or "")})
    names = sorted({m.group(0) for m in _OBJ_NAME_RE.finditer(text or "")})
    return {"numbered": numbered, "names": names}


def extract_item_refs(text: str, known_ids: List[str]) -> List[str]:
    """Sibling item ids mentioned in the text (only KNOWN ids count — no false positives)."""
    found = set(re.findall(r"\b(\d{6})\b", text or ""))
    return sorted(i for i in found if i in set(known_ids))


def analyze(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Pure, deterministic feature analysis. Same input -> same output, always."""
    children = tree.get("children", [])
    active = [c for c in children if str(c.get("state", "")).lower() not in _EXCLUDED_STATES]
    excluded = [{"id": c["id"], "state": c["state"], "title": c["title"][:80]}
                for c in children if str(c.get("state", "")).lower() in _EXCLUDED_STATES]
    known_ids = [str(c["id"]) for c in active]

    items: List[Dict[str, Any]] = []
    object_owners: Dict[str, List[str]] = {}
    mention_edges: List[Dict[str, str]] = []
    for c in active:
        cid = str(c["id"])
        text = f"{c.get('title', '')}\n{c.get('description', '')}"
        refs = extract_object_refs(text)
        for obj in refs["numbered"] + refs["names"]:
            object_owners.setdefault(obj, []).append(cid)
        mentions = [m for m in extract_item_refs(text, known_ids) if m != cid]
        for target in mentions:
            mention_edges.append({"from": cid, "to": target})
        items.append({
            "id": cid,
            "title": c.get("title", ""),
            "state": c.get("state", ""),
            "done": str(c.get("state", "")).lower() in _TERMINAL_STATES,
            "objects": refs["numbered"] + refs["names"],
            "mentions": mentions,
        })

    shared_objects = {obj: sorted(set(owners))
                      for obj, owners in sorted(object_owners.items())
                      if len(set(owners)) > 1}

    # Foundation-first suggestion: most-referenced items are likely providers.
    # HEURISTIC by design — the model finalizes waves in `notes`, the human approves.
    inbound: Dict[str, int] = {i["id"]: 0 for i in items}
    for edge in mention_edges:
        inbound[edge["to"]] = inbound.get(edge["to"], 0) + 1
    suggested = sorted(items, key=lambda i: (-inbound.get(i["id"], 0), int(i["id"])))

    return {
        "item_count": len(items),
        "items": items,
        "excluded": excluded,
        "shared_objects": shared_objects,
        "mention_edges": mention_edges,
        "suggested_order": [
            {"id": i["id"], "inbound_mentions": inbound.get(i["id"], 0),
             "state": i["state"], "title": i["title"][:80]}
            for i in suggested
        ],
        "collision_warnings": [
            f"{obj} is touched by {len(ids)} items ({', '.join(ids)}) — one owner or strict sequencing required"
            for obj, ids in shared_objects.items()
        ],
    }


def render_plan_md(feature: Dict[str, Any], analysis: Dict[str, Any],
                   notes: Optional[str] = None) -> str:
    lines = [
        f"# Feature Plan: {feature.get('title', '')} (#{feature.get('id')})",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()} from the captured tree — facts are",
        "deterministic; the wave narrative under 'Decisions & Waves' is model judgment; the",
        "approval of THIS file is the human gate._",
        "",
        f"## Items ({analysis['item_count']} active)",
    ]
    for entry in analysis["suggested_order"]:
        lines.append(f"- #{entry['id']} [{entry['state']}] {entry['title']}"
                     f" (referenced by {entry['inbound_mentions']} siblings)")
    if analysis["excluded"]:
        lines.append("")
        lines.append("## Excluded (state)")
        for e in analysis["excluded"]:
            lines.append(f"- #{e['id']} [{e['state']}] {e['title']}")
    lines += ["", "## Shared-object collisions"]
    if analysis["shared_objects"]:
        for warning in analysis["collision_warnings"]:
            lines.append(f"- {warning}")
    else:
        lines.append("- none detected")
    lines += ["", "## Cross-item mentions"]
    for edge in analysis["mention_edges"]:
        lines.append(f"- #{edge['from']} -> #{edge['to']}")
    lines += ["", "## Decisions & Waves (model judgment — reviewed at the plan gate)", ""]
    lines.append(notes.strip() if notes else "_(pending: model narrative via bc_plan_feature notes)_")
    return "\n".join(lines) + "\n"


def persist(root: str, spec_name: str, feature: Dict[str, Any],
            analysis: Dict[str, Any], notes: Optional[str]) -> Dict[str, str]:
    sdir = specs_root(Path(root).resolve()) / spec_name
    sdir.mkdir(parents=True, exist_ok=True)
    plan_json = sdir / "feature_plan.json"
    plan_md = sdir / "FEATURE-PLAN.md"
    plan_json.write_text(json.dumps({
        "feature": feature, "analysis": analysis, "notes": notes or "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    plan_md.write_text(render_plan_md(feature, analysis, notes), encoding="utf-8")
    return {"plan_json": str(plan_json), "plan_md": str(plan_md)}
