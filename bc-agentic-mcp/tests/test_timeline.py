"""Tests for the per-item lifecycle timeline (unified with the checkpoint store)."""
from pathlib import Path

import pytest

from bc_agentic_mcp import timeline, checkpoints as memory


@pytest.fixture(autouse=True)
def _colocated(monkeypatch):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    yield


def test_record_phase_writes_checkpoint_and_md(tmp_path):
    timeline.record_phase(tmp_path, "wi-1", "item_received")
    timeline.record_phase(tmp_path, "wi-1", "spec_written", artifacts=["a/spec.json"])

    cps = [c for c in memory.load_checkpoints(tmp_path, "wi-1") if c.get("kind") == "phase"]
    assert [c["details"]["phase"] for c in cps] == ["item_received", "spec_written"]

    md = (tmp_path / ".specs" / "wi-1" / "TIMELINE.md").read_text(encoding="utf-8")
    assert "Item received" in md and "Spec written" in md and "spec.json" in md


def test_consecutive_identical_phase_is_deduped(tmp_path):
    timeline.record_phase(tmp_path, "wi-2", "review_prepared")
    second = timeline.record_phase(tmp_path, "wi-2", "review_prepared")
    assert second is None
    phases = [c for c in memory.load_checkpoints(tmp_path, "wi-2") if c.get("kind") == "phase"]
    assert len(phases) == 1


def test_record_tool_phase_maps_tool_to_phase(tmp_path):
    timeline.record_tool_phase(str(tmp_path), "wi-3", "bc_capture_item_context", {"summary_path": "x"})
    assert timeline.current_phase(tmp_path, "wi-3") == "item_received"
    # Unknown / non-phase tools are ignored.
    timeline.record_tool_phase(str(tmp_path), "wi-3", "bc_status", {})
    assert timeline.current_phase(tmp_path, "wi-3") == "item_received"


def test_digest_and_narrative_includes_mistakes(tmp_path):
    timeline.record_phase(tmp_path, "wi-4", "implemented")
    memory.append_checkpoint(tmp_path, "wi-4", kind="mistake", summary="wrong scope")
    d = timeline.digest(tmp_path, "wi-4")
    assert d["current_phase"] == "implemented"
    kinds = {e["kind"] for e in d["recent"]}
    assert "mistake" in kinds and "phase" in kinds


def test_digest_none_when_empty(tmp_path):
    assert timeline.digest(tmp_path, "wi-empty") is None


def test_handle_timeline_writes_file(tmp_path):
    timeline.record_phase(tmp_path, "wi-5", "verified")
    out = timeline.handle_timeline(str(tmp_path), "wi-5")
    assert out["current_phase"] == "verified"
    assert Path(out["timeline_path"]).exists()
    assert out["event_count"] >= 1
