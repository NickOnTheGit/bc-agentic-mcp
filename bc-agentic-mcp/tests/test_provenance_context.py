"""Tests for the paradigm wiring: provenance, context-source, comments, stale detection."""
import json
from pathlib import Path

from bc_agentic_mcp import provenance, item_context, detectors


def _bundle(root: Path, spec: str = "s1", desc: str = "Add field to table CustomerFDN.", comments=None):
    item_context.capture(str(root), spec, item_id="1", description=desc, comments=comments)


def test_provenance_stamp_and_staleness():
    spec: dict = {}
    provenance.stamp(spec, "abc")
    assert spec["provenance"]["generator_version"] == provenance.GENERATOR_VERSION
    assert spec["provenance"]["context_sha"] == "abc"
    assert provenance.staleness(spec, "abc") is None          # unchanged
    assert provenance.staleness(spec, "xyz")                  # context changed
    spec["provenance"]["generator_version"] = "planner/OLD"
    assert provenance.staleness(spec, "abc")                  # planner changed


def test_context_source_includes_comments(tmp_path):
    _bundle(tmp_path, comments=[{"author": "Cosmina", "date": "2026",
                                 "text": "The field must be NOT editable and Code[250]."}])
    src = item_context.context_source(str(tmp_path), "s1")
    assert src is not None
    assert "not editable" in src["text"].lower()
    assert "code[250]" in src["text"].lower()
    assert len(src["sha"]) >= 8


def test_context_source_none_without_bundle(tmp_path):
    assert item_context.context_source(str(tmp_path), "missing") is None


def test_stale_spec_detector_fires_on_generator_change(tmp_path):
    _bundle(tmp_path)
    spec_dir = tmp_path / ".specs" / "s1"
    (spec_dir).mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.json").write_text(
        json.dumps({"provenance": {"generator_version": "planner/OLD", "context_sha": "zzz"}}),
        encoding="utf-8",
    )
    findings = detectors.detect(tmp_path, "s1", diagnostics=[])
    assert any(f["detector"] == "stale_spec" for f in findings)


def test_stale_spec_detector_quiet_when_current(tmp_path):
    _bundle(tmp_path)
    src = item_context.context_source(str(tmp_path), "s1")
    spec_dir = tmp_path / ".specs" / "s1"
    (spec_dir).mkdir(parents=True, exist_ok=True)
    spec = {}
    provenance.stamp(spec, src["sha"])
    (spec_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    findings = detectors.detect(tmp_path, "s1", diagnostics=[])
    assert not any(f["detector"] == "stale_spec" for f in findings)
