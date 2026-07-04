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
async def test_write_spec_idempotency_binds_to_input_contract():
    """Same idempotency key with changed bullets must not return stale idempotent result."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        await handle_write_spec(
            project_root=str(root),
            spec_name="test-feature",
            human_bullets="- Add field A",
            idempotency_key="key-contract",
        )
        result2 = await handle_write_spec(
            project_root=str(root),
            spec_name="test-feature",
            human_bullets="- Add field B",
            idempotency_key="key-contract",
        )
        assert "already exists" not in result2.get("summary", {}).get("status", "")


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


@pytest.mark.asyncio
async def test_write_spec_flags_ungrounded_modification():
    """A modify-object that cannot be grounded must set status needs_grounding (fail-closed)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        await handle_write_spec(
            project_root=str(root),
            spec_name="ground-check",
            human_bullets="Add field to table NonExistentFooFDN (id 50000): name 'X', type Code[10].",
            idempotency_key="k-ground",
        )
        spec = json.loads((root / ".specs" / "ground-check" / "spec.json").read_text())
        assert spec["status"] == "needs_grounding"
        assert spec["open_questions"]


@pytest.mark.asyncio
async def test_write_spec_flags_unnamed_upgrade_create():
    """Upgrade intent without a concrete object name must remain needs_grounding."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        await handle_write_spec(
            project_root=str(root),
            spec_name="upgrade-ground-check",
            human_bullets=(
                "Add field name 'Facility Code Filter', type Code[250] to table Rental (id 50000). "
                "Deliver a data upgrade codeunit that populates existing records."
            ),
            idempotency_key="k-upgrade-ground",
        )
        spec = json.loads((root / ".specs" / "upgrade-ground-check" / "spec.json").read_text())
        assert spec["status"] == "needs_grounding"
        assert any(q.get("blocking") for q in spec.get("open_questions", []))
