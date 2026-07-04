"""Tests for bc_request_approval and bc_submit_decision."""
import tempfile
from pathlib import Path
import json
import pytest
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision


@pytest.mark.asyncio
async def test_request_approval_creates_file():
    """handle_request_approval should create an approval markdown file."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        artifact = root / "spec.json"
        artifact.write_text('{"spec": "content for the human to review"}', encoding="utf-8")
        result = await handle_request_approval(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            artifact_path=str(artifact),
            summary="Review the spec.",
            idempotency_key="key-1",
        )
        approval_path = Path(result["approval_path"])
        assert approval_path.exists()
        assert result["status"] == "pending"
        # PRESENTATION WALL: the response must carry the artifact content so the
        # orchestrator can show it verbatim (approval on a paraphrase is void).
        assert result["present_to_human"]["content"].startswith('{"spec"')
        assert result["present_to_human"]["truncated"] is False


@pytest.mark.asyncio
async def test_request_approval_refuses_missing_artifact():
    """A non-existent artifact_path must fail closed — the human cannot review air
    (observed live on wi267598: any path string was accepted unverified)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        result = await handle_request_approval(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            artifact_path=str(root / "does-not-exist.md"),
            summary="Review.",
            idempotency_key="key-x",
        )
        assert result["status"] == "blocked_artifact_missing"
        assert result["blocked"] is True


@pytest.mark.asyncio
async def test_submit_decision_updates_file():
    """handle_submit_decision should update approval file with decision."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        artifact = root / "spec.json"
        artifact.write_text('{"spec": true}', encoding="utf-8")
        await handle_request_approval(
            project_root=str(root),
            spec_name="test-spec",
            phase="spec",
            artifact_path=str(artifact),
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


@pytest.mark.asyncio
async def test_submit_tasks_approval_blocked_on_invalid_spec_contract():
    """Tasks approval must block when strict traceability/spec contract is invalid."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        specs_dir = root / ".specs" / "test-spec"
        specs_dir.mkdir(parents=True)
        # Deliberately invalid for strict schema+traceability gate.
        (specs_dir / "spec.json").write_text(json.dumps({"spec_name": "test-spec"}), encoding="utf-8")

        await handle_request_approval(
            project_root=str(root),
            spec_name="test-spec",
            phase="tasks",
            artifact_path=str(specs_dir / "spec.json"),
            summary="Review tasks.",
            idempotency_key="key-tasks",
        )
        result = await handle_submit_decision(
            project_root=str(root),
            spec_name="test-spec",
            phase="tasks",
            decision="approve",
        )
        assert result["status"] == "blocked"
        assert "traceability/spec contract gate" in result["message"]
