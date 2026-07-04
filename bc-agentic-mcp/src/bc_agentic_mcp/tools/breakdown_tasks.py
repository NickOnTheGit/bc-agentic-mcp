"""bc_breakdown_tasks — decompose design into dependency-ordered tasks.
See spec Section 3.6.
"""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, Optional
from bc_agentic_mcp.spec_loader import validate_spec_contract


async def handle_breakdown_tasks(
    project_root: str,
    spec_name: str,
    design_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Decompose design into dependency-ordered implementation tasks."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name

    spec_path = specs_dir / "spec.json"
    spec = json.loads(spec_path.read_text())

    contract_issues = validate_spec_contract(spec, strict_schema=True)
    if contract_issues:
        return {
            "status": "blocked_invalid_spec_contract",
            "reason": "Invalid grounded spec contract: " + "; ".join(contract_issues),
            "tasks_path": None,
            "review_path": None,
            "task_count": 0,
            "waves": [],
        }

    # Fail-closed: do not emit generic tasks from an ungrounded spec.
    if spec.get("status") == "needs_grounding":
        return {
            "status": "blocked_needs_grounding",
            "reason": ("Objects could not be grounded in the repo. Resolve the object "
                       "name(s)/id(s) and regenerate the spec before breaking down tasks."),
            "open_questions": spec.get("open_questions", []),
            "tasks_path": None,
            "review_path": None,
            "task_count": 0,
            "waves": [],
        }

    unnamed_creates = [o for o in spec.get("objects_to_create", []) if not o.get("name")]
    if unnamed_creates:
        return {
            "status": "blocked_needs_grounding",
            "reason": ("At least one object-to-create is abstract (missing name). "
                       "Resolve concrete object identities before breaking down tasks."),
            "open_questions": spec.get("open_questions", []),
            "tasks_path": None,
            "review_path": None,
            "task_count": 0,
            "waves": [],
        }

    task_id = 1

    # Wave 1: Foundation — tables and enums (no dependencies)
    wave_1 = []
    for obj in spec.get("objects_to_create", []):
        if obj["type"] in ("Table", "Enum"):
            tid = f"T-{task_id:03d}"
            wave_1.append(
                {
                    "id": tid,
                    "wave": 1,
                    "dependencies": [],
                    "object_to_create": obj,
                    "description": f"Create {obj['type']} {obj['name']}",
                    "acceptance_criteria": [f"{obj['type']} {obj['name']} compiles without errors"],
                    "estimated_lines": 50 if obj["type"] == "Table" else 20,
                }
            )
            task_id += 1

    # Include table/enum modifications in wave 1 (e.g. add a field to an existing table).
    _field_names = [f.get("field") for f in spec.get("data_model", []) if f.get("field")]
    for obj in spec.get("objects_to_modify", []):
        obj_type = str(obj.get("type", "")).lower()
        if obj_type in ("table", "tableextension", "enum", "enumextension"):
            tid = f"T-{task_id:03d}"
            target = obj.get("target", obj.get("name", "existing table"))
            ac = [f"{target} compiles"]
            if _field_names:
                ac.append(f"Field(s) {', '.join(_field_names)} added with the specified type and editability")
            else:
                ac.append(obj.get("change", "Apply the requested schema change"))
            wave_1.append(
                {
                    "id": tid,
                    "wave": 1,
                    "dependencies": [],
                    "object_to_modify": obj,
                    "description": f"Modify {obj.get('type', 'Table')} {target}",
                    "acceptance_criteria": ac,
                    "estimated_lines": 30,
                }
            )
            task_id += 1

    # Wave 2: Business logic — codeunits
    wave_2 = []
    wave_1_ids = [t["id"] for t in wave_1]
    for obj in spec.get("objects_to_create", []):
        if obj["type"] == "Codeunit":
            tid = f"T-{task_id:03d}"
            wave_2.append(
                {
                    "id": tid,
                    "wave": 2,
                    "dependencies": wave_1_ids,
                    "object_to_create": obj,
                    "description": f"Create Codeunit {obj['name']}",
                    "acceptance_criteria": [
                        f"Codeunit {obj['name']} compiles",
                        "All business rules implemented",
                    ],
                    "estimated_lines": 100,
                }
            )
            task_id += 1

    # Include modifications that are codeunit-like in wave 2
    for obj in spec.get("objects_to_modify", []):
        obj_type = str(obj.get("type", "")).lower()
        if "codeunit" in obj_type:
            tid = f"T-{task_id:03d}"
            target = obj.get("target", obj.get("name", "existing codeunit"))
            wave_2.append(
                {
                    "id": tid,
                    "wave": 2,
                    "dependencies": wave_1_ids,
                    "object_to_modify": obj,
                    "description": f"Modify {obj.get('type', 'Codeunit')} {target}",
                    "acceptance_criteria": [
                        f"{target} compiles",
                        "Requested behavior is implemented",
                    ],
                    "estimated_lines": 60,
                }
            )
            task_id += 1

    # Wave 3: UI — pages
    wave_3 = []
    wave_1_2_ids = [t["id"] for t in (wave_1 + wave_2)]
    for obj in spec.get("objects_to_create", []):
        if obj["type"] in ("Page", "PageExtension"):
            tid = f"T-{task_id:03d}"
            wave_3.append(
                {
                    "id": tid,
                    "wave": 3,
                    "dependencies": wave_1_2_ids,
                    "object_to_create": obj,
                    "description": f"Create {obj['type']} {obj['name']}",
                    "acceptance_criteria": [f"{obj['type']} {obj['name']} compiles"],
                    "estimated_lines": 80,
                }
            )
            task_id += 1

    # Include page/API modifications in wave 3
    for obj in spec.get("objects_to_modify", []):
        obj_type = str(obj.get("type", "")).lower()
        if "page" in obj_type or "api" in obj_type:
            tid = f"T-{task_id:03d}"
            target = obj.get("target", obj.get("name", "existing page"))
            change = obj.get("change", "Apply requested field and behavior updates")
            wave_3.append(
                {
                    "id": tid,
                    "wave": 3,
                    "dependencies": wave_1_2_ids,
                    "object_to_modify": obj,
                    "description": f"Modify {obj.get('type', 'Page')} {target}",
                    "acceptance_criteria": [
                        f"{target} compiles",
                        change,
                    ],
                    "estimated_lines": 50,
                }
            )
            task_id += 1

    # Wave 4: Integration — event subscribers
    wave_4 = []
    all_prev_ids = [t["id"] for t in (wave_1 + wave_2 + wave_3)]
    for sub in spec.get("event_subscribers", []):
        tid = f"T-{task_id:03d}"
        wave_4.append(
            {
                "id": tid,
                "wave": 4,
                "dependencies": all_prev_ids,
                "event_subscriber": sub,
                "description": f"Implement event subscriber: {sub.get('event', sub.get('purpose', ''))}",
                "acceptance_criteria": ["Event subscriber compiles", "Trigger condition verified"],
                "estimated_lines": 30,
            }
        )
        task_id += 1

    # Wave 5: Tests + Upgrade
    wave_5 = [
        {
            "id": f"T-{task_id:03d}",
            "wave": 5,
            "dependencies": [t["id"] for t in (wave_1 + wave_2 + wave_3 + wave_4)],
            "description": "Generate test codeunits",
            "acceptance_criteria": ["All tests compile", "Critical path covered"],
            "estimated_lines": 200,
        },
    ]
    # Upgrade codeunit task only when the item actually involves a data upgrade.
    _needs_upgrade = "upgrade" in spec.get("work_types", []) or any(
        "upgrade" in str(o.get("subtype", "")).lower()
        or "upgrade" in str(o.get("name", "")).lower()
        for o in spec.get("objects_to_create", [])
    )
    if _needs_upgrade:
        task_id += 1
        wave_5.append(
            {
                "id": f"T-{task_id:03d}",
                "wave": 5,
                "dependencies": [],
                "description": "Generate upgrade codeunit",
                "acceptance_criteria": ["Upgrade codeunit compiles", "Triggers implemented"],
                "estimated_lines": 80,
            }
        )

    all_tasks = wave_1 + wave_2 + wave_3 + wave_4 + wave_5
    waves = [
        {"wave_number": 1, "tasks": [t["id"] for t in wave_1], "description": "Foundation — tables, enums"},
        {"wave_number": 2, "tasks": [t["id"] for t in wave_2], "description": "Business logic — codeunits"},
        {"wave_number": 3, "tasks": [t["id"] for t in wave_3], "description": "UI — pages"},
        {"wave_number": 4, "tasks": [t["id"] for t in wave_4], "description": "Integration — event subscribers"},
        {"wave_number": 5, "tasks": [t["id"] for t in wave_5], "description": "Tests + upgrade"},
    ]

    tasks_lines = [
        f"# Implementation Tasks: {spec_name}",
        "",
        f"Total tasks: {len(all_tasks)}",
        f"Total objects: {len(spec.get('objects_to_create', [])) + len(spec.get('objects_to_modify', []))}",
        "",
    ]
    for wave in waves:
        tasks_lines.append(f"## Wave {wave['wave_number']}: {wave['description']}")
        tasks_lines.append("")
        for tid in wave["tasks"]:
            t = next(t for t in all_tasks if t["id"] == tid)
            tasks_lines.append(f"### {t['id']}: {t['description']}")
            tasks_lines.append(f"- **Wave:** {t['wave']}")
            tasks_lines.append(f"- **Dependencies:** {', '.join(t['dependencies']) or 'None'}")
            tasks_lines.append("- **Acceptance Criteria:**")
            for ac in t.get("acceptance_criteria", []):
                tasks_lines.append(f"  - [ ] {ac}")
            tasks_lines.append("")

    tasks_path = specs_dir / "TASKS.md"
    tasks_path.write_text("\n".join(tasks_lines), encoding="utf-8")

    review_lines = [
        f"# Spec Review: {spec_name}",
        "",
        "## Review Status",
        "",
        "- [ ] Requirements reviewed",
        "- [ ] Design reviewed",
        "- [ ] Tasks reviewed",
        "- [ ] Approved for implementation",
        "",
        "## Source Artifacts",
        "",
        f"- TDD: {specs_dir / 'TDD.md'}",
        f"- Spec: {spec_path}",
        f"- Design: {design_path or specs_dir / 'DESIGN.md'}",
        f"- Tasks: {tasks_path}",
        "",
        "## Human Requirements",
        "",
    ]

    tdd_path = specs_dir / "TDD.md"
    if tdd_path.exists():
        review_lines.append(tdd_path.read_text(encoding="utf-8"))
    else:
        review_lines.append("(missing TDD.md)")

    review_lines.extend([
        "",
        "## Machine Spec",
        "",
        json.dumps(spec, indent=2),
        "",
        "## Design Summary",
        "",
    ])

    design_file = Path(design_path) if design_path else specs_dir / "DESIGN.md"
    if design_file.exists():
        review_lines.append(design_file.read_text(encoding="utf-8"))
    else:
        review_lines.append("(missing DESIGN.md)")

    review_lines.extend([
        "",
        "## Implementation Tasks",
        "",
        tasks_path.read_text(encoding="utf-8"),
        "",
        "## Approval Notes",
        "",
        "Use this file to confirm the scope before implementation starts.",
    ])

    review_path = specs_dir / "REVIEW.md"
    review_path.write_text("\n".join(review_lines), encoding="utf-8")

    return {
        "tasks_path": str(tasks_path),
        "review_path": str(review_path),
        "task_count": len(all_tasks),
        "waves": waves,
        "total_estimated_objects": len(spec.get("objects_to_create", []))
        + len(spec.get("objects_to_modify", [])),
    }
