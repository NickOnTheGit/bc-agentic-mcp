"""Tests for bc_clarify tool."""
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.tools.clarify import _detect_ambiguities


def test_detect_ambiguities_match_notify():
    """'notify' keyword should trigger notification question."""
    questions = _detect_ambiguities("notify the user when overdue", None, None)
    assert len(questions) >= 1
    assert any("notified" in q["question"].lower() for q in questions)


def test_detect_ambiguities_fallback():
    """No matching patterns should produce a fallback question."""
    questions = _detect_ambiguities("Add a simple field.", None, None)
    assert len(questions) == 1
    assert questions[0]["id"] == "Q-001"


def test_specific_concern_returns_immediately():
    """specific_concern should bypass heuristics."""
    questions = _detect_ambiguities("notify users", None, "What about performance?")
    assert len(questions) == 1
    assert questions[0]["question"] == "What about performance?"


@pytest.mark.asyncio
async def test_handle_clarify_creates_file():
    """handle_clarify should create clarifications.md."""
    from bc_agentic_mcp.tools.clarify import handle_clarify

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result = await handle_clarify(
            project_root=str(root),
            spec_name="test-spec",
            context="notify the user when done",
        )
        clar_path = root / ".specs" / "test-spec" / "clarifications.md"
        assert clar_path.exists()
        # Compare resolved paths to avoid 8.3 short-name discrepancies
        assert Path(result["file_path"]).resolve() == clar_path.resolve()
        assert len(result["questions"]) >= 1
