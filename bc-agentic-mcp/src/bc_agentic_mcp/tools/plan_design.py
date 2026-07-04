"""bc_plan_design — generate technical design from spec. See spec Section 3.5."""
import json
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, Optional
from bc_agentic_mcp.spec_loader import validate_spec_contract


def _derive_flow_steps(rule: Dict[str, Any], spec: Dict[str, Any], is_api: bool = True) -> Dict[str, str]:
    desc = str(rule.get("description", "")).lower()
    api_targets = [obj.get("target", obj.get("name", "")) for obj in spec.get("objects_to_modify", [])]
    api_targets = [t for t in api_targets if t]
    fields = rule.get("fields", [])

    trigger = (
        "External application sends GET/PATCH requests to the API endpoints."
        if is_api else
        "The change is applied at build/upgrade time; there is no external runtime trigger."
    )

    if fields:
        validation = (
            "Validate request payload against API field mappings and AL data types for: "
            + ", ".join(fields)
            + "."
        )
    else:
        validation = "Validate payload shape and field permissions against existing API metadata."

    if "read and update" in desc or rule.get("mode") == "read-write":
        processing = (
            "Map API attributes to Rec fields on target API pages and persist updates through standard AL record handling."
        )
        outcome = "Requested fields are readable and updatable via API without changing unrelated rentalMutation behavior."
    elif "expose" in desc:
        processing = "Add/verify API field declarations on target pages and bind each attribute to the corresponding Rec field."
        outcome = "New subprocess on-hold attributes are exposed consistently across all supported API versions."
    else:
        processing = "Apply the rule using existing API page mappings and module boundaries."
        outcome = "Business rule is enforced and API behavior remains backward compatible."

    if api_targets:
        processing += " Targets: " + ", ".join(api_targets) + "."

    return {
        "trigger": trigger,
        "validation": validation,
        "processing": processing,
        "outcome": outcome,
    }


