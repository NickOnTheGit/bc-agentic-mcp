"""Tests for bc_upgrade_codeunit tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.upgrade_codeunit import handle_upgrade_codeunit


@pytest.mark.asyncio
async def test_upgrade_codeunit_generates_file():
    """Should create an upgrade codeunit .al file."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {"spec_name": "test-feature", "module": "MyModule"}
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_upgrade_codeunit(str(root), "test-feature")
        path = Path(result["upgrade_path"])
        assert path.exists()
        content = path.read_text()
        assert "Subtype = Upgrade" in content
        assert "test-feature Upgrade" in content


@pytest.mark.asyncio
async def test_upgrade_codeunit_uses_module_from_spec():
    """Upgrade tag should include module name from spec."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {"spec_name": "test-feature", "module": "CustomModule"}
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_upgrade_codeunit(str(root), "test-feature")
        path = Path(result["upgrade_path"])
        content = path.read_text()
        assert "CustomModule" in content
