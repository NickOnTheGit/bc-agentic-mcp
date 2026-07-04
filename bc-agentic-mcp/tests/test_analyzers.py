"""Tests for analyzers — compiler-integrated AL analyzer merge (provenance + dedup)."""
from pathlib import Path

from bc_agentic_mcp import analyzers


def _self():
    return [
        {"code": "V0061", "message": "API page should set ODataKeyFields", "severity": "warning",
         "sourceLocation": {"file": "src/Foo.Page.al", "line": 3}},
        {"code": "V0070", "message": "field name too long", "severity": "warning",
         "sourceLocation": {"file": "src/Bar.Table.al", "line": 9}},
        {"code": "V0100", "message": "per-company upgrade of shared table", "severity": "warning",
         "sourceLocation": {"file": "src/Up.Codeunit.al", "line": 5}},
    ]


def test_normalize_tags_source_and_shape():
    raw = [{"ruleId": "LC0061", "shortMessage": "x", "file": "a.al", "line": 2}]
    out = analyzers.normalize(raw, source="compiler")
    assert out[0]["code"] == "LC0061"
    assert out[0]["source"] == "compiler"
    assert out[0]["sourceLocation"] == {"file": "a.al", "line": 2}


def test_dedup_drops_regex_rule_superseded_by_compiler():
    analyzer = analyzers.normalize([
        {"code": "LC0061", "message": "ODataKeyFields", "severity": "warning",
         "sourceLocation": {"file": "src/Foo.Page.al", "line": 4}},
    ])
    merged = analyzers.dedup([{**d, "source": "self"} for d in _self()], analyzer)
    codes = [d["code"] for d in merged]
    # V0061 dropped (LC0061 present in same file); V0070 + V0100 kept.
    assert "V0061" not in codes
    assert "LC0061" in codes and "V0070" in codes and "V0100" in codes


def test_collect_self_only_when_no_runner():
    got = analyzers.collect(Path("."), altool_status=None, self_diags=_self())
    assert got["mode"] == "self"
    assert got["analyzers"] == []
    assert got["sources"]["self"] == 3 and got["sources"]["compiler"] == 0


def test_collect_with_injected_runner_merges_compiler():
    def runner(root):
        return [{"code": "LC0061", "message": "z", "severity": "warning",
                 "sourceLocation": {"file": "src/Foo.Page.al", "line": 4}}]

    got = analyzers.collect(Path("."), self_diags=_self(), runner=runner)
    assert got["mode"] == "compiler+self"
    assert "CodeCop" in got["analyzers"] and "LinterCop" in got["analyzers"]
    codes = [d["code"] for d in got["diagnostics"]]
    assert "LC0061" in codes and "V0061" not in codes  # superseded
    assert got["sources"]["compiler"] == 1


def test_collect_runner_failure_is_contained():
    def boom(root):
        raise RuntimeError("altool crashed")

    got = analyzers.collect(Path("."), self_diags=_self(), runner=boom)
    assert got["mode"] == "self"
    assert got["analyzer_error"] == "altool crashed"
    assert len(got["diagnostics"]) == 3  # regex diags still returned
