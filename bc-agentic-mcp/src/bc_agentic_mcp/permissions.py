"""permissions — deterministic Business Central permission-set coverage analysis.

Encodes the lesson from the rentalMutation correction: BC permissions are granted at the
TABLE level (``tabledata X = RIMD``), so adding fields to an API page needs NO permission
change when a set already grants the required access on that table (directly or via
``IncludedPermissionSets``). This checker parses the actual permission-set files and answers
"is <table> already granted <access>?" — so the agent verifies instead of guessing (and does
not blindly edit a permission set, nor wrongly assume one is needed).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_HEADER_RE = re.compile(
    r"permissionset\s+(?P<id>\d+)\s+(?P<name>\"[^\"]+\"|[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_INCLUDED_RE = re.compile(r"IncludedPermissionSets\s*=\s*(?P<body>[^;]+);", re.IGNORECASE)
_PERMS_RE = re.compile(r"Permissions\s*=\s*(?P<body>.*?);", re.IGNORECASE | re.DOTALL)
_TABLEDATA_RE = re.compile(
    r"tabledata\s+(?:\"(?P<t1>[^\"]+)\"|(?P<t2>[A-Za-z0-9_]+))\s*=\s*(?P<acc>[RIMDX]+)",
    re.IGNORECASE,
)


def _norm(name: str) -> str:
    return str(name or "").strip().strip('"').lower()


def _split_list(body: str) -> List[str]:
    out: List[str] = []
    for part in body.split(","):
        p = part.strip().strip('"').strip()
        if p:
            out.append(p)
    return out


def parse_permission_set(text: str) -> Optional[Dict[str, Any]]:
    """Parse one permissionset AL file into {name, id, included[], tabledata{table: access}}."""
    h = _HEADER_RE.search(text or "")
    if not h:
        return None
    included: List[str] = []
    mi = _INCLUDED_RE.search(text)
    if mi:
        included = _split_list(mi.group("body"))
    tabledata: Dict[str, str] = {}
    mp = _PERMS_RE.search(text)
    if mp:
        for m in _TABLEDATA_RE.finditer(mp.group("body")):
            table = m.group("t1") or m.group("t2")
            acc = m.group("acc").upper()
            tabledata[_norm(table)] = "".join(sorted(set(acc)))
    return {
        "name": h.group("name").strip('"'),
        "id": int(h.group("id")),
        "included": included,
        "tabledata": tabledata,
    }


def scan_permission_sets(root: str) -> Dict[str, Dict[str, Any]]:
    """Parse every ``*.permissionset*.al`` under ``root`` -> {normalized name: parsed}."""
    base = Path(root)
    out: Dict[str, Dict[str, Any]] = {}
    for path in base.rglob("*.al"):
        low = path.name.lower()
        if "permissionset" not in low:
            continue
        try:
            parsed = parse_permission_set(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if parsed:
            parsed["file"] = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
            out[_norm(parsed["name"])] = parsed
    return out


def effective_tabledata(sets: Dict[str, Dict[str, Any]], name: str) -> Dict[str, str]:
    """Resolve a set's effective tabledata grants, merging IncludedPermissionSets (recursive)."""
    result: Dict[str, str] = {}
    seen: Set[str] = set()

    def visit(n: str) -> None:
        key = _norm(n)
        if key in seen or key not in sets:
            return
        seen.add(key)
        ps = sets[key]
        for inc in ps.get("included", []):
            visit(inc)
        for table, acc in ps.get("tabledata", {}).items():
            merged = set(result.get(table, "")) | set(acc)
            result[table] = "".join(sorted(merged))

    visit(name)
    return result


def covers(granted_access: str, required_access: str) -> bool:
    """Deterministic: every required permission letter is present in the granted access."""
    return set(required_access.upper()) <= set((granted_access or "").upper())


def find_coverage(root: str, table: str, required_access: str) -> Dict[str, Any]:
    """Which permission sets grant ``required_access`` on ``table`` (directly or via includes)?"""
    sets = scan_permission_sets(root)
    table_n = _norm(table)
    covering: List[Dict[str, Any]] = []
    for name, ps in sets.items():
        eff = effective_tabledata(sets, ps["name"])
        granted = eff.get(table_n, "")
        if granted and covers(granted, required_access):
            covering.append({"permission_set": ps["name"], "granted": granted, "file": ps.get("file")})
    covering.sort(key=lambda c: c["permission_set"])
    return {
        "table": table,
        "required_access": required_access.upper(),
        "covered": bool(covering),
        "covering_sets": covering,
        # The actionable conclusion: if covered, adding fields needs NO permission change.
        "permission_change_needed": not covering,
        "sets_scanned": len(sets),
    }
