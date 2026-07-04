"""End-to-end integration test for the BC agentic pipeline.

Exercises: init -> analyze -> clarify -> write_spec -> plan_design ->
breakdown_tasks -> request_approval -> submit_decision -> status.
"""
import tempfile
from pathlib import Path
import pytest

from bc_agentic_mcp.tools.init import handle_init
from bc_agentic_mcp.tools.analyze import analyze_module
from bc_agentic_mcp.tools.clarify import handle_clarify
from bc_agentic_mcp.tools.write_spec import handle_write_spec
from bc_agentic_mcp.tools.plan_design import handle_plan_design
from bc_agentic_mcp.tools.breakdown_tasks import handle_breakdown_tasks
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision
from bc_agentic_mcp.tools.status import handle_status


@pytest.fixture
def al_project():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "app.json").write_text('{"name": "Empire", "idRanges": [{"from": 50000, "to": 50099}]}')
        src = root / "src" / "Tables"
        src.mkdir(parents=True)
        (src / "Rental.Table.al").write_text('table 50000 "Rental" { fields { field(1; "No."; Code[20]) } }')
        yield root


@pytest.mark.asyncio
async def test_full_pipeline(al_project):
    root = str(al_project)
    spec_name = "rental-mutation"

    init = await handle_init(project_root=root, module_name="Empire")
    assert init["success"] is True

    analysis = analyze_module(al_project, spec_name=spec_name)
    assert analysis["module_summary"]["object_count"] == 1

    clar = await handle_clarify(
        project_root=root, spec_name=spec_name, context="notify the user and validate the date"
    )
    assert Path(clar["file_path"]).exists()
    assert len(clar["questions"]) >= 1

    spec = await handle_write_spec(
        project_root=root,
        spec_name=spec_name,
        human_bullets="Add field MutationDate to table Rental (id 50000)",
        idempotency_key="k-001",
    )
    assert Path(spec["tdd_path"]).exists()
    assert Path(spec["machine_spec_path"]).exists()

    # Idempotent re-call returns the existing spec.
    spec2 = await handle_write_spec(
        project_root=root,
        spec_name=spec_name,
        human_bullets="Add field MutationDate to table Rental (id 50000)",
        idempotency_key="k-001",
    )
    assert "idempotent" in spec2["summary"]["status"]

    design = await handle_plan_design(project_root=root, spec_name=spec_name)
    assert Path(design["design_path"]).exists()

    tasks = await handle_breakdown_tasks(project_root=root, spec_name=spec_name)
    assert tasks["task_count"] >= 2
    assert len(tasks["waves"]) == 5

    appr = await handle_request_approval(
        project_root=root,
        spec_name=spec_name,
        phase="spec",
        artifact_path=spec["tdd_path"],
        summary="Spec for rental mutation",
        idempotency_key="k-appr-1",
    )
    assert appr["status"] == "pending"

    decision = await handle_submit_decision(
        project_root=root, spec_name=spec_name, phase="spec", decision="approve"
    )
    assert decision["status"] == "approve"
    assert decision["next_action"] == "proceed_to_bc_plan_design"

    approval_file = Path(appr["approval_path"]).read_text()
    assert "**Status:** approve" in approval_file
    assert "- [x] approve" in approval_file

    status = await handle_status(project_root=root)
    assert "summary" in status
