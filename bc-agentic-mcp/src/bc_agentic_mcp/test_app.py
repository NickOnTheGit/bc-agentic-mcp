"""test_app — D2: resolve the REAL test extension so scaffolds land where they run.

Deterministic resolution, no guessing:
- a "test app" is an app.json whose dependencies include a Microsoft test library
  (Library Assert / Test Runner / any of the known test-framework names) or whose
  name contains "test";
- object IDs are allocated from the app's OWN idRanges, skipping every ID already
  used by ANY object in that app's .al sources (scan, not convention);
- zero candidates or multiple candidates without a hint => structured refusal that
  lists what was found (fail-closed, never a silent default).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_SKIP_DIRS = {".git", ".vscode", ".alpackages", ".specs", "node_modules", ".venv", "out", "build"}

# Names that identify Microsoft's test framework dependencies (case-insensitive substrings).
_TEST_DEP_MARKERS = (
    "library assert", "test runner", "tests-testlibraries", "test framework",
    "library - assert", "any", "ai test toolkit",
)

_OBJECT_ID_RE = re.compile(
    r"^\s*(?:codeunit|table|page|report|query|xmlport|enum|interface|controladdin|"
    r"pageextension|tableextension|enumextension|reportextension|permissionset)\s+(\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)


def find_app_jsons(project_root: Path, *, max_depth: int = 4) -> List[Path]:
    """Every app.json under the root (bounded depth, noisy dirs skipped), stable order."""
    root = Path(project_root).resolve()
    found: List[Path] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                if child.name.lower() not in _SKIP_DIRS:
                    _walk(child, depth + 1)
            elif child.name == "app.json":
                found.append(child)

    _walk(root, 0)
    return found


def load_app_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def is_test_app(app_json: Dict[str, Any]) -> bool:
    name = str(app_json.get("name", "")).lower()
    if "test" in name:
        return True
    for dep in app_json.get("dependencies", []) or []:
        dep_name = str(dep.get("name", "")).lower()
        if any(marker in dep_name for marker in _TEST_DEP_MARKERS if marker != "any"):
            return True
    return False


def id_ranges(app_json: Dict[str, Any]) -> List[Dict[str, int]]:
    ranges = app_json.get("idRanges") or []
    if not ranges and app_json.get("idRange"):
        ranges = [app_json["idRange"]]
    return [
        {"from": int(r.get("from", 0)), "to": int(r.get("to", 0))}
        for r in ranges
        if int(r.get("from", 0)) > 0
    ]


def used_object_ids(app_dir: Path) -> "set[int]":
    used: "set[int]" = set()
    for al_file in sorted(app_dir.rglob("*.al")):
        if any(part.lower() in _SKIP_DIRS for part in al_file.parts):
            continue
        try:
            text = al_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        used.update(int(m.group(1)) for m in _OBJECT_ID_RE.finditer(text))
    return used


def allocate_object_id(app_dir: Path, ranges: List[Dict[str, int]]) -> Optional[int]:
    """First free ID across the app's ranges (deterministic: lowest wins)."""
    used = used_object_ids(app_dir)
    for r in ranges:
        for candidate in range(r["from"], r["to"] + 1):
            if candidate not in used:
                return candidate
    return None


def resolve_target(project_root: Path, *, hint: Optional[str] = None) -> Dict[str, Any]:
    """Resolve the ONE test app scaffolds must land in.

    ``hint`` (folder path or app-name substring) disambiguates when several exist.
    """
    candidates: List[Dict[str, Any]] = []
    for path in find_app_jsons(project_root):
        data = load_app_json(path)
        if data and is_test_app(data):
            candidates.append({
                "app_name": data.get("name", ""),
                "app_folder": str(path.parent),
                "id_ranges": id_ranges(data),
            })

    if hint:
        hint_lower = hint.replace("\\", "/").lower()
        candidates = [
            c for c in candidates
            if hint_lower in c["app_folder"].replace("\\", "/").lower()
            or hint_lower in c["app_name"].lower()
        ]

    if not candidates:
        return {"status": "none", "candidates": []}
    if len(candidates) > 1:
        return {"status": "ambiguous", "candidates": candidates}

    target = candidates[0]
    ranges = target["id_ranges"]
    object_id = allocate_object_id(Path(target["app_folder"]), ranges) if ranges else None
    return {"status": "resolved", **target, "object_id": object_id}
