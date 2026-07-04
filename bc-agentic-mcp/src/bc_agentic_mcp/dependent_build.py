"""dependent_build — pre-PR wall for cross-app compile truth.

Born from the same live failure family as breaking_change (Bug 267600): the app
under change compiled clean while its DEPENDENT app (EmpireHousingTests, which
referenced a deleted codeunit) broke — locally invisible because the per-app
compile never builds dependents, then the container publish failed with AL0185
and uninstalled the whole dependency chain (collateral damage, twice).

The wall: from the merge-base diff, find every app whose files changed, expand
to the apps that DEPEND on them (app.json dependency graph over the repo), and
compile that closure in dependency order. A dependent that no longer compiles
is exactly what the merge pipeline's full build would catch — an hour later.

Pure parts (graph build, closure, order) are unit-testable without a compiler;
`gate()` composes them with al_compiler.compile_project.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def discover_apps(repo_root: Path) -> Dict[str, Dict[str, Any]]:
    """Map app-name -> {id, dir, dependencies:[names], test_app:bool} from app.json files.

    Only apps under extensions/ (repo convention); symbol caches and node_modules
    never contain app.json at depth 2.
    """
    apps: Dict[str, Dict[str, Any]] = {}
    ext = repo_root / "extensions"
    if not ext.is_dir():
        return apps
    for app_json in sorted(ext.glob("*/app.json")):
        try:
            data = json.loads(app_json.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(data.get("name") or app_json.parent.name)
        deps = [str(d.get("name")) for d in (data.get("dependencies") or []) if d.get("name")]
        apps[name] = {
            "id": str(data.get("id") or ""),
            "dir": app_json.parent,
            "dependencies": deps,
            "test_app": any("test" in str(d.get("name", "")).lower()
                            for d in (data.get("dependencies") or [])) or name.lower().endswith("tests"),
        }
    return apps


def changed_apps(repo_root: Path, target_branch: str = "master") -> Set[str]:
    """App names whose files changed vs merge-base(origin/<target>, HEAD)."""
    def _git(*args: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=120, check=False,
                stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    base = _git("merge-base", f"origin/{target_branch}", "HEAD") or _git("merge-base", target_branch, "HEAD")
    if not base:
        return set()
    files = _git("diff", "--name-only", base.strip(), "HEAD") or ""
    apps = discover_apps(repo_root)
    hit: Set[str] = set()
    for line in files.splitlines():
        p = line.strip().replace("\\", "/")
        for name, info in apps.items():
            rel = str(info["dir"].relative_to(repo_root)).replace("\\", "/")
            if p.startswith(rel + "/"):
                hit.add(name)
    return hit


def dependents_closure(apps: Dict[str, Dict[str, Any]], seeds: Set[str]) -> Set[str]:
    """Seeds plus every app that (transitively) depends on a seed (pure)."""
    result = set(s for s in seeds if s in apps)
    changed = True
    while changed:
        changed = False
        for name, info in apps.items():
            if name in result:
                continue
            if any(dep in result for dep in info["dependencies"]):
                result.add(name)
                changed = True
    return result


def build_order(apps: Dict[str, Dict[str, Any]], names: Set[str]) -> List[str]:
    """Dependency-ordered list (dependencies first) of the selected apps (pure)."""
    ordered: List[str] = []
    remaining = set(n for n in names if n in apps)
    while remaining:
        progress = False
        for name in sorted(remaining):
            deps_in_set = [d for d in apps[name]["dependencies"] if d in remaining]
            if not deps_in_set:
                ordered.append(name)
                remaining.discard(name)
                progress = True
        if not progress:  # cycle — emit deterministically rather than hang
            ordered.extend(sorted(remaining))
            break
    return ordered


def gate(repo_root: Path, target_branch: str = "master",
         compile_fn=None, max_apps: int = 8) -> Dict[str, Any]:
    """Compile the dependent closure of changed apps; refuse on NEW errors.

    Oversized closures are TRUNCATED, never skipped: a widely-depended app
    (EmpireHousing has ~20 transitive dependents) would otherwise get ZERO
    protection exactly where the observed bug class lives — its DIRECT
    dependents (the test app referencing a deleted codeunit). Priority:
    seeds, then direct dependents, then the rest in dependency order.

    compile_fn(app_dir) -> List[error dicts] is injectable for tests; default
    wires al_compiler.compile_project with the shared .alpackages cache.
    """
    apps = discover_apps(repo_root)
    seeds = changed_apps(repo_root, target_branch)
    if not seeds:
        return {"ok": True, "checked": [], "note": "no changed apps vs merge-base"}
    closure = dependents_closure(apps, seeds)
    order = build_order(apps, closure)
    truncated = False
    if len(order) > max_apps:
        direct = {name for name, info in apps.items()
                  if any(dep in seeds for dep in info["dependencies"])}
        priority = [n for n in order if n in seeds or n in direct]
        rest = [n for n in order if n not in seeds and n not in direct]
        order = (priority + rest)[:max_apps]
        truncated = True

    if compile_fn is None:
        from bc_agentic_mcp import al_compiler

        def compile_fn(app_dir: Path) -> List[Dict[str, Any]]:  # pragma: no cover (integration)
            result = al_compiler.compile_project(app_dir)
            return [i for i in (result.get("issues") or [])
                    if str(i.get("severity", "")).lower() == "error"]

    failures: List[Dict[str, Any]] = []
    checked: List[str] = []
    for name in order:
        errors = compile_fn(apps[name]["dir"])
        checked.append(name)
        if errors:
            failures.append({
                "app": name,
                "error_count": len(errors),
                "first_errors": [str(e.get("message", ""))[:160] for e in errors[:5]],
            })
    out: Dict[str, Any] = {
        "ok": not failures,
        "checked": checked,
        "order": order,
        "seeds": sorted(seeds),
        "failures": failures,
    }
    if truncated:
        out["note"] = (f"closure of {len(closure)} apps truncated to {max_apps} "
                       "(seeds + direct dependents first); the pipeline full build "
                       "covers the remainder")
    return out
