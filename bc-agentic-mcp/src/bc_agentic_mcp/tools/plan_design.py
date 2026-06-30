"""bc_plan_design — generate technical design from spec. See spec Section 3.5."""
import json
from pathlib import Path
from typing import Dict, Any, Optional


async def handle_plan_design(
    project_root: str,
    spec_name: str,
    machine_spec_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate technical design (DESIGN.md + ADRs) from machine spec."""
    root = Path(project_root).resolve()
    specs_dir = root / ".specs" / spec_name

    if not machine_spec_path:
        machine_spec_path = str(specs_dir / "spec.json")
    spec = json.loads(Path(machine_spec_path).read_text())

    design_lines = [
        f"# Technical Design: {spec_name}",
        "",
        "## 1. Architecture Decisions (ADRs)",
        "",
    ]

    adrs = []
    create_count = len(spec.get("objects_to_create", []))
    modify_count = len(spec.get("objects_to_modify", []))
    adrs.append(
        {
            "title": "Object Architecture",
            "decision": f"Create {create_count} new objects, modify {modify_count} existing objects",
            "rationale": "Minimizes blast radius by only touching declared objects",
            "alternatives_considered": [
                "Full refactor (rejected: too risky)",
                "Greenfield extension (rejected: duplicate maintenance)",
            ],
        }
    )

    design_lines.append(f"### ADR 1: {adrs[0]['title']}")
    design_lines.append(f"**Decision:** {adrs[0]['decision']}")
    design_lines.append(f"**Rationale:** {adrs[0]['rationale']}")
    design_lines.append(f"**Alternatives:** {adrs[0]['alternatives_considered']}")
    design_lines.append("")

    subscribers = spec.get("event_subscribers", [])
    if subscribers:
        adrs.append(
            {
                "title": "Integration Pattern",
                "decision": f"Use {len(subscribers)} event subscribers for integration",
                "rationale": "Decouples business logic from triggering events",
                "alternatives_considered": [
                    "Direct API calls (rejected: tight coupling)",
                    "Job queue polling (rejected: latency)",
                ],
            }
        )
        design_lines.append(f"### ADR 2: {adrs[1]['title']}")
        design_lines.append(f"**Decision:** {adrs[1]['decision']}")
        design_lines.append("")

    design_lines.append("## 2. Dependency Graph")
    design_lines.append("")
    objects_to_create = spec.get("objects_to_create", [])
    objects_to_modify = spec.get("objects_to_modify", [])

    nodes = []
    edges = []
    for i, obj in enumerate(objects_to_create):
        nodes.append({"id": f"new_{i}", "type": obj["type"], "name": obj["name"]})
    for i, obj in enumerate(objects_to_modify):
        nodes.append(
            {"id": f"mod_{i}", "type": obj["type"], "name": obj.get("target", obj.get("name"))}
        )

    design_lines.append("```mermaid")
    design_lines.append("graph TD")
    for node in nodes:
        design_lines.append(f"    {node['id']}[{node['type']}: {node['name']}]")
    design_lines.append("```")
    design_lines.append("")

    design_lines.append("## 3. Data Flow")
    design_lines.append("")
    for i, rule in enumerate(spec.get("business_rules", [])):
        design_lines.append(f"### Flow {i+1}: {rule.get('description', rule.get('id', ''))}")
        design_lines.append("1. Trigger: (to be filled by AI model)")
        design_lines.append("2. Validation: (to be filled by AI model)")
        design_lines.append("3. Processing: (to be filled by AI model)")
        design_lines.append("4. Outcome: (to be filled by AI model)")
        design_lines.append("")

    design_lines.append("## 4. Error Handling Strategy")
    design_lines.append("")
    design_lines.append("| Error | Detection | User Message | Recovery |")
    design_lines.append("|-------|-----------|-------------|----------|")
    design_lines.append("| (to be filled) | (to be filled) | (to be filled) | (to be filled) |")
    design_lines.append("")

    design_lines.append("## 5. Extension Points")
    design_lines.append("")
    design_lines.append("(to be filled by AI model — where future changes are anticipated)")
    design_lines.append("")

    design_path = specs_dir / "DESIGN.md"
    design_path.write_text("\n".join(design_lines), encoding="utf-8")

    return {
        "design_path": str(design_path),
        "adrs": adrs,
        "data_flow": {"steps": []},
        "dependency_graph": {"nodes": nodes, "edges": edges},
    }
