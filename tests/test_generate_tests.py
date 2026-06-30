"""Tests for bc_generate_tests tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.generate_tests import handle_generate_tests


@pytest.mark.asyncio
async def test_generate_tests_with_business_rules():
    """Should create test functions matching business rules."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "test-feature",
            "business_rules": [
                {"id": "BR-001", "description": "Validate customer credit limit"},
                {"id": "BR-002", "description": "Block overdue customers"},
            ],
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_generate_tests(str(root), "test-feature")
        test_path = Path(result["test_path"])
        assert test_path.exists()
        content = test_path.read_text()
        assert "Test_001" in content
        assert "Test_002" in content
        assert result["test_count"] == 2


@pytest.mark.asyncio
async def test_generate_tests_without_rules():
    """Should create a placeholder test when no rules defined."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-feature"
        specs_dir.mkdir(parents=True)
        spec = {"spec_name": "test-feature", "business_rules": []}
        (specs_dir / "spec.json").write_text(json.dumps(spec))

        result = await handle_generate_tests(str(root), "test-feature")
        content = Path(result["test_path"]).read_text()
        assert "Test_Placeholder" in content
        assert result["test_count"] == 1
