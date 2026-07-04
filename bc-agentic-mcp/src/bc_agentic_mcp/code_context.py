"""code_context — deterministic "search the code for similar work" phase.

After the item read-context, find the precedents to MIRROR before planning: sibling objects in
the same area, same-kind precedents (e.g. other upgrade codeunits), and the target's conventions
(DataPerCompany, existing field ids). Fast (reuses the cached filename index; reads only the few
candidate files), explicit (records WHY each hit matches), deterministic (pure ranking).

Precondition (enforced): the source must be LATEST + CLEAN (repo_state) — precedents from stale or
dirty source mislead. Cleaning is reversible only (ff-pull / stash); never destructive.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import object_resolver, repo_state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repo_index_sha(index: Dict[str, List[Path]]) -> str:
    """Stable hash of the repo's .al filename set — changes when files are added/removed."""
    joined = "\n".join(sorted(index.keys()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _index_by_dir(index: Dict[str, List[Path]]) -> Dict[str, List[Path]]:
    by_dir: Dict[str, List[Path]] = {}
    for paths in index.values():
        for p in paths:
            by_dir.setdefault(str(p.parent), []).append(p)
    return by_dir


def build(
    project_root: str,
    objects: List[Dict[str, Any]],
    fields: Optional[List[Dict[str, Any]]] = None,
    work_types: Optional[List[str]] = None,
    *,
    require_clean_latest: bool = True,
    auto_pull: bool = True,
    runner=None,
    symbol_lookup=None,
) -> Dict[str, Any]:
    """Build the code read-context (precedents + conventions) for the given item objects."""
    root = Path(project_root).resolve()
    # Authoritative object grounding from .alpackages symbol packages, falling back to the
    # filename heuristic inside object_resolver when no packages are present.
    if symbol_lookup is None:
        try:
            from bc_agentic_mcp import symbols as _symbols
            symbol_lookup = _symbols.make_lazy_symbol_lookup(root)
        except Exception:
            symbol_lookup = None
    work_types = work_types or []
    result: Dict[str, Any] = {
        "generated_at": _now(), "work_types": work_types,
        "repo": None, "status": "ok", "similar": {}, "conventions": {},
    }

    # --- Precondition: latest + clean source -------------------------------
    if require_clean_latest:
        check = repo_state.is_clean_latest(str(root), runner)
        if not check["ok"] and check["status"].get("behind") and auto_pull:
            # Reversible remediation: fast-forward pull, then re-check.
            result["pull"] = repo_state.make_latest(str(root), runner)
            check = repo_state.is_clean_latest(str(root), runner)
        result["repo"] = check
        if not check["ok"]:
            result["status"] = "blocked_repo_not_clean_latest"
            result["remediation"] = [
                "git pull --ff-only (done automatically when only 'behind')",
                "commit or `git stash` local changes (reversible) — destructive cleaning is not automatic",
            ]
            return result

    # --- Ground objects in the repo ----------------------------------------
    # Index-first (the persistent schema-4 table of contents); symbol packages are
    # only consulted for dependency objects the workspace does not contain.
    resolved = object_resolver.resolve(root, objects, symbol_lookup=symbol_lookup)

    # Siblings/conventions come from the SAME persistent index — no second repo walk.
    from bc_agentic_mcp import object_index as _oi
    toc = _oi.refresh(root, max_age_seconds=300)
    entries = [v for k, v in toc["objects"].items() if " " in k]
    by_dir: Dict[str, List[str]] = {}
    for e in sorted(entries, key=lambda x: x.get("rel", "")):
        rel = e.get("rel")
        if rel:
            by_dir.setdefault(str(Path(rel).parent), []).append(rel)
    result["repo_index_sha"] = hashlib.sha256(
        "\n".join(sorted(f["rel"] for f in entries if f.get("rel"))).encode("utf-8")
    ).hexdigest()[:16]

    # Target directories (for proximity ranking of precedents).
    target_dirs = [str(Path(o["target"]).parent) for o in resolved if o.get("target")]

    def _proximity(rel_path: str) -> int:
        """Shared leading path-segment count with the nearest target dir (higher = closer)."""
        parts = Path(rel_path).parts
        best = 0
        for td in target_dirs:
            tparts = Path(td).parts
            shared = 0
            for a, b in zip(parts, tparts):
                if a == b:
                    shared += 1
                else:
                    break
            best = max(best, shared)
        return best

    # 1) Sibling precedents: indexed objects in the SAME folder as each resolved target.
    siblings: List[Dict[str, str]] = []
    seen_sib = set()
    for o in resolved:
        target = o.get("target")
        if not target:
            continue
        d = str(Path(target).parent)
        for rel in by_dir.get(d, []):
            if rel == target or rel in seen_sib:
                continue
            seen_sib.add(rel)
            siblings.append({"path": rel, "why": f"same folder as {o.get('kind')} {o.get('name')}"})
    result["similar"]["siblings"] = siblings[:25]

    # 2) Same-kind precedents (bounded, from the index — no repo scan).
    if "upgrade" in work_types:
        upg = []
        for e in entries:
            rel = e.get("rel", "")
            if e["kind"] == "codeunit" and "upgrade" in rel.lower():
                upg.append({"path": rel, "why": "existing upgrade codeunit — mirror its interface/registration"})
        result["similar"]["upgrade_precedents"] = sorted(
            upg, key=lambda x: (-_proximity(x["path"]), x["path"])
        )[:12]

    # 3) Conventions of the target — from the index's precomputed detail (no file reads).
    conventions: Dict[str, Any] = {}
    for o in resolved:
        if o.get("kind") == "table" and o.get("target"):
            entry = toc["objects"].get(f"table {o.get('object_id')}")
            detail = (entry or {}).get("detail") or {}
            props = " ".join(detail.get("props", []))
            m = re.search(r"(?i)DataPerCompany\s*=\s*(\w+)", props)
            conventions["data_per_company"] = (m.group(1).lower() if m else "true (AL default)")
            field_ids = [f["id"] for f in detail.get("fields", []) or []]
            if field_ids:
                conventions["max_field_id"] = max(field_ids)
                conventions["next_field_id_hint"] = max(field_ids) + 10
            conventions["target_table_file"] = o["target"]
    result["conventions"] = conventions

    result["unresolved_objects"] = [
        {"kind": o.get("kind"), "name": o.get("name")} for o in resolved if not o.get("resolved")
    ]
    return result


def save(project_root: str, spec_name: str, code_context: Dict[str, Any]) -> str:
    """Persist the code read-context next to the item read-context."""
    root = Path(project_root).resolve()
    cdir = specs_root(root) / spec_name / "context" / "code"
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / "code_context.json"
    path.write_text(json.dumps(code_context, indent=2), encoding="utf-8")
    return str(path)


def handle_read_code_context(
    project_root: str,
    spec_name: str,
    objects: Optional[List[Dict[str, Any]]] = None,
    fields: Optional[List[Dict[str, Any]]] = None,
    work_types: Optional[List[str]] = None,
    require_clean_latest: bool = True,
) -> Dict[str, Any]:
    """bc_read_code_context: find precedents/conventions for the item's objects, save to disk.

    If objects/work_types are omitted, they are derived from the saved spec.json (if present).
    """
    root = Path(project_root).resolve()
    if objects is None or work_types is None:
        spec_path = specs_root(root) / spec_name / "spec.json"
        if spec_path.exists():
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                spec = {}
            work_types = work_types or spec.get("work_types", [])
            if objects is None:
                objects = []
                for o in spec.get("objects_to_modify", []) + spec.get("objects_to_create", []):
                    objects.append({"kind": str(o.get("type", "")).lower(), "name": o.get("name")})
    ctx = build(str(root), objects or [], fields, work_types or [],
                require_clean_latest=require_clean_latest)
    ctx["path"] = save(str(root), spec_name, ctx)
    return ctx
