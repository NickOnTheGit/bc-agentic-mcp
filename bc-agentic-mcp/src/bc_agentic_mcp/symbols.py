"""symbols — authoritative object grounding from AL symbol packages.

``object_resolver`` uses a filename-index heuristic (``<Name>.<Kind>.al`` + affix drop). That
is fast but can mis-resolve when the file name diverges from the object name, and it only sees
workspace files (not base app / dependency objects).

The authoritative source is the compiled **symbol packages** shipped in ``.alpackages/*.app``.
Each ``.app`` is a 40-byte header followed by a standard zip archive containing a
``SymbolReference.json`` that lists every object with its Id, Name and source file. Parsing it
gives deterministic, complete object grounding (workspace + dependencies + base app).

This module builds a cached ``(kind, name) -> {object_id, target}`` index from those packages
and exposes a ``symbol_lookup`` callable that plugs into ``object_resolver.resolve``. It is
best-effort: any parse failure (or absent ``.alpackages``) yields an empty index, so callers
transparently fall back to the filename heuristic.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_ZIP_MAGIC = b"PK\x03\x04"

# SymbolReference.json collection key -> our lowercase object kind.
_COLLECTION_KIND = {
    "Tables": "table",
    "TableExtensions": "tableextension",
    "Pages": "page",
    "PageExtensions": "pageextension",
    "Codeunits": "codeunit",
    "Enums": "enum",
    "EnumExtensions": "enumextension",
    "Reports": "report",
    "Queries": "query",
    "XmlPorts": "xmlport",
    "Interfaces": "interface",
    "Profiles": "profile",
    "PermissionSets": "permissionset",
}

# Common Zig/BC object-name affixes the source file usually drops (mirrors object_resolver).
_AFFIX = re.compile(r"(FDN|SAN|OPN|TAI|HSG|WRV)$")

# (kind, name_lower) -> {"object_id": int|None, "target": str|None}
_INDEX_CACHE: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}

# Skip very large packages (Microsoft base/system/foundation ship 100+ MB symbol files and are
# never the item's target objects). Keeps the scan fast and bounded.
_MAX_APP_BYTES = 30 * 1024 * 1024

# Soft wall-clock budget for a COLD symbol-index build (parsing every .app). On a very large
# multi-app workspace a cold parse can exceed the MCP tool timeout; when the budget is hit we
# return the partial (best-effort) index rather than blocking. Overridable via env.
_SYMBOL_BUDGET_ENV = "BC_MCP_SYMBOL_BUDGET_S"
_DEFAULT_BUDGET_S = 25.0


def _budget_seconds() -> float:
    try:
        return float(os.environ.get(_SYMBOL_BUDGET_ENV, _DEFAULT_BUDGET_S))
    except (TypeError, ValueError):
        return _DEFAULT_BUDGET_S


def clear_cache() -> None:
    _INDEX_CACHE.clear()


def _app_paths(root: Path) -> List[Path]:
    """The ``.alpackages/*.app`` files under the project (sorted, deterministic)."""
    apps = [
        app for app in root.rglob("*.app")
        if ".alpackages" in {p.name for p in app.parents}
    ]
    return sorted(apps, key=lambda p: str(p).lower())


def _fingerprint(apps: List[Path]) -> str:
    """Cheap change-detector over the .app set (path + mtime + size); no parsing."""
    parts: List[str] = []
    for app in apps:
        try:
            st = app.stat()
        except OSError:
            continue
        parts.append(f"{str(app).lower()}|{st.st_mtime_ns}|{st.st_size}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _disk_cache_path(root: Path) -> Path:
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".bc-agentic-mcp" / "symbol-cache" / f"{key}.json"


def _load_disk_cache(path: Path, fingerprint: str) -> Optional[Dict[Tuple[str, str], Dict[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in data.get("entries", []):
        try:
            kind, name, obj_id, target = row
        except (TypeError, ValueError):
            continue
        index[(kind, name)] = {"object_id": obj_id, "target": target}
    return index


def _save_disk_cache(path: Path, fingerprint: str, index: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = [[k[0], k[1], v.get("object_id"), v.get("target")] for k, v in index.items()]
        path.write_text(json.dumps({"fingerprint": fingerprint, "entries": entries}),
                        encoding="utf-8")
    except OSError:
        pass


def read_app_symbols(app_path: Path) -> Optional[Dict[str, Any]]:
    """Extract and parse ``SymbolReference.json`` from an AL ``.app`` package. None on failure."""
    try:
        data = Path(app_path).read_bytes()
    except OSError:
        return None
    start = data.find(_ZIP_MAGIC)
    if start < 0:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data[start:])) as zf:
            name = next(
                (n for n in zf.namelist() if n.lower().endswith("symbolreference.json")),
                None,
            )
            if name is None:
                return None
            raw = zf.read(name)
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    # Symbol JSON is sometimes UTF-8 with a BOM.
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _ingest(node: Dict[str, Any], index: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    """Ingest a node's object collections, recursing into nested ``Namespaces`` (modern format)."""
    for collection, kind in _COLLECTION_KIND.items():
        for obj in node.get(collection) or []:
            if not isinstance(obj, dict):
                continue
            name = obj.get("Name")
            if not name:
                continue
            key = (kind, str(name).lower())
            # First definition wins (workspace packages are typically scanned first); do not
            # overwrite a resolved source path with a base-app entry that lacks one.
            entry = index.get(key)
            target = obj.get("ReferenceSourceFileName") or None
            obj_id = obj.get("Id")
            if entry is None:
                index[key] = {"object_id": obj_id, "target": target}
            elif entry.get("target") is None and target:
                entry["target"] = target
    for ns in node.get("Namespaces") or []:
        if isinstance(ns, dict):
            _ingest(ns, index)


def build_symbol_index(
    project_root: Path, *, use_disk_cache: bool = True
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Build (and cache) the object index from every ``.alpackages/*.app`` under the project.

    Persistent + bounded: a disk cache keyed on the .app fingerprint means a cold process
    (e.g. a freshly restarted MCP server) reuses a prior parse instead of re-reading every
    package; a wall-clock budget stops an unbounded cold parse and returns the partial index.
    """
    root = Path(project_root).resolve()
    key = str(root)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    apps = _app_paths(root)
    fingerprint = _fingerprint(apps)
    disk_path = _disk_cache_path(root)
    if use_disk_cache:
        disk = _load_disk_cache(disk_path, fingerprint)
        if disk is not None:
            _INDEX_CACHE[key] = disk
            return disk

    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    budget = _budget_seconds()
    start = time.monotonic()
    truncated = False
    for app in apps:
        if time.monotonic() - start > budget:
            truncated = True  # best-effort: return what we have rather than block
            break
        try:
            if app.stat().st_size > _MAX_APP_BYTES:
                continue  # skip huge platform/base packages (not item targets)
        except OSError:
            continue
        symref = read_app_symbols(app)
        if symref:
            _ingest(symref, index)
    _INDEX_CACHE[key] = index
    if use_disk_cache and not truncated:
        _save_disk_cache(disk_path, fingerprint, index)
    return index


def make_symbol_lookup(project_root: Path) -> Callable[[str, str], Optional[Dict[str, Any]]]:
    """Return a ``symbol_lookup(kind, name) -> {target, object_id} | None`` over the symbol index.

    Tries the exact name, then the affix-stripped core (e.g. ``VeraSpaceDetailTypeFDN`` ->
    ``VeraSpaceDetailType``), matching ``object_resolver`` conventions.
    """
    index = build_symbol_index(project_root)

    def lookup(kind: str, name: str) -> Optional[Dict[str, Any]]:
        if not name:
            return None
        k = (kind or "").lower()
        candidates: List[str] = [name.lower()]
        core = _AFFIX.sub("", name)
        if core and core.lower() != name.lower():
            candidates.append(core.lower())
        for cand in candidates:
            hit = index.get((k, cand))
            if hit:
                return {"target": hit.get("target"), "object_id": hit.get("object_id")}
        return None

    return lookup


def make_lazy_symbol_lookup(project_root: Path) -> Callable[[str, str], Optional[Dict[str, Any]]]:
    """Like :func:`make_symbol_lookup` but defers the (potentially costly) index build to the FIRST
    actual lookup. Paired with filename-first resolution, workspace-only items never build it."""
    root = Path(project_root)
    state: Dict[str, Any] = {}

    def lookup(kind: str, name: str) -> Optional[Dict[str, Any]]:
        fn = state.get("fn")
        if fn is None:
            fn = make_symbol_lookup(root)
            state["fn"] = fn
        return fn(kind, name)

    return lookup
