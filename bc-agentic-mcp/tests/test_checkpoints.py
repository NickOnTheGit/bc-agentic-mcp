"""Tests for durable planning memory (Charter + checkpoint log) and bc_recall."""
import pytest

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp.tools.recall import handle_recall, handle_checkpoint


def test_charter_is_create_once_and_immutable(tmp_path):
    first = memory.write_charter(
        tmp_path,
        "wi-x",
        purpose="Expose subprocess on-hold fields on the rentalMutation API",
        operations={"read": True, "update": True},
        acceptance_criteria=["GET returns the fields", "PATCH persists the fields"],
    )
    assert first["operations"] == {"read": True, "update": True}
    # A second write without overwrite must NOT change the pinned intent.
    memory.write_charter(
        tmp_path,
        "wi-x",
        purpose="DIFFERENT purpose",
        operations={"read": True, "update": False},
        acceptance_criteria=[],
    )
    loaded = memory.load_charter(tmp_path, "wi-x")
    assert loaded["purpose"] == "Expose subprocess on-hold fields on the rentalMutation API"
    assert loaded["operations"] == {"read": True, "update": True}
    # Human-readable form is persisted too.
    assert (tmp_path / ".specs" / "wi-x" / "CHARTER.md").exists()


def test_charter_overwrite_is_explicit(tmp_path):
    memory.write_charter(tmp_path, "wi-x", purpose="A", operations={"read": True})
    memory.write_charter(tmp_path, "wi-x", purpose="B", operations={"read": False}, overwrite=True)
    assert memory.load_charter(tmp_path, "wi-x")["purpose"] == "B"


def test_checkpoints_append_and_order(tmp_path):
    memory.append_checkpoint(tmp_path, "wi-x", kind="gate", summary="first")
    memory.append_checkpoint(tmp_path, "wi-x", kind="decision", summary="second")
    memory.append_checkpoint(tmp_path, "wi-x", kind="decision", summary="third")
    entries = memory.load_checkpoints(tmp_path, "wi-x")
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert [e["summary"] for e in entries] == ["first", "second", "third"]
    # limit returns the most recent
    assert [e["summary"] for e in memory.load_checkpoints(tmp_path, "wi-x", limit=2)] == ["second", "third"]


def test_recall_digest_reports_missing(tmp_path):
    digest = memory.recall_digest(tmp_path, "nope")
    assert digest["found"] is False
    assert digest["charter"] is None


@pytest.mark.asyncio
async def test_recall_tool_reanchors_on_purpose(tmp_path):
    memory.write_charter(
        tmp_path,
        "wi-x",
        purpose="Read and update rental mutation subprocess fields",
        operations={"read": True, "update": True},
        acceptance_criteria=["PATCH persists the value"],
    )
    memory.append_checkpoint(tmp_path, "wi-x", kind="gate", summary="quality gate pass=True")

    result = await handle_recall(project_root=str(tmp_path), spec_name="wi-x")
    assert result["found"] is True
    assert "update=yes" in result["reanchor"]
    assert "Read and update rental mutation subprocess fields" in result["reanchor"]
    assert "quality gate pass=True" in result["reanchor"]


@pytest.mark.asyncio
async def test_checkpoint_tool_records(tmp_path):
    resp = await handle_checkpoint(
        project_root=str(tmp_path),
        spec_name="wi-x",
        summary="Chose in-place writable v21 page",
        kind="decision",
    )
    assert resp["recorded"] is True
    assert resp["checkpoint"]["seq"] == 1
    assert memory.load_checkpoints(tmp_path, "wi-x")[0]["summary"] == "Chose in-place writable v21 page"


def test_attach_reanchor_injects_charter(tmp_path):
    from bc_agentic_mcp.server import _attach_reanchor

    memory.write_charter(
        tmp_path,
        "wi-x",
        purpose="Read and update subprocess fields",
        operations={"read": True, "update": True},
        acceptance_criteria=["PATCH persists"],
    )
    result = _attach_reanchor({"status": "ok"}, {"project_root": str(tmp_path), "spec_name": "wi-x"})
    assert "update=yes" in result["reanchor"]
    assert result["charter"]["operations"] == {"read": True, "update": True}


def test_attach_reanchor_noop_without_charter(tmp_path):
    from bc_agentic_mcp.server import _attach_reanchor

    # No charter for this spec -> response is unchanged (no reanchor key).
    result = _attach_reanchor({"status": "ok"}, {"project_root": str(tmp_path), "spec_name": "missing"})
    assert "reanchor" not in result
    # And non-spec-scoped calls are untouched.
    assert _attach_reanchor({"a": 1}, {}) == {"a": 1}
