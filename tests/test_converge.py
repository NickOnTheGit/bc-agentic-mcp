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
