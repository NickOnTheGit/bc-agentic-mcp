"""Tests for automatic self-reflection (nudge + record)."""
import pytest

from bc_agentic_mcp import reflection
from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import lessons as lessons_store


def test_no_pending_when_no_signals(tmp_path):
    memory.write_charter(tmp_path, "s", purpose="p", operations={}, acceptance_criteria=["c"])
    assert reflection.pending_reflections(tmp_path, "s")["count"] == 0


def test_mistake_checkpoint_becomes_pending(tmp_path):
    memory.write_charter(tmp_path, "s", purpose="p", operations={}, acceptance_criteria=["c"])
    memory.append_checkpoint(tmp_path, "s", kind="mistake", summary="wrong target API")
    p = reflection.pending_reflections(tmp_path, "s")
    assert p["count"] == 1 and p["signals"][0]["kind"] == "mistake"


def test_record_reflection_clears_and_records_lesson(tmp_path):
    memory.write_charter(tmp_path, "s", purpose="p", operations={}, acceptance_criteria=["c"])
    memory.append_checkpoint(tmp_path, "s", kind="correction", summary="reverted wrong change")
    r = reflection.record_reflection(
        tmp_path, "s", note="learned target selection",
        lessons=[{"message": "Read the wiki before choosing the target API.", "match": {"keyword": "api"}}],
    )
    assert r["reflected"] and r["lessons_recorded"] == 1 and r["addressed_signals"] == 1
    # Reflection checkpoint clears the pending nudge.
    assert reflection.pending_reflections(tmp_path, "s")["count"] == 0
    # The lesson was persisted to the project store.
    assert any("wiki" in l["message"].lower() for l in lessons_store.load_lessons(tmp_path))


def test_new_signal_after_reflection_is_pending_again(tmp_path):
    memory.write_charter(tmp_path, "s", purpose="p", operations={}, acceptance_criteria=["c"])
    memory.append_checkpoint(tmp_path, "s", kind="mistake", summary="m1")
    reflection.record_reflection(tmp_path, "s", note="done", lessons=[])
    assert reflection.pending_reflections(tmp_path, "s")["count"] == 0
    memory.append_checkpoint(tmp_path, "s", kind="override", summary="approved despite gap")
    assert reflection.pending_reflections(tmp_path, "s")["count"] == 1


def test_promote_writes_global(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(tmp_path / "g.json"))
    memory.write_charter(tmp_path, "s", purpose="p", operations={}, acceptance_criteria=["c"])
    memory.append_checkpoint(tmp_path, "s", kind="mistake", summary="m")
    reflection.record_reflection(
        tmp_path, "s", note="n", promote=True,
        lessons=[{"message": "Permissions are table-level.", "match": {"keyword": "permission"}}],
    )
    assert any("table-level" in g["message"].lower() for g in lessons_store.load_global_lessons())
