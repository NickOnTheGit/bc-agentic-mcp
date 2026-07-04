"""object_resolver — fast, cached BC symbol grounding.

Grounds planner objects in the real repo WITHOUT reading every file. Strategy:
  1. If an AL symbol lookup is injected (the AL language server / al_symbolsearch), use it.
  2. Else: build a filename index ONCE per repo (a cheap directory walk, no file reads,
     cached), match candidate filenames by AL's `<Name>.<Kind>.al` convention (allowing the
     dropped affix, e.g. VeraSpaceDetailTypeFDN -> VeraSpaceDetailType.Table.al), and read
     only the handful of matching files to confirm the object header.

Best-effort and non-blocking: unresolved objects are marked, never raised — the plan still emits.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_HEADER = re.compile(
    r"(?im)^\s*(table|tableextension|page|pageextension|codeunit|enum|enumextension|"
    r'report|query|xmlport|interface)\s+(\d+)\s+("([^"]+)"|([A-Za-z0-9_]+))'
)

_KIND_SUFFIX = {
    "table": "Table", "tableextension": "TableExt", "page": "Page", "pageextension": "PageExt",
    "codeunit": "Codeunit", "enum": "Enum", "enumextension": "EnumExt", "report": "Report",
    "query": "Query", "xmlport": "XmlPort", "interface": "Interface",
}
# Common Zig/BC object-name affixes that the source FILE name usually drops.
_AFFIX = re.compile(r"(FDN|SAN|OPN|TAI|HSG|WRV)$")

# Cache the repo's .al filename index (filename_lower -> [paths]); one cheap walk per root.
_INDEX_CACHE: Dict[str, Dict[str, List[Path]]] = {}


def _filename_index(root: Path) -> Dict[str, List[Path]]:
    key = str(root)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index: Dict[str, List[Path]] = {}
    for p in root.rglob("*.al"):  # directory walk only — no file reads
        index.setdefault(p.name.lower(), []).append(p)
    _INDEX_CACHE[key] = index
    return index


def clear_cache() -> None:
    _INDEX_CACHE.clear()


def _candidate_filenames(name: str, kind: str) -> List[str]:
    suffix = _KIND_SUFFIX.get(kind, "")
    cands: List[str] = []
    if suffix:
        cands.append(f"{name}.{suffix}.al".lower())
    cands.append(f"{name}.al".lower())
    core = _AFFIX.sub("", name)
    if core and core != name and suffix:
        cands.append(f"{core}.{suffix}.al".lower())    # file usually drops the affix
        cands.append(f"{core}s.{suffix}.al".lower())   # page names are often plural
    return cands


def _resolve_one(root: Path, index: Dict[str, List[Path]], name: str, kind: str) -> Optional[Dict[str, Any]]:
    paths: List[Path] = []
    for fn in _candidate_filenames(name, kind):
        paths.extend(index.get(fn, []))
    if not paths:  # fallback: any file whose name starts with the affix-stripped core
        core = _AFFIX.sub("", name).lower()
        if len(core) >= 4:
            paths = [p for fn, ps in index.items() if fn.startswith(core) for p in ps][:8]
    for p in paths:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _HEADER.search(content)
        if m and (m.group(4) or m.group(5)).lower() == name.lower():
            return {"target": str(p.relative_to(root)), "object_id": int(m.group(2))}
    return None


def resolve(
    project_root: Path,
    objects: List[Dict[str, Any]],
    symbol_lookup: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Attach {target, object_id, resolved} to each named object. Fast, cached, non-blocking.

    Resolution order (fail down, never up):
      1. the persistent OBJECT INDEX (schema-4 cache; ~0 file reads, memoized) — the
         same table of contents bc_repo_map serves;
      2. the filename-convention heuristic (cheap directory walk, few file reads);
      3. injected symbol packages (dependency objects only — the EXPENSIVE path,
         consulted last and only for objects the workspace does not contain).
    """
    root = Path(project_root).resolve()
    index: Optional[Dict[str, List[Path]]] = None
    toc_objects: Optional[Dict[str, Any]] = None
    try:
        from bc_agentic_mcp import object_index as _oi
        # Grounding needs name->path bindings, not minute-fresh detail: a 24h-old
        # index is fine (object files rarely move), while a 300s TTL forced a full
        # stat-refresh of ~12k files inside EVERY one-shot spec build after git
        # touched mtimes (observed live: bc_write_spec 63s -> 303s timeouts).
        toc_objects = _oi.refresh(root, max_age_seconds=86400)["objects"]
    except Exception:
        toc_objects = None
    out: List[Dict[str, Any]] = []
    for obj in objects:
        merged = dict(obj)
        name, kind = obj.get("name"), obj.get("kind", "")
        found: Optional[Dict[str, Any]] = None
        if name:
            # 1. Persistent index: name alias or affix-stripped core — zero file reads.
            if toc_objects is not None:
                hit = toc_objects.get(str(name).lower()) or toc_objects.get(
                    _AFFIX.sub("", str(name)).lower())
                if hit is not None and (not kind or hit["kind"] == str(kind).lower()):
                    found = {"target": hit.get("rel") or hit.get("file"),
                             "object_id": int(hit["number"])}
            # 2. Filename-convention heuristic.
            if found is None:
                if index is None:
                    index = _filename_index(root)
                found = _resolve_one(root, index, name, kind)
            # 3. Symbol packages — dependency objects only (costly; consulted last).
            if found is None and symbol_lookup is not None:
                try:
                    found = symbol_lookup(kind, name)
                except Exception:
                    found = None
        if found:
            merged.update({"target": found.get("target"), "object_id": found.get("object_id"), "resolved": True})
        else:
            merged["resolved"] = False
        out.append(merged)
    return out
