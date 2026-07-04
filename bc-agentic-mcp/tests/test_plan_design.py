"""Tests for bc_plan_design."""
import json
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.tools.plan_design import handle_plan_design


@pytest.fixture
def spec_project():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-spec"
        specs_dir.mkdir(parents=True)
        spec = {
            "schema_version": "2.0",
            "spec_id": "test-spec",
            "spec_name": "test-spec",
            "summary": {"goal": "test", "in_scope": [], "out_of_scope": []},
            "objects_to_create": [
                {"type": "Table", "name": "TestTable", "target": "src/TestTable.Table.al"},
                {"type": "Codeunit", "name": "TestMgt", "target": "src/TestMgt.Codeunit.al"},
            ],
            "objects_to_modify": [],
            "requirements": [
                {"id": "REQ-001", "statement": "test", "acceptance_tests": ["AT-001"]}
            ],
            "acceptance_tests": [
                {"id": "AT-001", "requirement_ref": "REQ-001", "statement": "test"}
            ],
            "business_rules": [
                {"id": "BR-001", "description": "Validate date is not in the past"}
            ],
            "event_subscribers": [],
            "scope_boundaries": {
                "allowed_extensions": ["src"],
                "allowed_files": ["src/TestMgt.Codeunit.al", "src/TestTable.Table.al"],
                "scope_mode": "strict",
            },
            "traceability": {
                "requirement_to_test": {"REQ-001": ["AT-001"]},
                "requirement_to_object": {"REQ-001": ["OBJ-001"]},
                "field_to_object": {},
            },
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        yield root


class TestPlanDesign:
    async def test_generates_design_file(self, spec_project):
        result = await handle_plan_design(
            project_root=str(spec_project),
            spec_name="test-spec",
        )
        design_path = spec_project / ".specs" / "test-spec" / "DESIGN.md"
        assert design_path.exists()
        content = design_path.read_text()
        assert "Architecture Decisions" in content
        assert "Dependency Graph" in content

    async def test_includes_adrs(self, spec_project):
        result = await handle_plan_design(
            project_root=str(spec_project),
            spec_name="test-spec",
        )
        assert len(result["adrs"]) >= 1
        assert result["adrs"][0]["title"] == "Object Architecture"

    async def test_blocks_on_needs_grounding(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["status"] = "needs_grounding"
        spec["open_questions"] = [{"id": "OQ-001", "question": "confirm target", "blocking": True}]
        spec_path.write_text(json.dumps(spec))
        result = await handle_plan_design(project_root=str(spec_project), spec_name="test-spec")
        assert result.get("status") == "blocked_needs_grounding"
        assert not (spec_project / ".specs" / "test-spec" / "DESIGN.md").exists()

    async def test_blocks_on_invalid_grounded_create_contract(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["status"] = "grounded"
        spec["scope_boundaries"] = {"allowed_files": ["src/Existing.Table.al"]}
        spec["objects_to_create"] = [{"type": "Codeunit", "name": "MyUpgrade", "target": None}]
        spec_path.write_text(json.dumps(spec))
        result = await handle_plan_design(project_root=str(spec_project), spec_name="test-spec")
        assert result.get("status") == "blocked_invalid_spec_contract"
