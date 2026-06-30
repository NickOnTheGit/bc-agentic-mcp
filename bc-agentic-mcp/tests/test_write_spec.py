"""Tests for bc_write_spec tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.write_spec import handle_write_spec


@pytest.mark.asyncio
async def test_write_spec_creates_files():
    """handle_write_spec should create TDD.md and spec.json."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result = await handle_write_spec(
            project_root=str(root),
            spec_name="test-feature",
            human_bullets="- Add a new field",
            idempotency_key="key-1",
        )
        specs_dir = root / ".specs" / "test-feature"
        tdd_path = specs_dir / "TDD.md"
        assert tdd_path.exists()
        assert (specs_dir / "spec.json").exists()
        # Compare resolved paths to avoid 8.3 short-name discrepancies
        assert Path(result["tdd_path"]).resolve() == tdd_path.resolve()


@pytest.mark.asyncio
async def test_write_spec_idempotent():
    """Same idempotency_key should return existing spec."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result1 = await handle_write_spec(
            project_root=str(root),
            spec_name="test-feature",
            human_bullets="- Add a field",
            idempotency_key="key-1",
        )
        result2 = await handle_write_spec(
            project_root=str(root),
            spec_name="test-feature",
            human_bullets="- Add a field",
            idempotency_key="key-1",
        )
        assert "already exists" in result2.get("summary", {}).get("status", "")


@pytest.mark.asyncio
async def test_write_spec_stores_bullets_in_tdd():
    """Human bullets should appear in the TDD.md content."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        await handle_write_spec(
            project_root=str(root),
            spec_name="test-feature",
            human_bullets="- Add validation",
            idempotency_key="key-2",
        )
        tdd = (root / ".specs" / "test-feature" / "TDD.md").read_text()
        assert "Add validation" in tdd


@pytest.mark.asyncio
async def test_write_spec_rejects_empty_name():
    """Empty spec_name should raise ValueError."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        with pytest.raises(ValueError, match="spec_name"):
            await handle_write_spec(
                project_root=str(root),
                spec_name="",
                human_bullets="- test",
                idempotency_key="key-3",
            )
