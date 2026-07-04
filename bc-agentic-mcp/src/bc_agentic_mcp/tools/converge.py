"""bc_converge — compare implementation against the spec. See spec Section 3.13."""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any

from bc_agentic_mcp.spec_loader import load_spec
from bc_agentic_mcp.tools.analyze import OBJECT_PATTERNS


def _scan_workspace_al_files(project_root: Path, max_files: int = 5000) -> list[dict[str, Any]]:
    """Scan all AL files in the workspace (not only root/src)."""
    objects: list[dict[str, Any]] = []
    for index, al_file in enumerate(sorted(project_root.rglob("*.al"))):
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
                break
    return objects


def _normalize_obj_type(value: str) -> str:
    t = (value or "").strip().lower()
    mapping = {
        "api page": "page",
        "tableextension": "tableextension",
        "pageextension": "pageextension",
        "enumextension": "enumextension",
    }
    return mapping.get(t, t)


def _code_field_token(field_name: str) -> str:
    return "".join(ch for ch in (field_name or "") if ch.isalnum())


async def handle_converge(
    project_root: str,
    spec_name: str,
) -> Dict[str, Any]:
    """Compare what was declared in spec.json against what exists on disk."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name
    spec = load_spec(specs_dir)

    declared_creates = [o for o in spec.get("objects_to_create", []) if o.get("name")]
    declared_modifies = spec.get("objects_to_modify", [])

    declared_create_names = {
        (_normalize_obj_type(o.get("type", "")), str(o.get("name"))) for o in declared_creates
    }

    existing = _scan_workspace_al_files(root)
    existing_names = {
        (_normalize_obj_type(o.get("type", "")), str(o.get("name"))) for o in existing
    }

    missing_creates = [
        f"{t.title()} {n}" for (t, n) in declared_create_names - existing_names
    ]

    missing_modifies: list[str] = []
    data_fields = [d.get("field") for d in spec.get("data_model", []) if d.get("field")]
    for obj in declared_modifies:
        target = obj.get("target")
        if not target:
            missing_modifies.append(f"Modify target missing for {obj.get('type', 'Object')} {obj.get('name', '')}".strip())
            continue
        target_path = root / target
        if not target_path.exists():
            missing_modifies.append(f"Modify target not found: {target}")
            continue
        obj_type = _normalize_obj_type(obj.get("type", ""))
        content = target_path.read_text(encoding="utf-8", errors="replace")
        if obj_type in ("table", "tableextension") and data_fields:
            for field in data_fields:
                token = _code_field_token(field)
                if token and f"field(" not in content:
                    missing_modifies.append(f"{target} missing field declaration for '{field}'")
                    continue
                if token and token not in content:
                    missing_modifies.append(f"{target} missing field '{field}'")
        if obj_type in ("page", "pageextension") and data_fields:
            for field in data_fields:
                token = _code_field_token(field)
                if token and f"Rec.{token}" not in content and f'Rec."{field}"' not in content:
                    missing_modifies.append(f"{target} missing page binding for '{field}'")

    missing = sorted(missing_creates + missing_modifies)

    extra = sorted([f"{t.title()} {n}" for (t, n) in existing_names - declared_create_names])

    declared_count = len(declared_create_names) + len(declared_modifies)
    implemented_count = len(declared_create_names & existing_names) + (
        len(declared_modifies) - len(missing_modifies)
    )
    converged = len(missing) == 0

    return {
        "spec_name": spec_name,
        "converged": converged,
        "declared_count": declared_count,
        "implemented_count": implemented_count,
        "missing": missing,
        "unexpected": extra,
    }
