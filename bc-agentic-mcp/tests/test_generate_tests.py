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


@pytest.mark.asyncio
async def test_generate_tests_emits_layered_categories():
    """A spec with date/text/code fields should emit negative + boundary + business-logic tests."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "on-hold"
        specs_dir.mkdir(parents=True)
        spec = {
            "spec_name": "on-hold",
            "business_rules": [{"id": "BR-1", "description": "On hold persists"}],
            "operations": [{"type": "read"}, {"type": "update"}],
            "objects": [{"type": "API Page", "object_name": "rentalMutation"}],
            "data_model": [
                {"name": "OnHoldTill", "al_type": "Date"},
                {"name": "OnHoldUser", "al_type": "Code[50]"},
                {"name": "Remark", "al_type": "Text[250]"},
            ],
        }
        (specs_dir / "spec.json").write_text(json.dumps(spec))
        # D1 schema-reality gate: API-facing fields must be reconciled against the
        # deployed schema before tests may be generated.
        (specs_dir / "reconcile_report.json").write_text(json.dumps({
            "existing": ["OnHoldTill", "OnHoldUser", "Remark"],
            "new": [],
            "all_requested_exist": True,
        }))

        result = await handle_generate_tests(str(root), "on-hold")
        content = Path(result["test_path"]).read_text()
        # Layered sections present.
        assert "// --- negative ---" in content
        assert "// --- boundary ---" in content
        assert "// --- business-logic ---" in content
        assert "// --- api-contract ---" in content
        # Backward-compatible happy-path count, richer total.
        assert result["test_count"] == 1
        assert result["total_procedures"] > 1
        assert result["categories"]["negative"] >= 1
        assert result["categories"]["api-contract"] >= 1
        # A PLAN.md is written alongside.
        assert Path(result["plan_path"]).exists()
        assert result["validation_slices"] == ["item", "regression", "api-if-applicable"]
        assert result["suggested_minimums"]["item"] >= 1
        assert result["suggested_minimums"]["regression"] == 1
        assert result["suggested_minimums"]["api"] == 1
        # Scaffolds must FAIL until implemented — no vacuous green.
        assert "IsTrue(true" not in content
        assert "asserterror LibraryAssert.Fail" not in content
        assert content.count("LibraryAssert.Fail('TODO") == result["total_procedures"]

