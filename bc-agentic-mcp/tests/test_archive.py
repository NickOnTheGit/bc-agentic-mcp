"""Tests for bc_archive tool."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.archive import handle_archive


@pytest.mark.asyncio
async def test_archive_closes_spec():
    """handle_archive should mark spec as closed with outcome."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs"
        specs_dir.mkdir(parents=True)
        # Init state with a spec
        sm_path = specs_dir / "state.json"
        state = {
            "active_spec": "test-spec",
            "total_specs": 1,
            "specs": {
                "test-spec": {
                    "name": "test-spec",
                    "phase": "implement",
                    "created": "2024-01-01T00:00:00+00:00",
                    "last_activity": "2024-01-01T00:00:00+00:00",
                }
            },
        }
        sm_path.write_text(json.dumps(state))

        result = await handle_archive(str(root), "test-spec", outcome="merged")
        assert result["status"] == "closed"
        assert result["outcome"] == "merged"

        # Verify state was updated
        updated = json.loads(sm_path.read_text())
        assert updated["specs"]["test-spec"]["phase"] == "closed"
        assert updated["specs"]["test-spec"]["outcome"] == "merged"
