"""Tests for bc_converge tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.converge import handle_converge


@pytest.mark.asyncio
async def test_converge_reports_converged_when_empty():
    """Empty spec + empty project should report converged."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {"spec_name": "test-feature", "objects_to_create": []}
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_converge(str(root), "test-feature")
        assert result["converged"] is True
        assert result["declared_count"] == 0
        assert result["implemented_count"] == 0


@pytest.mark.asyncio
async def test_converge_reports_missing_when_not_implemented():
    """Declared objects not on disk should appear in missing list."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-feature",
            "objects_to_create": [
                {"type": "Table", "name": "MyTable"},
            ],
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_converge(str(root), "test-feature")
        assert result["converged"] is False
        assert "Table MyTable" in result["missing"]


@pytest.mark.asyncio
async def test_converge_checks_declared_modify_target_and_field_presence():
    """Declared modify target should be validated for required data_model fields."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        target_dir = root / "extensions" / "BaseApp" / "src"
        target_dir.mkdir(parents=True)
        target_file = target_dir / "Sample.Table.al"
        target_file.write_text(
            'table 50000 "Sample" { fields { field(1; Code; Code[20]) { } } }',
            encoding="utf-8",
        )

        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-feature",
            "objects_to_create": [],
            "objects_to_modify": [
                {
                    "type": "Table",
                    "name": "Sample",
                    "target": "extensions/BaseApp/src/Sample.Table.al",
                }
            ],
            "data_model": [{"field": "Facility Code Filter", "al_type": "Code[250]"}],
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")

        result = await handle_converge(str(root), "test-feature")
        assert result["converged"] is False
        assert any("Facility Code Filter" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_converge_passes_when_modify_target_contains_required_field():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        target_dir = root / "extensions" / "BaseApp" / "src"
        target_dir.mkdir(parents=True)
        target_file = target_dir / "Sample.Table.al"
        target_file.write_text(
            (
                'table 50000 "Sample" { fields { '
                'field(1; Code; Code[20]) { } '
                'field(2; FacilityCodeFilter; Code[250]) { } } }'
            ),
            encoding="utf-8",
        )

        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-feature",
            "objects_to_create": [],
            "objects_to_modify": [
                {
                    "type": "Table",
                    "name": "Sample",
                    "target": "extensions/BaseApp/src/Sample.Table.al",
                }
            ],
            "data_model": [{"field": "Facility Code Filter", "al_type": "Code[250]"}],
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")

        result = await handle_converge(str(root), "test-feature")
        assert result["converged"] is True
