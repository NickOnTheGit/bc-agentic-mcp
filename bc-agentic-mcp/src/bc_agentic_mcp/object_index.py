"""object_index — persistent, incremental AL object index (the repo's table of contents).

Schema 3: the ONE expensive pass extracts everything downstream consumers need —
object headers, outgoing references, AND per-object detail (caption, decision-input
properties, field shapes, keys, procedure signatures). After the first build,
refreshes are stat-only and every lookup/render is a pure cache read: the huge cost
is paid exactly once per repo, then everything is fast.

Cache: .specs/.index/objects.json — shared by refinement, the ranked code pack,
and the repo-map query tool.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from bc_agentic_mcp.workspace import specs_root

_SCHEMA = 6  # bump to force a full rebuild when the per-file payload changes

_SKIP_DIRS = {".git", ".vscode", ".alpackages", ".specs", "node_modules", "out", "build"}
# LONGER alternatives FIRST (enumextension before enum, …) or the shorter keyword wins
# the alternation. Name = quoted string OR bare identifier — NEVER a space-including
# class (observed live: 'FeatureExt extends FeatureSAN' was swallowed as the name,
# making every extension object unresolvable). The extends-target is captured as data.
_HEADER_RE = re.compile(
    r'^\s*(tableextension|table|pageextension|page|codeunit|reportextension|report|'
    r'enumextension|enum|query|xmlport|interface|permissionset|profile|controladdin)'
    r'\s+(\d+)\s+("[^"]+"|[A-Za-z0-9_]+)(?:\s+extends\s+("[^"]+"|[A-Za-z0-9_]+))?',
    re.IGNORECASE | re.MULTILINE)
_REF_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{4,}(?:FDN|HSG|CRM|FIN|SAN|OPN|EMP|TAI|WRV)\b")
_CAPTION_RE = re.compile(r"^\s*Caption\s*=\s*'([^']*)'", re.MULTILINE)
_PROP_RE = re.compile(
    r'^\s*((?:DataPerCompany|DataClassification|TableType|LookupPageId|DrillDownPageId|'
    r'SourceTable|PageType|ApplicationArea|Extensible|ObsoleteState)\s*=\s*[^;]+);',
    re.MULTILINE)
_PROC_RE = re.compile(
    r'^\s*((?:local\s+|internal\s+|protected\s+)?(?:procedure|trigger)\s+"?[A-Za-z0-9_]+"?\s*\([^)]*\)(?:\s*:?\s*[A-Za-z0-9_\[\]" ]+)?)',
    re.MULTILINE)
_KEY_RE = re.compile(r'^\s*(key\(\s*[A-Za-z0-9_]+\s*;[^)]+\))', re.MULTILINE)
_FIELD_RE = re.compile(r'field\(\s*(\d+)\s*;\s*"?([A-Za-z0-9_ ]+?)"?\s*;\s*([^);]+)\)')

_MAX_FIELDS = 200
_MAX_PROCS = 80

# In-process memo (the MCP server is long-lived): parsed cache + derived edges,
# keyed by the cache file's mtime — if the file on disk hasn't changed, neither
# re-parsing 11MB of JSON nor rebuilding the graph is ever repeated.
_MEMO: Dict[str, Dict[str, Any]] = {}


def index_path(project_root: Path) -> Path:
    return specs_root(Path(project_root).resolve()) / ".index" / "objects.json"


def _field_details(text: str) -> List[Dict[str, Any]]:
    """Field shapes with property flags, extracted once at index time."""
    fields: List[Dict[str, Any]] = []
    for m in _FIELD_RE.finditer(text):
        block_start = text.find("{", m.end())
        depth, pos = 0, block_start
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
        })
        if len(fields) >= _MAX_FIELDS:
            break
    return fields


def _parse_file(path: Path) -> Dict[str, Any]:
    """THE one expensive pass per file: header, references, and full object detail."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"objects": [], "refs": []}
    objects = []
    own_names: Set[str] = set()
    m = _HEADER_RE.search(text[:2048])
    if m:
        kind, number, name = m.group(1).lower(), m.group(2), m.group(3).strip('"')
        extends = m.group(4).strip('"') if m.group(4) else None
        own_names.add(name)
        cap = _CAPTION_RE.search(text)
        detail: Dict[str, Any] = {
            "caption": cap.group(1) if cap else "",
            "props": [p.group(1).strip() for p in _PROP_RE.finditer(text)][:10],
            "procedures": [" ".join(p.group(1).split()) for p in _PROC_RE.finditer(text)][:_MAX_PROCS],
        }
        if extends:
            detail["extends"] = extends
        if kind in ("table", "tableextension"):
            detail["fields"] = _field_details(text)
            detail["keys"] = [k.group(1).strip() for k in _KEY_RE.finditer(text)][:10]
        objects.append({"kind": kind, "number": number, "name": name, "detail": detail})
    refs = sorted({r for r in set(_REF_NAME_RE.findall(text)) if r not in own_names})
    return {"objects": objects, "refs": refs}