async def handle_plan_design(
    project_root: str,
    spec_name: str,
    machine_spec_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate technical design (DESIGN.md + ADRs) from machine spec."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root) / spec_name

    if not machine_spec_path:
        machine_spec_path = str(specs_dir / "spec.json")
    spec = json.loads(Path(machine_spec_path).read_text())

    contract_issues = validate_spec_contract(spec, strict_schema=True)
    if contract_issues:
        return {
            "status": "blocked_invalid_spec_contract",
            "reason": "Invalid grounded spec contract: " + "; ".join(contract_issues),
            "design_path": None,
            "adrs": [],
        }

    # Fail-closed: do not emit a generic design from an ungrounded spec.
    if spec.get("status") == "needs_grounding":
        return {
            "status": "blocked_needs_grounding",
            "reason": ("Objects could not be grounded in the repo. Resolve the object "
                       "name(s)/id(s) (fix the item or run bc_read_code_context) and "
                       "regenerate the spec before designing."),
            "open_questions": spec.get("open_questions", []),
            "design_path": None,
            "adrs": [],
        }

    unnamed_creates = [o for o in spec.get("objects_to_create", []) if not o.get("name")]
    if unnamed_creates:
        return {
            "status": "blocked_needs_grounding",
            "reason": ("At least one object-to-create is abstract (missing name). "
                       "Resolve concrete object identities before generating design."),
            "open_questions": spec.get("open_questions", []),
            "design_path": None,
            "adrs": [],
        }

    _obj_types = " ".join(
        str(o.get("type", "")).lower()
        for o in spec.get("objects_to_modify", []) + spec.get("objects_to_create", [])
    )
    is_api = ("api" in spec.get("work_types", [])) or ("api" in _obj_types)

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

    # A schema, not a node dump (human feedback 2026-07-04: orphan nodes with raw
    # backslash paths rendered as nothing). Labels are OBJECT NAMES (filename stem as
    # fallback), quoted for mermaid safety; edges: the item touches every object, and
    # each test codeunit exercises every production object.
    def _label(obj) -> str:
        name = obj.get("name") or Path(str(obj.get("target", ""))).name.split(".")[0] or "?"
        return str(name).replace('"', "'")

    def _is_test(obj) -> bool:
        target = str(obj.get("target", "")).lower()
        return ("test" in target.split("\\")[0] + target.split("/")[0]
                or "\\testapp\\" in target or "/testapp/" in target
                or "tests\\" in target or "tests/" in target
                or str(obj.get("name", "")).endswith(("FDNT", "SANT", "EMPT", "HSGT")))

    nodes = []
    for i, obj in enumerate(objects_to_create):
        nodes.append({"id": f"new_{i}", "type": obj["type"], "label": _label(obj),
                      "test": _is_test(obj)})
    for i, obj in enumerate(objects_to_modify):
        nodes.append({"id": f"mod_{i}", "type": obj["type"], "label": _label(obj),
                      "test": _is_test(obj)})

    design_lines.append("```mermaid")
    design_lines.append("graph TD")
    design_lines.append(f'    item(["{spec_name}"])')
    for node in nodes:
        shape = ("{{" + f'"{node["type"]}: {node["label"]}"' + "}}") if node["test"] \
            else f'["{node["type"]}: {node["label"]}"]'
        design_lines.append(f"    {node['id']}{shape}")
    prod_nodes = [n for n in nodes if not n["test"]]
    for node in nodes:
        verb = "creates" if node["id"].startswith("new_") else "modifies"
        design_lines.append(f"    item -->|{verb}| {node['id']}")
    for tnode in (n for n in nodes if n["test"]):
        for pnode in prod_nodes:
            design_lines.append(f"    {tnode['id']} -.->|tests| {pnode['id']}")
    design_lines.append("```")
    design_lines.append("")

    design_lines.append("## 3. Data Flow")
    design_lines.append("")
    for i, rule in enumerate(spec.get("business_rules", [])):
        design_lines.append(f"### Flow {i+1}: {rule.get('description', rule.get('id', ''))}")
        steps = _derive_flow_steps(rule, spec, is_api)
        design_lines.append(f"1. Trigger: {steps['trigger']}")
        design_lines.append(f"2. Validation: {steps['validation']}")
        design_lines.append(f"3. Processing: {steps['processing']}")
        design_lines.append(f"4. Outcome: {steps['outcome']}")
        design_lines.append("")

    design_lines.append("## 4. Error Handling Strategy")
    design_lines.append("")
    if is_api:
        design_lines.append("| Error | Detection | User Message | Recovery |")
        design_lines.append("|-------|-----------|-------------|----------|")
        design_lines.append("| Invalid field value | API request binding/type check | \"The provided value is not valid for this field.\" | Reject the request; record stays unchanged |")
        design_lines.append("| Unknown attribute | API metadata mismatch | \"The attribute is not recognized.\" | Ignore unknown attributes per API contract |")
        design_lines.append("| Concurrency conflict | Record version mismatch on update | \"The record was modified by another process.\" | Client refetches and retries |")
    else:
        design_lines.append("| Error | Detection | Handling |")
        design_lines.append("|-------|-----------|----------|")
        design_lines.append("| Invalid field value | AL type/length check on assignment | Reject; record left unchanged |")
        design_lines.append("| Referenced object missing | Compile-time (object not found) | Resolve the object/id before build |")
        design_lines.append("| Upgrade re-run | Upgrade tag already set | Idempotent: Get-before-Modify, no duplicate writes |")
    design_lines.append("")

    design_lines.append("## 5. Extension Points")
    design_lines.append("")
    if is_api:
        design_lines.append("- Additional subprocess fields can be exposed by extending the same API field mappings.")
        design_lines.append("- New API versions follow the identical field-mapping pattern as existing versions.")
        design_lines.append("- Validation rules can be added later via OnValidate without changing the API contract.")
    else:
        design_lines.append("- Further fields follow the same table/page pattern.")
        design_lines.append("- Business logic can be added via OnValidate / table triggers without changing the schema.")
        design_lines.append("- The data upgrade is idempotent and safe to re-run.")
    design_lines.append("")

    design_path = specs_dir / "DESIGN.md"
    design_path.write_text("\n".join(design_lines), encoding="utf-8")

    return {
        "design_path": str(design_path),
        "adrs": adrs,
        "data_flow": {"steps": []},
        "dependency_graph": {"nodes": nodes},
    }
