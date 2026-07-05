"""context_recovery — the disk IS the memory when agent context compacts.

Contracts:
- results over the threshold are persisted VERBATIM (as the agent saw them) to
  .specs/<spec>/artifacts/ and gain a `recovery` pointer; small results untouched
- the persistence lands in the item timeline (kind="artifact") so checkpoints.jsonl
  reconstructs the whole story, including where the big payloads live
- read-only projections (bc_status & co) are never persisted (no self-reference)
- pruning keeps the newest N artifacts; env overrides for threshold/keep
- disk_map lists key lifecycle files + newest artifacts/logs for bc_status
- fail-open: non-dict results, missing spec/root -> passthrough
"""
import json
from pathlib import Path

from bc_agentic_mcp import context_recovery, timeline


def _big_result(**extra):
    return {"status": "ok", "payload": ["x" * 100] * 300, **extra}  # >16KB serialized


def test_small_result_untouched(tmp_path):
    result = {"status": "ok", "n": 1}
    out = context_recovery.persist_result(result, str(tmp_path), "s1", "bc_review")
    assert out is result and "recovery" not in out
    assert not (tmp_path / ".specs" / "s1" / "artifacts").exists()


def test_large_result_persisted_verbatim_and_announced(tmp_path):
    result = _big_result()
    out = context_recovery.persist_result(result, str(tmp_path), "s1", "bc_review")
    rec = out["recovery"]
    artifact = Path(rec["artifact"])
    assert artifact.exists() and rec["bytes"] == artifact.stat().st_size or rec["bytes"] > 0
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["payload"] == result["payload"]  # verbatim
    assert "recovery" not in saved  # pointer is response-only, file is pure payload
    # Announced in the timeline -> digest carries the pointer into every response.
    digest = timeline.digest(tmp_path, "s1")
    assert digest is not None
    assert any("persisted for recovery" in e["summary"] for e in digest["recent"])
    events = timeline.load_timeline(tmp_path, "s1")
    art = [e for e in events if e.get("kind") == "artifact"]
    assert art and art[0]["details"]["path"] == str(artifact)


def test_excluded_tools_never_persist(tmp_path):
    out = context_recovery.persist_result(_big_result(), str(tmp_path), "s1", "bc_status")
    assert "recovery" not in out
    assert not (tmp_path / ".specs" / "s1" / "artifacts").exists()


def test_passthrough_without_spec_or_dict(tmp_path):
    assert context_recovery.persist_result("text", str(tmp_path), "s1", "bc_review") == "text"
    big = _big_result()
    assert "recovery" not in context_recovery.persist_result(big, None, "s1", "bc_review")
    assert "recovery" not in context_recovery.persist_result(big, str(tmp_path), None, "bc_review")


def test_threshold_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_MCP_RECOVERY_THRESHOLD", "10")
    out = context_recovery.persist_result(
        {"status": "ok", "text": "well over ten chars"}, str(tmp_path), "s1", "bc_detect")
    assert "recovery" in out


def test_prune_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_MCP_RECOVERY_THRESHOLD", "10")
    monkeypatch.setenv("BC_MCP_RECOVERY_KEEP", "3")
    adir = context_recovery.artifacts_dir(tmp_path, "s1")
    adir.mkdir(parents=True)
    for i in range(5):  # stamp-prefixed names sort chronologically
        (adir / f"2026070{i}-000000-bc_old.json").write_text("{}", encoding="utf-8")
    context_recovery.persist_result(
        {"status": "ok", "text": "trigger persistence now"}, str(tmp_path), "s1", "bc_detect")
    remaining = sorted(f.name for f in adir.glob("*.json"))
    assert len(remaining) == 3
    assert "20260700-000000-bc_old.json" not in remaining  # oldest gone


def test_disk_map_lists_key_files_artifacts_logs(tmp_path):
    base = tmp_path / ".specs" / "s1"
    (base / "context").mkdir(parents=True)
    (base / "artifacts").mkdir()
    (base / "logs").mkdir()
    (base / "spec.json").write_text("{}", encoding="utf-8")
    (base / "context" / "manifest.json").write_text("{}", encoding="utf-8")
    (base / "artifacts" / "20260705-120000-bc_review.json").write_text("{}", encoding="utf-8")
    (base / "logs" / "bc_run_tests-stdout-20260705.log").write_text("out", encoding="utf-8")
    dm = context_recovery.disk_map(tmp_path, "s1")
    paths = [e["path"] for e in dm["key_files"]]
    assert any(p.endswith("spec.json") for p in paths)
    assert any(p.endswith("manifest.json") for p in paths)
    assert len(dm["artifacts"]) == 1 and dm["artifacts"][0]["bytes"] == 2
    assert len(dm["logs"]) == 1
    assert "never re-run" in dm["hint"]


def test_disk_map_empty_spec_is_safe(tmp_path):
    dm = context_recovery.disk_map(tmp_path, "ghost")
    assert dm["key_files"] == [] and dm["artifacts"] == [] and dm["logs"] == []


def test_status_carries_recovery_surface(tmp_path):
    """bc_status(spec) = the resume packet: timeline story + on-disk map."""
    import asyncio
    from bc_agentic_mcp.tools.status import handle_status
    specs_dir = tmp_path / ".specs"
    specs_dir.mkdir()
    (specs_dir / "state.json").write_text(json.dumps(
        {"active_spec": "s1", "specs": {"s1": {"phase": "implementing"}}}), encoding="utf-8")
    (specs_dir / "s1").mkdir()
    (specs_dir / "s1" / "spec.json").write_text("{}", encoding="utf-8")
    timeline.record_phase(tmp_path, "s1", "spec_written")
    context_recovery.persist_result(_big_result(), str(tmp_path), "s1", "bc_review")
    out = asyncio.run(handle_status(str(tmp_path), "s1"))
    assert out["timeline"]["current_phase"] == "spec_written"
    assert any("persisted for recovery" in e["summary"] for e in out["timeline"]["recent"])
    assert any(e["path"].endswith("spec.json") for e in out["on_disk"]["key_files"])
    assert len(out["on_disk"]["artifacts"]) == 1
