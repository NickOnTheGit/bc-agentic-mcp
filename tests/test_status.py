"""Tests for bc_status tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.status import handle_status


@pytest.mark.asyncio
async def test_status_returns_empty_when_no_state():
    """No state file should return empty summary."""
    with tempfile.TemporaryDirectory() as d:
        result = await handle_status(project_root=str(d))
        assert result["active_spec"] is None
        assert result["summary"]["total_specs"] == 0


@pytest.mark.asyncio
async def test_status_returns_spec_summary():
    """State file with specs should return summary counts."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs"
        specs_dir.mkdir(parents=True)
        state = {
            "active_spec": "spec-1",
            "total_specs": 2,
            "specs": {
                "spec-1": {"name": "spec-1", "phase": "implement"},
                "spec-2": {"name": "spec-2", "phase": "closed"},
            },
        }
        (specs_dir / "state.json").write_text(json.dumps(state))

        result = await handle_status(project_root=str(root))
        assert result["active_spec"] == "spec-1"
        assert result["summary"]["total_specs"] == 2
        assert result["summary"]["completed"] == 1