def _walk_al_files(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith(".al"):
                yield Path(dirpath) / fn


def _objects_from_files(root: Path, files: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    objects: Dict[str, Dict[str, Any]] = {}
    for rel in sorted(files):
        for obj in files[rel]["objects"]:
            entry = {**obj, "file": str(root / rel), "rel": rel}
            objects.setdefault(f"{obj['kind']} {obj['number']}", entry)
            objects.setdefault(obj["name"].lower(), entry)
    return objects


def _memo_get(root: Path) -> Optional[Dict[str, Any]]:
    """Return the memoized parsed cache if the on-disk cache file is unchanged."""
    cache_file = index_path(root)
    try:
        mtime = cache_file.stat().st_mtime_ns
    except OSError:
        return None
    memo = _MEMO.get(str(root))
    if memo and memo["cache_mtime"] == mtime:
        return memo
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if cached.get("schema") != _SCHEMA:
        return None
    files = cached.get("files", {})
    memo = {
        "cache_mtime": mtime,
        "built_at": cached.get("built_at", ""),
        "files": files,
        "objects": _objects_from_files(root, files),
        "edges": None,  # built lazily, memoized below
    }
    _MEMO[str(root)] = memo
    return memo


def refresh(project_root: Path, *, subdir: str = "extensions",
            max_age_seconds: float = 0) -> Dict[str, Any]:
    """Build or incrementally refresh the index. Returns {objects, files, stats}.

    ``max_age_seconds`` > 0 enables the TTL fast-path: when the on-disk cache is
    younger than the TTL, the stat-walk is skipped entirely and answers come from
    the in-process memo (~ms). Refinement callers use 0 (always reconcile with the
    filesystem); read-only browsing (bc_repo_map) tolerates a small TTL.
    """
    started = time.monotonic()
    root = Path(project_root).resolve()

    if max_age_seconds > 0:
        cache_file = index_path(root)
        try:
            age = time.time() - cache_file.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age <= max_age_seconds:
            memo = _memo_get(root)
            if memo is not None:
                return {
                    "root": str(root),
                    "objects": memo["objects"],
                    "files": memo["files"],
                    "stats": {"files": len(memo["files"]), "parsed": 0,
                              "reused": len(memo["files"]),
                              "objects": len({id(v) for v in memo["objects"].values()}),
                              "took_ms": round((time.monotonic() - started) * 1000),
                              "fast_path": True, "cache_age_s": round(age)},
                }

    base = root / subdir
    if not base.is_dir():
        base = root
    cache_file = index_path(root)

    cached_files: Dict[str, Any] = {}
    memo = _memo_get(root)
    if memo is not None:
        cached_files = memo["files"]  # memo == parsed disk cache, no re-read
    elif cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("schema") == _SCHEMA:  # incompatible cache = full rebuild
                cached_files = cached.get("files", {})
        except (OSError, json.JSONDecodeError):
            cached_files = {}

    files: Dict[str, Any] = {}
    parsed = reused = 0
    for path in _walk_al_files(base):
        rel = str(path.relative_to(root))
        try:
            stat = path.stat()
        except OSError:
            continue
        sig = [int(stat.st_mtime_ns), int(stat.st_size)]
        prior = cached_files.get(rel)
        if prior and prior.get("sig") == sig:
            files[rel] = prior
            reused += 1
        else:
            files[rel] = {"sig": sig, **_parse_file(path)}
            parsed += 1

    objects = _objects_from_files(root, files)

    if parsed > 0 or len(files) != len(cached_files):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "schema": _SCHEMA,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "root": str(root), "files": files,
        }), encoding="utf-8")
    try:
        cache_mtime = cache_file.stat().st_mtime_ns
    except OSError:
        cache_mtime = 0
    _MEMO[str(root)] = {"cache_mtime": cache_mtime, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "files": files, "objects": objects, "edges": None}

    return {
        "root": str(root),
        "objects": objects,
        "files": files,
        "stats": {
            "files": len(files), "parsed": parsed, "reused": reused,
            "objects": len({id(v) for v in objects.values()}),
            "took_ms": round((time.monotonic() - started) * 1000),
        },
    }


def edges_for(project_root: Path, index_data: Dict[str, Any]) -> Dict[str, Set[str]]:
    """Reference graph, memoized per cache generation — built at most once per change."""
    root = str(Path(project_root).resolve())
    memo = _MEMO.get(root)
    if memo is not None and memo.get("edges") is not None:
        return memo["edges"]
    edges = build_edges(index_data.get("files", {}), index_data["objects"])
    if memo is not None:
        memo["edges"] = edges
    return edges


def build_edges(files: Dict[str, Any], objects: Dict[str, Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Object-level reference graph: canonical key -> set of referenced canonical keys.

    Canonical key = "<kind> <number>" (stable across renames of casing). References
    are resolved through the name index; unresolvable names are dropped (external
    or base-app symbols outside the repo).
    """
    edges: Dict[str, Set[str]] = {}
    for rel in sorted(files):
        payload = files[rel]
        for obj in payload.get("objects", []):
            src_key = f"{obj['kind']} {obj['number']}"
            targets = edges.setdefault(src_key, set())
            for name in payload.get("refs", []):
                ref = objects.get(name.lower())
                if ref is not None:
                    ref_key = f"{ref['kind']} {ref['number']}"
                    if ref_key != src_key:
                        targets.add(ref_key)
    return edges


def toc_search(
    objects: Dict[str, Dict[str, Any]],
    query: str,
    *,
    kind: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Table-of-contents lookup: substring match on name OR caption, deterministic order.

    Pure cache read — zero file I/O. This is what "find the right object fast"
    looks like once the repo is indexed.
    """
    q = (query or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for key in sorted(objects):
        if " " not in key:  # skip the name aliases; canonical keys only
            continue
        entry = objects[key]
        if kind and entry["kind"] != kind.lower():
            continue
        caption = str((entry.get("detail") or {}).get("caption", ""))
        if q and q not in entry["name"].lower() and q not in caption.lower():
            continue
        ident = f"{entry['kind']} {entry['number']}"
        if ident in seen:
            continue
        seen.add(ident)
        detail = entry.get("detail") or {}
        rows.append({
            "object": f"{entry['kind']} {entry['number']} {entry['name']}",
            "caption": caption,
            "file": entry.get("rel", entry.get("file", "")),
            "fields": len(detail.get("fields", []) or []),
            "procedures": len(detail.get("procedures", []) or []),
        })
        if len(rows) >= max(1, int(limit)):
            break
    return rows
