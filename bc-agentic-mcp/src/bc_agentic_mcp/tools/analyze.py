"""bc_analyze_module — read AL module structure and extract patterns.
See spec Section 3.2.
"""
import json
import re
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, List

from bc_agentic_mcp.al_mcp_client import analyze_project as analyze_project_with_microsoft


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


def scan_al_files(project_root: Path, max_files: int = 1000) -> List[Dict[str, Any]]:
    """Scan all .al files in the project and extract object metadata."""
    objects: List[Dict[str, Any]] = []
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return objects

    for index, al_file in enumerate(sorted(src_dir.rglob("*.al"))):
        if index >= max_files:
            break
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


def find_event_subscribers(project_root: Path, max_files: int = 1000) -> List[Dict[str, str]]:
    """Find event subscribers in the project."""
    subscribers: List[Dict[str, str]] = []
    src_dir = Path(project_root) / "src"
    if not src_dir.exists():
        return subscribers

    for index, al_file in enumerate(sorted(src_dir.rglob("*.al"))):
        if index >= max_files:
            break
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
    project_root: Path,
    current_objects: List[Dict[str, Any]],
    max_sibling_modules: int = 12,
) -> List[Dict[str, Any]]:
    """Find sibling modules with similar object type composition."""
    parent = Path(project_root).parent
    similar: List[Dict[str, Any]] = []

    if not parent.exists():
        return similar

    for sibling_index, sibling in enumerate(parent.iterdir()):
        if sibling_index >= max_sibling_modules:
            break
        if not sibling.is_dir() or sibling.resolve() == Path(project_root).resolve():
            continue
        sibling_app = sibling / "app.json"
        if not sibling_app.exists():
            continue
        sibling_objects = scan_al_files(sibling, max_files=250)
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


def _analyze_module_local(
    module_path: Path,
    spec_name: str = None,
    depth: str = "basic",
    max_files: int = 1000,
    max_sibling_modules: int = 12,
) -> Dict[str, Any]:
    """Analyze an AL module and return structured findings."""
    root = Path(module_path)
    objects = scan_al_files(root, max_files=max_files)
    conventions = extract_naming_conventions(objects)
    subscribers = find_event_subscribers(root, max_files=max_files)
    depth_key = (depth or "basic").lower()
    similar = []
    if depth_key != "basic":
        similar = find_similar_modules(root, objects, max_sibling_modules=max_sibling_modules)

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
        specs_dir = specs_root(root) / spec_name
        specs_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = specs_dir / "analysis.md"
        analysis_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    return result


def analyze_module(
    module_path: Path,
    spec_name: str = None,
    depth: str = "basic",
    max_files: int = 1000,
    max_sibling_modules: int = 12,
) -> Dict[str, Any]:
    """Synchronous local fallback for tests and offline use."""
    return _analyze_module_local(
        module_path=module_path,
        spec_name=spec_name,
        depth=depth,
        max_files=max_files,
        max_sibling_modules=max_sibling_modules,
    )


async def handle_analyze_module(
    module_path: Path,
    spec_name: str = None,
    depth: str = "basic",
    max_files: int = 1000,
    max_sibling_modules: int = 12,
) -> Dict[str, Any]:
    """Primary analyzer that prefers Microsoft's AL MCP backend."""
    root = Path(module_path)
    try:
        microsoft_result = await analyze_project_with_microsoft(root, depth=depth)
        local_result = _analyze_module_local(
            module_path=root,
            spec_name=None,
            depth=depth,
            max_files=max_files,
            max_sibling_modules=max_sibling_modules,
        )
        result = {
            **local_result,
            "module_summary": {
                **local_result["module_summary"],
                **microsoft_result.summary,
                "analysis_backend": "microsoft-al-mcp",
            },
            "dependencies": {
                **local_result.get("dependencies", {}),
                **microsoft_result.dependencies,
            },
            "diagnostics": microsoft_result.diagnostics,
            "analysis_backend": "microsoft-al-mcp",
        }
    except Exception:
        result = _analyze_module_local(
            module_path=root,
            spec_name=None,
            depth=depth,
            max_files=max_files,
            max_sibling_modules=max_sibling_modules,
        )
        result["analysis_backend"] = "local-fallback"

    if spec_name:
        specs_dir = specs_root(root) / spec_name
        specs_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = specs_dir / "analysis.md"
        analysis_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    return result
