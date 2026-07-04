"""feature_refine — heuristic + empiric feature understanding (H4).

Confronts SPEC-WORLD (what the PBIs claim) with CODE-WORLD (what the AL source
actually is) BEFORE any spec is written. Deterministic outputs only:

- verified:      claims that match code reality (field free, target exists, ...)
- mismatches:    things that DO NOT ADD UP (cited field id wrong/missing, id collision)
- redundancies:  proposed artifacts duplicating existing mechanisms
- conflicts:     two sibling PBIs claiming the same artifact
- guideline_flags: data-model rules (name length, captions, editability, upgrade needs)

The MODEL then writes the first-principles critique on top of these facts; the HUMAN
approves at gate F1. Empiric deep-dive (deployed schema / container) stays per item
via bc_reconcile_target — this module flags WHERE it is required.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root

_SKIP_DIRS = {".git", ".vscode", ".alpackages", ".specs", "node_modules", "out", "build"}
_HEADER_RE = re.compile(
    r'^\s*(tableextension|table|pageextension|page|codeunit|reportextension|report|'
    r'enumextension|enum|query|xmlport|interface)\s+(\d+)\s+("[^"]+"|[A-Za-z0-9_]+)',
    re.IGNORECASE | re.MULTILINE)
_FIELD_RE = re.compile(r'field\(\s*(\d+)\s*;\s*"?([A-Za-z0-9_ ]+?)"?\s*;\s*([^);]+)\)')
_MAX_FIELD_NAME = 30  # AL0468


# ---------------------------------------------------------------------------
# Code-world: object index + table shape
# ---------------------------------------------------------------------------

def build_object_index(project_root: Path, *, subdir: str = "extensions") -> Dict[str, Dict[str, Any]]:
    """Index every AL object header in the repo: number & name -> {kind, file}.

    Header-only scan (first 2KB per file) so a 10k-file repo stays fast. Deterministic
    ordering; later duplicates do not overwrite earlier ones (first wins, stable walk).
    """
    index: Dict[str, Dict[str, Any]] = {}
    base = Path(project_root) / subdir
    if not base.is_dir():
        base = Path(project_root)
    for path in sorted(base.rglob("*.al")):
        if any(part.lower() in _SKIP_DIRS for part in path.parts):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2048]
        except OSError:
            continue
        m = _HEADER_RE.search(head)
        if not m:
            continue
        kind, number, name = m.group(1).lower(), m.group(2), m.group(3).strip('"')
        entry = {"kind": kind, "number": number, "name": name, "file": str(path)}
        index.setdefault(f"{kind} {number}", entry)
        index.setdefault(name.lower(), entry)
    return index


def parse_table_fields(file_path: str) -> List[Dict[str, Any]]:
    """Real field shapes of a table/tableextension: id, name, type, flowfield, editable, relation."""
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    fields: List[Dict[str, Any]] = []
    for m in _FIELD_RE.finditer(text):
        start = m.end()
        depth = 0
        block_start = text.find("{", start)
        pos = block_start
        while pos != -1 and pos < len(text):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
                if depth == 0:
                    break
            pos += 1
        block = text[block_start:pos + 1] if block_start != -1 else ""
        fields.append({
            "id": int(m.group(1)),
            "name": m.group(2).strip(),
            "type": m.group(3).strip(),
            "flowfield": bool(re.search(r"FieldClass\s*=\s*FlowField", block, re.IGNORECASE)),
            "editable": not re.search(r"Editable\s*=\s*false", block, re.IGNORECASE),
            "has_relation": bool(re.search(r"TableRelation\s*=", block, re.IGNORECASE)),
            "calc_formula": bool(re.search(r"CalcFormula\s*=", block, re.IGNORECASE)),
        })
    return fields


# ---------------------------------------------------------------------------
# Spec-world: claims extraction from PBI text (deterministic patterns)
# ---------------------------------------------------------------------------

_TABLE_CTX_RE = re.compile(r"table\s+(?:'?\"?)([A-Za-z0-9_]+)(?:'?\"?)?\s*\(\s*(\d{5,8})\s*\)", re.IGNORECASE)
_NEW_FIELD_RE = re.compile(r"Field\s+(\d{1,4})\s*[:\-]\s*([A-Za-z0-9_]+)", re.IGNORECASE)
_CITED_FIELD_RE = re.compile(r"field\s+'([A-Za-z0-9_ ]+)'\s*\(\s*(\d{1,4})\s*\)", re.IGNORECASE)


def extract_claims(item_id: str, text: str) -> Dict[str, Any]:
    """What this PBI claims: tables it touches, NEW fields it introduces, EXISTING fields it cites.

    All three lists are DE-DUPLICATED (a PBI repeating a table five times is one claim) —
    otherwise every finding multiplies by the number of mentions (observed live: 43
    findings that were really 5).
    """
    tables_seen: Dict[str, Dict[str, str]] = {}
    for m in _TABLE_CTX_RE.finditer(text or ""):
        tables_seen.setdefault(m.group(2), {"name": m.group(1), "number": m.group(2)})
    new_seen: Dict[Any, Dict[str, Any]] = {}
    for m in _NEW_FIELD_RE.finditer(text or ""):
        new_seen.setdefault((int(m.group(1)), m.group(2).lower()),
                            {"id": int(m.group(1)), "name": m.group(2)})
    cited_seen: Dict[Any, Dict[str, Any]] = {}
    for m in _CITED_FIELD_RE.finditer(text or ""):
        cited_seen.setdefault((m.group(1).strip().lower(), int(m.group(2))),
                              {"name": m.group(1).strip(), "id": int(m.group(2))})
    return {"item_id": str(item_id), "tables": list(tables_seen.values()),
            "new_fields": list(new_seen.values()), "cited_fields": list(cited_seen.values())}


# ---------------------------------------------------------------------------
# Confrontation: claims x code reality
# ---------------------------------------------------------------------------

def cross_check(
    claims_by_item: List[Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    verified: List[str] = []
    mismatches: List[str] = []
    redundancies: List[str] = []
    conflicts: List[str] = []
    guideline_flags: List[str] = []
    empiric_required: List[str] = []

    table_fields_cache: Dict[str, List[Dict[str, Any]]] = {}

    def fields_of(table_key: str) -> List[Dict[str, Any]]:
        if table_key not in table_fields_cache:
            entry = index.get(table_key)
            table_fields_cache[table_key] = parse_table_fields(entry["file"]) if entry else []
        return table_fields_cache[table_key]

    # cross-item: same new field name on the same table proposed by 2+ items
    proposal_owners: Dict[str, List[str]] = {}

    for claims in claims_by_item:
        item = claims["item_id"]
        for table in claims["tables"]:
            key = f"table {table['number']}"
            entry = index.get(key)
            if entry is None:
                mismatches.append(
                    f"#{item}: claims table {table['name']} ({table['number']}) — NOT FOUND in source")
                continue
            if entry["name"].lower() != table["name"].lower():
                mismatches.append(
                    f"#{item}: table {table['number']} is '{entry['name']}' in source, "
                    f"PBI calls it '{table['name']}'")
            else:
                verified.append(f"#{item}: table {table['name']} ({table['number']}) exists "
                                f"[{entry['file']}]")
            existing = fields_of(key)
            by_id = {f["id"]: f for f in existing}
            by_name = {f["name"].lower(): f for f in existing}

            for nf in claims["new_fields"]:
                proposal_owners.setdefault(f"{key}:{nf['name'].lower()}", []).append(item)
                if nf["id"] in by_id:
                    mismatches.append(
                        f"#{item}: proposes NEW field {nf['id']} '{nf['name']}' on {entry['name']} "
                        f"but id {nf['id']} is ALREADY USED by '{by_id[nf['id']]['name']}'")
                elif nf["name"].lower() in by_name:
                    redundancies.append(
                        f"#{item}: proposed field '{nf['name']}' already exists on {entry['name']} "
                        f"(id {by_name[nf['name'].lower()]['id']}) — duplicate mechanism?")
                else:
                    verified.append(
                        f"#{item}: field id {nf['id']} '{nf['name']}' is FREE on {entry['name']}")
                if len(nf["name"]) > _MAX_FIELD_NAME:
                    guideline_flags.append(
                        f"#{item}: field name '{nf['name']}' exceeds {_MAX_FIELD_NAME} chars (AL0468)")
                # near-duplicate semantics: same suffix already present as flowfield
                for f in existing:
                    if f["flowfield"] and nf["name"].lower() in f["name"].lower():
                        redundancies.append(
                            f"#{item}: proposed '{nf['name']}' overlaps existing flowfield "
                            f"'{f['name']}' on {entry['name']} — verify not redundant")
            if claims["new_fields"]:
                empiric_required.append(
                    f"#{item}: data-model change on {entry['name']} — per guidelines requires "
                    f"item-level bc_reconcile_target (deployed schema) + data-model approval artifact")

            for cf in claims["cited_fields"]:
                match = by_name.get(cf["name"].lower().replace(" ", "").lower()) or by_name.get(cf["name"].lower())
                if match is None:
                    # try relaxed: strip spaces from source names too
                    relaxed = {f["name"].replace(" ", "").lower(): f for f in existing}
                    match = relaxed.get(cf["name"].replace(" ", "").lower())
                if match is None:
                    mismatches.append(
                        f"#{item}: cites existing field '{cf['name']}' ({cf['id']}) on {entry['name']} "
                        f"— NO such field in source")
                elif match["id"] != cf["id"]:
                    mismatches.append(
                        f"#{item}: cites '{cf['name']}' as id {cf['id']} on {entry['name']} but source "
                        f"has id {match['id']} — WRONG ID in PBI")
                else:
                    verified.append(f"#{item}: cited field '{cf['name']}' ({cf['id']}) verified on {entry['name']}")

    for proposal, owners in sorted(proposal_owners.items()):
        if len(set(owners)) > 1:
            conflicts.append(
                f"{proposal.split(':')[1]} on {proposal.split(':')[0]} proposed by multiple items "
                f"({', '.join(sorted(set(owners)))}) — single owner required")

    def _uniq(entries: List[str]) -> List[str]:
        seen: set = set()
        return [e for e in entries if not (e in seen or seen.add(e))]

    verified, mismatches = _uniq(verified), _uniq(mismatches)
    redundancies, conflicts, guideline_flags = _uniq(redundancies), _uniq(conflicts), _uniq(guideline_flags)

    return {
        "verified": verified,
        "mismatches": mismatches,
        "redundancies": redundancies,
        "conflicts": conflicts,
        "guideline_flags": guideline_flags,
        "empiric_required": sorted(set(empiric_required)),
        "counts": {
            "verified": len(verified), "mismatches": len(mismatches),
            "redundancies": len(redundancies), "conflicts": len(conflicts),
            "guideline_flags": len(guideline_flags),
        },
    }


def context_pack(
    claims_by_item: List[Dict[str, Any]],
    index: Dict[str, Dict[str, Any]],
    *,
    max_related: int = 12,
) -> List[Dict[str, str]]:
    """Refinement-seeded code-context pack (deterministic, bounded).

    Rank 1: the files of every object the claims touch (the targets themselves).
    Rank 2: objects REFERENCED BY those target files (names found in their source —
    relations, lookup pages, called codeunits), resolved through the index.
    This is the seed set bc_read_code_context should ground on: verified targets
    first, their direct dependency ring second — never a keyword guess.
    """
    pack: List[Dict[str, str]] = []
    seen_files: set = set()

    def _add(entry: Dict[str, Any], rank: int, why: str) -> None:
        if entry["file"] not in seen_files:
            seen_files.add(entry["file"])
            pack.append({"rank": str(rank), "object": f"{entry['kind']} {entry['number']} {entry['name']}",
                         "file": entry["file"], "why": why})

    targets: List[Dict[str, Any]] = []
    for claims in claims_by_item:
        for table in claims["tables"]:
            entry = index.get(f"table {table['number']}")
            if entry:
                targets.append(entry)
                _add(entry, 1, f"target of #{claims['item_id']}")

    for entry in targets:
        try:
            text = Path(entry["file"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in sorted({m.group(0) for m in _OBJ_NAME_RE.finditer(text)}):
            ref = index.get(name.lower())
            if ref and ref["file"] != entry["file"]:
                _add(ref, 2, f"referenced by {entry['name']}")
            if sum(1 for p in pack if p["rank"] == "2") >= max_related:
                break
    return pack


_OBJ_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{4,}(?:FDN|HSG|CRM|FIN)\b")


def render_refinement_md(feature: Dict[str, Any], findings: Dict[str, Any],
                         critique: Optional[str], *, title_prefix: str = "Feature Refinement") -> str:
    c = findings["counts"]
    lines = [
        f"# {title_prefix}: {feature.get('title', '')} (#{feature.get('id')})",
        "",
        f"_Heuristic (code-world) confrontation, {datetime.now(timezone.utc).isoformat()}._",
        f"_Verdict: {c['verified']} verified · {c['mismatches']} mismatches · "
        f"{c['redundancies']} redundancies · {c['conflicts']} conflicts · "
        f"{c['guideline_flags']} guideline flags._",
        "",
    ]
    for title, key in (("Things that DO NOT add up", "mismatches"),
                       ("Redundancies", "redundancies"),
                       ("Cross-item conflicts", "conflicts"),
                       ("Guideline flags", "guideline_flags"),
                       ("Empiric verification required (per item)", "empiric_required"),
                       ("Verified against source", "verified")):
        lines.append(f"## {title}")
        entries = findings[key]
        lines.extend(f"- {e}" for e in entries) if entries else lines.append("- none")
        lines.append("")
    lines += ["## What this means, in plain language", ""]
    plain: List[str] = []
    if findings["mismatches"]:
        plain.append(
            f"- The ticket makes {len(findings['mismatches'])} claim(s) that are NOT true in "
            f"the actual code — for example: \"{findings['mismatches'][0]}\". We verified each "
            "one in source; the plan follows the code, not the ticket text.")
    if findings["redundancies"]:
        plain.append(
            f"- {len(findings['redundancies'])} thing(s) the ticket asks for already exist — "
            f"for example: \"{findings['redundancies'][0]}\". Building them again would create "
            "duplicates, so they are dropped from the plan.")
    if findings["conflicts"]:
        plain.append(
            f"- {len(findings['conflicts'])} place(s) where two items want to change the same "
            f"thing differently — for example: \"{findings['conflicts'][0]}\". These are decided "
            "ONCE at feature level so the items cannot contradict each other.")
    if findings["guideline_flags"]:
        plain.append(
            f"- {len(findings['guideline_flags'])} team-guideline flag(s) — for example: "
            f"\"{findings['guideline_flags'][0]}\". These need a named process step "
            "(e.g. a second developer's data-model sign-off) before merge.")
    if not plain:
        plain.append("- Every claim in the ticket matched the real code — nothing had to be "
                     "corrected, dropped, or escalated.")
    lines += plain + [""]
    lines += ["## First-principles critique (model judgment — reviewed at gate F1)", ""]
    lines.append(critique.strip() if critique else "_(pending: model critique via bc_refine_feature critique)_")
    return "\n".join(lines) + "\n"


def persist(root: str, spec_name: str, feature: Dict[str, Any],
            claims: List[Dict[str, Any]], findings: Dict[str, Any],
            critique: Optional[str], *,
            md_name: str = "FEATURE-REFINEMENT.md",
            json_name: str = "feature_refinement.json",
            title_prefix: str = "Feature Refinement") -> Dict[str, str]:
    sdir = specs_root(Path(root).resolve()) / spec_name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / json_name).write_text(json.dumps({
        "feature": feature, "claims": claims, "findings": findings,
        "critique": critique or "", "generated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    md = sdir / md_name
    md.write_text(render_refinement_md(feature, findings, critique, title_prefix=title_prefix),
                  encoding="utf-8")
    return {"refinement_md": str(md), "refinement_json": str(sdir / json_name)}
