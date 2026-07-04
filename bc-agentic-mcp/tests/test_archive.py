"""Tests for bc_archive tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.archive import handle_archive


def _make_state(specs_dir: Path, spec_name: str = "test-spec") -> Path:
    sm_path = specs_dir / "state.json"
    state = {
        "active_spec": spec_name,
        "total_specs": 1,
        "specs": {
            spec_name: {
                "name": spec_name,
                "phase": "implement",
                "created": "2024-01-01T00:00:00+00:00",
                "last_activity": "2024-01-01T00:00:00+00:00",
            }
        },
    }
    sm_path.write_text(json.dumps(state))
    return sm_path


@pytest.mark.asyncio
async def test_archive_blocked_without_tests():
    """handle_archive should block when no test evidence exists."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs"
        specs_dir.mkdir(parents=True)
        _make_state(specs_dir)

        result = await handle_archive(str(root), "test-spec", outcome="merged")
        assert result["archived"] is False
        assert result["blocked"] is True
        assert "next_action" in result
        assert result["next_action"]["tool"] == "bc_generate_tests"


@pytest.mark.asyncio
async def test_archive_blocked_with_unchecked_tasks():
    """handle_archive should block when TASKS.md has unchecked items."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs"
        spec_dir = specs_dir / "test-spec"
        spec_dir.mkdir(parents=True)
        _make_state(specs_dir)
        (spec_dir / "TASKS.md").write_text("# Tasks\n- [ ] T-001 pending\n", encoding="utf-8")

        result = await handle_archive(str(root), "test-spec", outcome="merged")
        assert result["archived"] is False
        assert result["blocked"] is True
        assert any("T-001" in b for b in result["blockers"])


@pytest.mark.asyncio
async def test_archive_closes_spec_with_force():
    """handle_archive with force=True bypasses the test gate and closes the spec."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs"
        specs_dir.mkdir(parents=True)
        sm_path = _make_state(specs_dir)

        result = await handle_archive(str(root), "test-spec", outcome="merged", force=True)
        assert result["status"] == "closed"
        assert result["outcome"] == "merged"
        assert result["forced"] is True

        updated = json.loads(sm_path.read_text())
        assert updated["specs"]["test-spec"]["phase"] == "closed"
        assert updated["specs"]["test-spec"]["outcome"] == "merged"
