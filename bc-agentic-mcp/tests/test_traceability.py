"""Tests for traceability — deterministic requirement->test tracing (OFT model)."""
from bc_agentic_mcp import traceability


def _reqs():
    return [
        {"id": "REQ-001", "statement": "shall x", "acceptance_tests": ["AT-001"]},
        {"id": "REQ-002", "statement": "shall y", "acceptance_tests": ["AT-002"]},
    ]


def _ats():
    return [
        {"id": "AT-001", "requirement_ref": "REQ-001", "statement": "given/then x"},
        {"id": "AT-002", "requirement_ref": "REQ-002", "statement": "given/then y"},
    ]


def test_full_coverage_is_ok():
    t = traceability.build_trace(_reqs(), _ats())
    assert t["ok"] is True
    assert t["coverage_pct"] == 100.0
    assert t["uncovered"] == [] and t["orphaned"] == []


def test_uncovered_requirement_detected():
    reqs = _reqs() + [{"id": "REQ-003", "statement": "shall z", "acceptance_tests": []}]
    t = traceability.build_trace(reqs, _ats())
    assert t["ok"] is False
    assert "REQ-003" in t["uncovered"]
    assert t["coverage_pct"] == 66.7


def test_orphaned_acceptance_test_detected():
    ats = _ats() + [{"id": "AT-099", "requirement_ref": "REQ-DOESNOTEXIST", "statement": "?"}]
    t = traceability.build_trace(_reqs(), ats)
    assert t["ok"] is False
    assert "AT-099" in t["orphaned"]


def test_unlinked_acceptance_test_is_orphaned():
    ats = _ats() + [{"id": "AT-050", "statement": "no ref"}]  # linked to nothing
    t = traceability.build_trace(_reqs(), ats)
    assert "AT-050" in t["orphaned"]


def test_unverified_when_no_passing_test_backs_it():
    t = traceability.build_trace(_reqs(), _ats(), passing_test_refs=["AT-001"])
    # REQ-002's AT-002 is not backed by a passing test.
    assert "REQ-002" in t["unverified"]
    assert "REQ-001" not in t["unverified"]


def test_empty_spec_is_not_ok():
    t = traceability.build_trace([], [])
    assert t["ok"] is False
    assert t["total_requirements"] == 0


def test_render_md_lists_gaps():
    t = traceability.build_trace(_reqs() + [{"id": "REQ-003", "acceptance_tests": []}], _ats())
    md = traceability.render_trace_md(t, "wi-x")
    assert "Uncovered requirements" in md and "REQ-003" in md
