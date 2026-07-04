"""consumers — deterministic discovery of who CONSUMES an AL symbol.

Operationalizes "business logic lives in the consumers": given a field/table/symbol name
and a source root, find every AL file that references it and classify the enclosing object
(table/page/codeunit/query/report/…). Pure filesystem scan — reproducible given the tree.
The symbol is an input; nothing is hardcoded.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# AL object header, e.g. `codeunit 50000 "My Cod"` / `tableextension 60 Foo extends Bar`.
_OBJECT_RE = re.compile(
    r"^\s*(?P<kind>table|tableextension|page|pageextension|codeunit|report|query|"
    r"xmlport|enum|enumextension|interface|controladdin|permissionset)\b"
    r"(?:\s+(?P<id>\d+))?\s+(?P<name>\"[^\"]+\"|[A-Za-z0-9_]+)",
    re.IGNORECASE,
)

_DEFAULT_GLOBS = ("*.al",)
_SKIP_DIRS = {".git", ".alpackages", ".vscode", "bin", "obj", "node_modules", "generated"}


def _word_re(symbol: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")


def find_consumers(
    root: str,
    symbol: str,
    *,
    exclude_definition: bool = True,
    max_files: Optional[int] = None,
) -> Dict[str, Any]:
    """Find AL files/objects that reference ``symbol``.

    Returns a deterministic, sorted structure. When ``exclude_definition`` is set, a file
    whose enclosing object is a table/tableextension AND that declares ``field(... symbol``
    is treated as the definition site, not a consumer.
    """
    base = Path(root)
    if not symbol.strip():
        raise ValueError("symbol is required")
    word = _word_re(symbol)
    field_def = re.compile(rf"field\s*\(\s*\d+\s*;\s*{re.escape(symbol)}\b", re.IGNORECASE)

    files = sorted(
        p for p in base.rglob("*.al")
        if not any(part in _SKIP_DIRS for part in p.parts)
    )
    consumers: List[Dict[str, Any]] = []
    definition_sites: List[Dict[str, Any]] = []
    scanned = 0
    for path in files:
        if max_files is not None and scanned >= max_files:
            break
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        scanned += 1
        current_obj: Optional[Dict[str, Any]] = None
        hits: List[Dict[str, Any]] = []
        is_definition = False
        for i, line in enumerate(lines, start=1):
            hdr = _OBJECT_RE.match(line)
            if hdr:
                current_obj = {
                    "kind": hdr.group("kind").lower(),
                    "id": int(hdr.group("id")) if hdr.group("id") else None,
                    "name": hdr.group("name").strip('"'),
                }
            if field_def.search(line):
                is_definition = True
            if word.search(line):
                hits.append({"line": i, "text": line.strip()[:200],
                             "object": dict(current_obj) if current_obj else None})
        if not hits:
            continue
        entry = {
            "file": str(path.relative_to(base)) if path.is_relative_to(base) else str(path),
            "hit_count": len(hits),
            "objects": sorted({(h["object"] or {}).get("kind", "") for h in hits}),
            "hits": hits,
        }
        if is_definition and exclude_definition:
            definition_sites.append(entry)
        else:
            consumers.append(entry)

    # Aggregate the object kinds that consume the symbol — the fastest "where does the
    # behaviour live" signal (e.g. a codeunit consumer implies derived logic to test).
    kinds: Dict[str, int] = {}
    for c in consumers:
        for k in c["objects"]:
            if k:
                kinds[k] = kinds.get(k, 0) + 1
    return {
        "symbol": symbol,
        "files_scanned": scanned,
        "consumer_count": len(consumers),
        "consumer_kinds": dict(sorted(kinds.items())),
        "consumers": consumers,
        "definition_sites": definition_sites,
        # Codeunit/report/query consumers usually mean derived behaviour worth a
        # business-logic test — a deterministic nudge, computed, not hardcoded.
        "has_derived_logic": any(k in kinds for k in ("codeunit", "report", "query")),
    }
