"""Tests for bc_feedback tool."""
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.tools.feedback import handle_feedback


@pytest.mark.asyncio
async def test_feedback_appends_to_feedback_file():
    """handle_feedback should append entry to FEEDBACK.md."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result = await handle_feedback(
            project_root=str(root),
            spec_name="test-spec",
            feedback="Great work!",
            rating=5,
        )
        feedback_path = Path(result["feedback_path"])
        assert feedback_path.exists()
        content = feedback_path.read_text()
        assert "Great work!" in content
        assert "rating: 5" in content
