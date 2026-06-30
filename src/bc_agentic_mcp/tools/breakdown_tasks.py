"""bc_breakdown_tasks — decompose design into dependency-ordered tasks.
See spec Section 3.6.
"""
import json
from pathlib import Path
from typing import Dict, Any, Optional


async def handle_breakdown_tasks(
    project_root: str,
    spec_name: str,
    design_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Decompose design into dependency-ordered implementation tasks."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name

    spec_path = specs_dir / "spec.json"
    spec = json.loads(spec_path.read_text())

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

    return {
        "tasks_path": str(tasks_path),
        "task_count": len(all_tasks),
        "waves": waves,
        "total_estimated_objects": len(spec.get("objects_to_create", []))
        + len(spec.get("objects_to_modify", [])),
    }
