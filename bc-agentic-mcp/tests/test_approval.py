"""Tests for bc_request_approval and bc_submit_decision."""
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision


@pytest.mark.asyncio
async def test_request_approval_creates_file():
    """handle_request_approval should create an approval markdown file."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result = await handle_request_approval(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            artifact_path="spec.json",
            summary="Review the spec.",
            idempotency_key="key-1",
        )
        approval_path = Path(result["approval_path"])
        assert approval_path.exists()
        assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_submit_decision_updates_file():
    """handle_submit_decision should update approval file with decision."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        await handle_request_approval(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            artifact_path="spec.json",
            summary="Review the spec.",
            idempotency_key="key-1",
        )

        result = await handle_submit_decision(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            decision="approve",
            feedback="Looks good!",
        )
        assert result["status"] == "approve"
        assert result["next_action"] == "proceed_to_bc_plan_design"

        # Verify file was updated
        approval_path = root / ".specs" / "test-spec" / "approvals" / "spec.md"
        content = approval_path.read_text()
        assert "**Status:** approve" in content


@pytest.mark.asyncio
async def test_submit_decision_without_pending():
    """submit without pending approval should return error."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result = await handle_submit_decision(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            decision="approve",
        )
        assert result["status"] == "error"
        assert "pending approval" in result["message"]
