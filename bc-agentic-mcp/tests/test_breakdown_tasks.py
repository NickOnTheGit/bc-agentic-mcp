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
            "spec_name": "test-spec",
            "objects_to_create": [
                {"type": "Table", "name": "TestTable"},
                {"type": "Codeunit", "name": "TestMgt"},
                {"type": "Page", "name": "TestCard"},
            ],
            "objects_to_modify": [],
            "business_rules": [{"id": "BR-001", "description": "Test"}],
            "event_subscribers": [{"event": "OnAfterModify", "purpose": "Log changes"}],
            "scope_boundaries": {"allowed_extensions": ["Test"]},
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
