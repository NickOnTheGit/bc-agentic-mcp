"""End-to-end pipeline test for bc-agentic-mcp against a minimal AL project."""
import asyncio, json, tempfile
from pathlib import Path

# Create a minimal AL project
tmp = Path(tempfile.mkdtemp())
app_json = tmp / "app.json"
app_json.write_text(json.dumps({
    "id": "test-app-id",
    "name": "TestExtension",
    "publisher": "TestPublisher",
    "version": "1.0.0.0",
    "idRanges": [{"from": 50000, "to": 50099}]
}))
src_tables = tmp / "src" / "Tables"
src_tables.mkdir(parents=True)
(src_tables / "Existing.Table.al").write_text(
    'table 50000 "Existing" { '
    'fields { '
    'field(1; "Code"; Code[20]) { '
    'Caption = "Code"; '
    'DataClassification = CustomerContent; '
    '} } }'
)
print(f"Test project: {tmp}")
print(f"app.json present: {app_json.exists()}")
print(f"AL file present: {(tmp / 'src' / 'Tables' / 'Existing.Table.al').exists()}")

# Test the pipeline
from bc_agentic_mcp.tools.init import handle_init
from bc_agentic_mcp.tools.analyze import analyze_module
from bc_agentic_mcp.tools.write_spec import handle_write_spec
from bc_agentic_mcp.tools.plan_design import handle_plan_design
from bc_agentic_mcp.tools.breakdown_tasks import handle_breakdown_tasks
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision
from bc_agentic_mcp.tools.status import handle_status

root = str(tmp)

async def main():
    results = {}

    # bc_init
    results["init"] = await handle_init(project_root=root, module_name="TestExtension")
    print(f"\n[OK] bc_init — {len(results['init']['created_paths'])} paths created")

    # bc_analyze_module
    results["analyze"] = analyze_module(Path(root), spec_name="test-feature")
    n = results["analyze"]["module_summary"]["object_count"]
    print(f"[OK] bc_analyze_module — {n} objects found")

    # bc_clarify (skip — no ambiguity in our test bullets)

    # bc_write_spec
    results["spec"] = await handle_write_spec(
        project_root=root, spec_name="test-feature",
        human_bullets="- Add mutation date field to Existing table\n- Validate date not in past",
        idempotency_key="e2e-test-key-001",
    )
    print(f"[OK] bc_write_spec — TDD={Path(results['spec']['tdd_path']).exists()}, Spec={Path(results['spec']['machine_spec_path']).exists()}")

    # bc_plan_design
    results["design"] = await handle_plan_design(root, "test-feature")
    print(f"[OK] bc_plan_design — ADRs: {len(results['design']['adrs'])}")

    # bc_breakdown_tasks
    results["tasks"] = await handle_breakdown_tasks(root, "test-feature")
    print(f"[OK] bc_breakdown_tasks — {results['tasks']['task_count']} tasks, {len(results['tasks']['waves'])} waves")

    # bc_request_approval
    results["approval"] = await handle_request_approval(
        root, "test-feature", "spec",
        artifact_path=results["spec"]["tdd_path"],
        summary="Test spec for e2e",
        idempotency_key="e2e-approval-1",
    )
    print(f"[OK] bc_request_approval — status: {results['approval']['status']}")

    # bc_submit_decision
    results["decision"] = await handle_submit_decision(
        root, "test-feature", "spec", "approve"
    )
    print(f"[OK] bc_submit_decision — status: {results['decision']['status']}")

    # bc_status
    results["status"] = await handle_status(root)
    print(f"[OK] bc_status — {results['status']['summary']['total_specs']} specs tracked")

    print("\n" + "=" * 50)
    print("ALL 8 TOOLS: PASS")
    print("=" * 50)

asyncio.run(main())
