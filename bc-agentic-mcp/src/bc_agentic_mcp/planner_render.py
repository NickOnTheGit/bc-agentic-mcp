"""planner_render — fill externalized .md templates from extracted facts.

Keeps the planner's prose transparent and editable (in templates/*.md) instead of buried in
Python string literals. Deterministic {{key}} substitution + small markdown-table helpers.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

_TPL_DIR = Path(__file__).resolve().parent / "templates"


def load_template(name: str) -> str:
    path = _TPL_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def fill(template: str, context: Dict[str, Any]) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(context.get(m.group(1), "")), template)


def objects_table(objects: List[Dict[str, Any]]) -> str:
    if not objects:
        return "_No objects identified._"
    rows = ["| Kind | Name | Id | Action | Resolved file |",
            "|------|------|----|--------|----------------|"]
    for o in objects:
        oid = o.get("object_id") or o.get("id") or ""
        target = o.get("target") or o.get("declared_target") or ""
        if not target and o.get("resolved") is False:
            target = "UNRESOLVED: confirm target path"
        rows.append(f"| {o.get('kind','')} | {o.get('name') or '(to create)'} | {oid} | "
                    f"{o.get('action','')} | {target} |")
    return "\n".join(rows)


def fields_table(fields: List[Dict[str, Any]]) -> str:
    if not fields:
        return "_No new/changed fields._"
    rows = ["| Field | AL type | Editable |", "|-------|---------|----------|"]
    for f in fields:
        rows.append(f"| {f.get('name','')} | {f.get('al_type','')} | {f.get('editable', True)} |")
    return "\n".join(rows)


def render_tdd(context: Dict[str, Any]) -> str:
    return fill(load_template("tdd_general.md"), context)
