"""Tests for enforcement — mechanical proof each engine ran, and the commit gate that uses it."""
import hashlib
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import enforcement, gate, timeline


@pytest.fixture(autouse=True)
def _colocated(monkeypatch):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    yield


def _green_spec(tmp: Path, spec: str = "wi-1") -> Path:
    """Materialize a fully-green item: linked spec, timeline, code-context, quality, approval."""
    d = tmp / ".specs" / spec
    (d / "context" / "code").mkdir(parents=True, exist_ok=True)
    (d / "approvals").mkdir(parents=True, exist_ok=True)
    spec_json = {
        "requirements": [{"id": "REQ-001", "acceptance_tests": ["AT-001"]}],
        "acceptance_tests": [{"id": "AT-001", "requirement_ref": "REQ-001"}],
    }
    (d / "spec.json").write_text(json.dumps(spec_json), encoding="utf-8")
    (d / "context" / "code" / "code_context.json").write_text("{}", encoding="utf-8")
    sha = hashlib.sha256((d / "spec.json").read_bytes()).hexdigest()
    (d / "quality.json").write_text(json.dumps({"errors": 0, "spec_sha": sha, "mode": "self"}), encoding="utf-8")
    # refinement engine: ran, clean verdict (claims confronted with code reality)
    (d / "item_refinement.json").write_text(json.dumps({
        "findings": {"counts": {"mismatches": 0, "conflicts": 0}},
        "critique": "verified", "generated_at": "2099-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    timeline.record_phase(tmp, spec, "implemented")
    (d / "approvals" / "implement.md").write_text("**Status:** approve\n", encoding="utf-8")
    return d


def test_all_engines_missing_blocks_everything(tmp_path):
    st = enforcement.engine_status(tmp_path, "nope")
    assert st["all_ok"] is False
    names = {b.split(":")[0] for b in st["blocking"]}
    # root_cause is lane-conditional: green for the default (pbi) lane, demanded only
    # when the captured identity says the item is a Bug (see test_root_cause.py).
    assert names == set(enforcement.REQUIRED_ENGINES) - {"root_cause"}


def test_fully_green_passes(tmp_path):
    _green_spec(tmp_path, "wi-1")
    st = enforcement.engine_status(tmp_path, "wi-1")
    assert st["all_ok"] is True
    assert st["blocking"] == []


def test_stale_quality_is_flagged(tmp_path):
    d = _green_spec(tmp_path, "wi-2")
    # Mutate the spec so the recorded spec_sha no longer matches.
    (d / "spec.json").write_text(json.dumps({"requirements": [{"id": "REQ-001", "acceptance_tests": ["AT-001"]}],
                                             "acceptance_tests": [{"id": "AT-001", "requirement_ref": "REQ-001"}],
                                             "changed": True}), encoding="utf-8")
    st = enforcement.engine_status(tmp_path, "wi-2")
    assert st["engines"]["quality"]["ok"] is False
    assert "stale" in st["engines"]["quality"]["reason"]


def test_orphaned_traceability_blocks(tmp_path):
    d = _green_spec(tmp_path, "wi-3")
    spec_json = json.loads((d / "spec.json").read_text())
    spec_json["acceptance_tests"].append({"id": "AT-099", "requirement_ref": "REQ-MISSING"})
    (d / "spec.json").write_text(json.dumps(spec_json), encoding="utf-8")
    # refresh quality spec_sha so only traceability fails
    sha = hashlib.sha256((d / "spec.json").read_bytes()).hexdigest()
    (d / "quality.json").write_text(json.dumps({"errors": 0, "spec_sha": sha}), encoding="utf-8")
    st = enforcement.engine_status(tmp_path, "wi-3")
    assert st["engines"]["traceability"]["ok"] is False


def test_gate_allows_when_authorized_and_all_engines_green(tmp_path):
    _green_spec(tmp_path, "wi-1")
    res = gate.check(str(tmp_path), ["src/Foo.Table.al"], spec_name="wi-1")
    assert res["allowed"] is True
    assert "all engines green" in res["reason"]


def test_gate_blocks_when_an_engine_did_not_run(tmp_path):
    d = _green_spec(tmp_path, "wi-1")
    (d / "quality.json").unlink()  # F1 never ran
    res = gate.check(str(tmp_path), ["src/Foo.Table.al"], spec_name="wi-1")
    assert res["allowed"] is False
    assert "quality" in res["reason"]


def test_gate_still_blocks_without_approval(tmp_path):
    _green_spec(tmp_path, "wi-1")
    (tmp_path / ".specs" / "wi-1" / "approvals" / "implement.md").write_text("**Status:** pending\n", encoding="utf-8")
    res = gate.check(str(tmp_path), ["src/Foo.Table.al"], spec_name="wi-1")
    assert res["allowed"] is False
    assert "approved charter" in res["reason"].lower()
