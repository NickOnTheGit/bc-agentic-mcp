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
            "spec_name": "test-spec",
            "objects_to_create": [
                {"type": "Table", "name": "TestTable"},
                {"type": "Codeunit", "name": "TestMgt"},
            ],
            "objects_to_modify": [],
            "business_rules": [
                {"id": "BR-001", "description": "Validate date is not in the past"}
            ],
            "event_subscribers": [],
            "scope_boundaries": {"allowed_extensions": ["Test"]},
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
