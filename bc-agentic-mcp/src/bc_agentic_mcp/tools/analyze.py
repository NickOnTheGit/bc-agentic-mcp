"""bc_analyze_module — read AL module structure and extract patterns.
See spec Section 3.2.
"""
import json
import re
from pathlib import Path
from typing import Dict, Any, List


# AL object type patterns — matches object declarations
OBJECT_PATTERNS = {
    "TableExtension": re.compile(r'tableextension\s+(\d+)\s+"([^"]+)"\s+extends\s+"([^"]+)"'),
    "PageExtension": re.compile(r'pageextension\s+(\d+)\s+"([^"]+)"\s+extends\s+"([^"]+)"'),
    "EnumExtension": re.compile(r'enumextension\s+(\d+)\s+"([^"]+)"\s+extends\s+"([^"]+)"'),
    "Table": re.compile(r'\btable\s+(\d+)\s+"([^"]+)"'),
    "Page": re.compile(r'\bpage\s+(\d+)\s+"([^"]+)"'),
    "Codeunit": re.compile(r'\bcodeunit\s+(\d+)\s+"([^"]+)"'),
    "Enum": re.compile(r'\benum\s+(\d+)\s+"([^"]+)"'),
    "Report": re.compile(r'\breport\s+(\d+)\s+"([^"]+)"'),
    "XmlPort": re.compile(r'\bxmlport\s+(\d+)\s+"([^"]+)"'),
}

# File suffix conventions
TYPE_SUFFIXES = {
    "Table": ".Table.al",
    "Page": ".Page.al",
    "Codeunit": ".Codeunit.al",
    "Enum": ".Enum.al",
    "Report": ".Report.al",
    "XmlPort": ".XmlPort.al",
    "TableExtension": ".TableExt.al",
    "PageExtension": ".PageExt.al",
    "EnumExtension": ".EnumExt.al",
}

EVENT_SUBSCRIBER_PATTERN = re.compile(r'\[EventSubscriber\(.*?\)\]', re.DOTALL)
ERROR_PATTERNS = re.compile(r'\b(Error|TestField|AssertError|FieldError)\(', re.MULTILINE)


def scan_al_files(project_root: Path) -> List[Dict[str, Any]]:
    """Scan all .al files in the project and extract object metadata."""
    objects: List[Dict[str, Any]] = []
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return objects

    for al_file in sorted(src_dir.rglob("*.al")):
        content = al_file.read_text(encoding="utf-8", errors="replace")
        rel_path = str(al_file.relative_to(project_root))

        for obj_type, pattern in OBJECT_PATTERNS.items():
            match = pattern.search(content)
            if match:
                obj = {
                    "type": obj_type,
                    "id": int(match.group(1)),
                    "name": match.group(2),
                    "path": rel_path,
                    "line_count": len(content.splitlines()),
                }
                if obj_type.endswith("Extension") and match.lastindex and match.lastindex >= 3:
                    obj["extends"] = match.group(3)
                objects.append(obj)
                break  # one object type per file

    return objects


def extract_naming_conventions(objects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract naming conventions from scanned objects."""
    conventions: Dict[str, Any] = {
        "suffixes": {},
        "prefixes": set(),
        "id_ranges": {},
    }

    for obj in objects:
        obj_type = obj["type"]
        if obj_type in TYPE_SUFFIXES:
            conventions["suffixes"].setdefault(obj_type, TYPE_SUFFIXES[obj_type])

        conventions["id_ranges"].setdefault(obj_type, {"min": obj["id"], "max": obj["id"]})
        conventions["id_ranges"][obj_type]["min"] = min(
            conventions["id_ranges"][obj_type]["min"], obj["id"]
        )
        conventions["id_ranges"][obj_type]["max"] = max(
            conventions["id_ranges"][obj_type]["max"], obj["id"]
        )

    return conventions


def find_event_subscribers(project_root: Path) -> List[Dict[str, str]]:
    """Find event subscribers in the project."""
    subscribers: List[Dict[str, str]] = []
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return subscribers

    for al_file in sorted(src_dir.rglob("*.al")):
        content = al_file.read_text(encoding="utf-8", errors="replace")
        for match in EVENT_SUBSCRIBER_PATTERN.finditer(content):
            subscribers.append(
                {
                    "file": str(al_file.relative_to(project_root)),
                    "subscriber": match.group(0).strip(),
                }
            )

    return subscribers


def find_similar_modules(
    project_root: Path, current_objects: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Find sibling modules with similar object type composition."""
    parent = Path(project_root).parent
    similar: List[Dict[str, Any]] = []

    if not parent.exists():
        return similar

    for sibling in parent.iterdir():
        if not sibling.is_dir() or sibling.resolve() == Path(project_root).resolve():
            continue
        sibling_app = sibling / "app.json"
        if not sibling_app.exists():
            continue
        sibling_objects = scan_al_files(sibling)
        if sibling_objects:
            current_types = {o["type"] for o in current_objects}
            sibling_types = {o["type"] for o in sibling_objects}
            overlap = current_types & sibling_types
            if len(overlap) >= 2:
                similar.append(
                    {
                        "path": str(sibling.relative_to(parent)),
                        "relevance_score": len(overlap) / max(len(current_types), 1),
                        "why_similar": f"Shares {len(overlap)} object types: {', '.join(sorted(overlap))}",
                        "object_count": len(sibling_objects),
                    }
                )

    return sorted(similar, key=lambda m: m["relevance_score"], reverse=True)


def analyze_module(
    module_path: Path,
    spec_name: str = None,
    depth: str = "basic",
) -> Dict[str, Any]:
    """Analyze an AL module and return structured findings."""
    root = Path(module_path)
    objects = scan_al_files(root)
    conventions = extract_naming_conventions(objects)
    subscribers = find_event_subscribers(root)
    similar = find_similar_modules(root, objects)

    result: Dict[str, Any] = {
        "module_summary": {
            "name": root.name,
            "path": str(root),
            "object_count": len(objects),
            "line_count": sum(o["line_count"] for o in objects),
        },
        "objects": objects,
        "naming_conventions": conventions,
        "patterns": {
            "event_subscribers": subscribers,
            "extension_patterns": [o for o in objects if o["type"].endswith("Extension")],
        },
        "dependencies": {},
        "similar_implementations": similar,
        "convention_deviations": [],
    }

    for obj in objects:
        expected_suffix = TYPE_SUFFIXES.get(obj["type"])
        if expected_suffix and not obj["path"].endswith(expected_suffix):
            result["convention_deviations"].append(
                {
                    "file": obj["path"],
                    "rule": f"{obj['type']} file should end with {expected_suffix}",
                }
            )

    app_json = root / "app.json"
    if app_json.exists():
        try:
            app_data = json.loads(app_json.read_text())
            result["dependencies"] = {
                "app_json_deps": app_data.get("dependencies", []),
                "id_ranges": app_data.get("idRanges", []),
                "runtime": app_data.get("runtime"),
                "target": app_data.get("target"),
            }
        except json.JSONDecodeError:
            pass

    if spec_name:
        specs_dir = root / ".specs" / spec_name
        specs_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = specs_dir / "analysis.md"
        analysis_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    return result
