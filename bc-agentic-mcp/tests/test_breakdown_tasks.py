"""Tests for bc_breakdown_tasks."""
import json
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.tools.breakdown_tasks import handle_breakdown_tasks


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
            "work_types": ["table", "codeunit", "page", "upgrade"],
            "objects_to_create": [
                {"type": "Table", "name": "TestTable", "target": "src/TestTable.Table.al"},
                {"type": "Codeunit", "name": "TestMgt", "target": "src/TestMgt.Codeunit.al"},
                {"type": "Page", "name": "TestCard", "target": "src/TestCard.Page.al"},
            ],
            "objects_to_modify": [],
            "requirements": [{"id": "REQ-001", "statement": "test", "acceptance_tests": ["AT-001"]}],
            "acceptance_tests": [{"id": "AT-001", "requirement_ref": "REQ-001", "statement": "test"}],
            "business_rules": [{"id": "BR-001", "description": "Test"}],
            "event_subscribers": [{"event": "OnAfterModify", "purpose": "Log changes"}],
            "scope_boundaries": {
                "allowed_extensions": ["src"],
                "allowed_files": ["src/TestMgt.Codeunit.al", "src/TestTable.Table.al", "src/TestCard.Page.al"],
                "scope_mode": "strict",
            },
            "traceability": {
                "requirement_to_test": {"REQ-001": ["AT-001"]},
                "requirement_to_object": {"REQ-001": ["OBJ-001"]},
                "field_to_object": {},
            },
            "upgrade_contract": {
                "table_target": "src/TestTable.Table.al",
                "data_per_company": False,
                "required_scope": "per-database",
                "idempotency_tag": "test-spec_upgrade_v1",
            },
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        yield root


class TestBreakdownTasks:
    async def test_creates_waves(self, spec_project):
        result = await handle_breakdown_tasks(
            project_root=str(spec_project),
            spec_name="test-spec",
        )
        assert len(result["waves"]) == 5
        assert result["task_count"] == 6  # 1 table + 1 codeunit + 1 page + 1 sub + 2 test/upgrade

    async def test_wave_1_no_dependencies(self, spec_project):
        result = await handle_breakdown_tasks(
            project_root=str(spec_project),
            spec_name="test-spec",
        )
        wave1 = result["waves"][0]
        assert wave1["wave_number"] == 1

    async def test_writes_tasks_md(self, spec_project):
        await handle_breakdown_tasks(
            project_root=str(spec_project),
            spec_name="test-spec",
        )
        tasks_path = spec_project / ".specs" / "test-spec" / "TASKS.md"
        assert tasks_path.exists()
        content = tasks_path.read_text()
        assert "Wave 1" in content
        assert "Wave 5" in content

    async def test_writes_review_md(self, spec_project):
        result = await handle_breakdown_tasks(
            project_root=str(spec_project),
            spec_name="test-spec",
        )
        review_path = spec_project / ".specs" / "test-spec" / "REVIEW.md"
        assert review_path.exists()
        content = review_path.read_text()
        assert "Spec Review: test-spec" in content
        assert "Human Requirements" in content
        assert "Implementation Tasks" in content
        assert Path(result["review_path"]).resolve() == review_path.resolve()

    async def test_blocks_on_needs_grounding(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["status"] = "needs_grounding"
        spec_path.write_text(json.dumps(spec))
        result = await handle_breakdown_tasks(project_root=str(spec_project), spec_name="test-spec")
        assert result.get("status") == "blocked_needs_grounding"
        assert not (spec_project / ".specs" / "test-spec" / "TASKS.md").exists()

    async def test_table_modification_emits_task(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["work_types"] = ["table-field"]
        spec["objects_to_create"] = []
        spec["event_subscribers"] = []
        spec["objects_to_modify"] = [
            {"type": "Table", "name": "VeraSpaceDetailTypeFDN",
             "target": "extensions/BaseApp/src/VeraSpaceDetailType.Table.al",
             "change": "Extend VeraSpaceDetailTypeFDN"}
        ]
        spec["data_model"] = [{"field": "FacilityCodeFilter", "al_type": "Code[250]", "editable": False}]
        spec_path.write_text(json.dumps(spec))
        result = await handle_breakdown_tasks(project_root=str(spec_project), spec_name="test-spec")
        content = (spec_project / ".specs" / "test-spec" / "TASKS.md").read_text()
        assert "Modify Table" in content
        assert "FacilityCodeFilter" in content
        assert result["waves"][0]["tasks"], "expected a wave-1 task for the table modification"

    async def test_upgrade_task_only_when_upgrade_work_type(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["work_types"] = ["table-field", "page"]  # no upgrade
        spec["objects_to_create"] = [{"type": "Page", "name": "SomeCard", "target": "src/SomeCard.Page.al"}]
        spec["objects_to_modify"] = []
        spec["event_subscribers"] = []
        spec["scope_boundaries"]["allowed_files"] = ["src/SomeCard.Page.al"]
        spec_path.write_text(json.dumps(spec))
        await handle_breakdown_tasks(project_root=str(spec_project), spec_name="test-spec")
        content = (spec_project / ".specs" / "test-spec" / "TASKS.md").read_text()
        assert "Generate upgrade codeunit" not in content

    async def test_blocks_when_create_object_has_no_name(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["status"] = "grounded"
        spec["objects_to_create"] = [{"type": "Codeunit", "name": None, "subtype": "upgrade"}]
        spec_path.write_text(json.dumps(spec))
        result = await handle_breakdown_tasks(project_root=str(spec_project), spec_name="test-spec")
        assert result.get("status") == "blocked_invalid_spec_contract"
        assert result.get("task_count") == 0

    async def test_blocks_on_invalid_grounded_create_contract(self, spec_project):
        spec_path = spec_project / ".specs" / "test-spec" / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["status"] = "grounded"
        spec["scope_boundaries"] = {"allowed_files": ["src/Some.Table.al"]}
        spec["objects_to_create"] = [{"type": "Codeunit", "name": "MyUpgrade", "target": None}]
        spec_path.write_text(json.dumps(spec))
        result = await handle_breakdown_tasks(project_root=str(spec_project), spec_name="test-spec")
        assert result.get("status") == "blocked_invalid_spec_contract"
        assert result.get("task_count") == 0

