"""Pipeline-truth walls: breaking-change pre-flight + mandatory reviewer gate.

Born from build 257437 (Bug 267600): a locally-green PR failed the incremental
merge build on AS0083. These tests pin the deterministic pre-flight subset.
"""
import json
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bc_agentic_mcp.breaking_change import scan_breaking
from bc_agentic_mcp.workspace import specs_root


# ---------------------------------------------------------------------------
# scan_breaking — pure diff analysis
# ---------------------------------------------------------------------------

def _diff(path: str, body: str, deleted: bool = False) -> str:
    header = f"diff --git a/{path} b/{path}\n"
    if deleted:
        header += "deleted file mode 100644\n"
    return header + body


def test_removed_enum_value_is_flagged_as0083():
    d = _diff("extensions/EmpireHousing/src/Features/HousingFeature.EnumExt.al",
              "-    value(11282361; UpdatePropertyValuationsJulyFirst)\n"
              "-    {\n-    }\n")
    findings = scan_breaking(d)
    assert [f["code"] for f in findings] == ["BC-BREAK-ENUMVAL"]
    assert "11282361" in findings[0]["message"]


def test_value_moved_within_file_is_not_flagged():
    # remove + re-add same (num, name) = a move/edit, not a removal
    d = _diff("extensions/X/src/A.EnumExt.al",
              "-    value(10; Foo)\n+    value(10; Foo)\n")
    assert scan_breaking(d) == []


def test_value_added_in_branch_then_removed_is_invisible_to_merge_base_diff():
    # diff vs merge-base simply does not contain the value at all — nothing to flag
    d = _diff("extensions/X/src/A.EnumExt.al", "+    // unrelated edit\n")
    assert scan_breaking(d) == []


def test_removed_table_field_is_flagged():
    d = _diff("extensions/BaseApp/src/Tables/Thing.Table.al",
              '-        field(220; SpaceEntryNo; Integer)\n')
    findings = scan_breaking(d)
    assert [f["code"] for f in findings] == ["BC-BREAK-FIELD"]


def test_deleted_schema_file_is_flagged_but_deleted_codeunit_is_not():
    schema = _diff("extensions/X/src/A.Enum.al", "-enum 1 A\n-{\n-}\n", deleted=True)
    codeunit = _diff("extensions/X/src/B.Codeunit.al", "-codeunit 2 B\n-{\n-}\n", deleted=True)
    assert [f["code"] for f in scan_breaking(schema)] == ["BC-BREAK-TABLE"]
    assert scan_breaking(codeunit) == []  # pipeline accepted our codeunit deletions


def test_obsolete_pending_edit_passes():
    # the FIX for AS0083 (keep value, add obsolete properties) must pass the wall
    d = _diff("extensions/EmpireHousing/src/Features/HousingFeature.EnumExt.al",
              "+        ObsoleteReason = 'decommissioned';\n"
              "+        ObsoleteState = Pending;\n"
              "+        ObsoleteTag = '28.2610';\n")
    assert scan_breaking(d) == []


# ---------------------------------------------------------------------------
# reviewer gate — mandatory fresh internal review before PR
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# URL-encoding class sweep: every ADO URL builder must survive spaced names
# (the exact class that broke PR creation live: repo 'ERP AL', Bug 267600)
# ---------------------------------------------------------------------------

def test_all_ado_url_builders_encode_spaced_project_names():
    from bc_agentic_mcp.item_context import build_work_item_rest_url, build_comments_rest_url
    from bc_agentic_mcp.feature_context import workitem_url
    from bc_agentic_mcp.wiki import build_rest_url
    urls = [
        build_work_item_rest_url("https://dev.azure.com/org", "My Proj", "1"),
        build_comments_rest_url("https://dev.azure.com/org", "My Proj", "1"),
        workitem_url("https://dev.azure.com/org", "My Proj", "1"),
        build_rest_url("https://dev.azure.com/org", "My Proj", "My.wiki name", "42"),
    ]
    for url in urls:
        assert " " not in url, f"unencoded space in {url}"
        assert "My%20Proj" in url


def _rubric_entry(passed: bool, ts: datetime) -> dict:
    return {"ts": ts.isoformat(), "scores": {"grounding": 1.0, "coverage": 1.0,
            "conventions": 1.0, "risk": 1.0}, "overall": 1.0,
            "passed": passed, "verdict": "approve", "note": ""}


def test_reviewer_gate_blocks_without_review(tmp_path):
    from bc_agentic_mcp.tools.pr import _reviewer_gate
    (specs_root(tmp_path) / "wi-x").mkdir(parents=True)
    block = _reviewer_gate(tmp_path, "wi-x")
    assert block is not None
    assert block["status"] == "blocked_reviewer_required"
    assert block["next_action"]["tool"] == "bc_review"


def test_reviewer_gate_blocks_failed_review(tmp_path):
    from bc_agentic_mcp.tools.pr import _reviewer_gate
    d = specs_root(tmp_path) / "wi-x"
    d.mkdir(parents=True)
    (d / "review_rubric.json").write_text(json.dumps(
        [_rubric_entry(False, datetime.now(timezone.utc))]), encoding="utf-8")
    assert _reviewer_gate(tmp_path, "wi-x") is not None


def test_reviewer_gate_passes_with_fresh_passing_review(tmp_path):
    from bc_agentic_mcp.tools.pr import _reviewer_gate
    d = specs_root(tmp_path) / "wi-x"
    d.mkdir(parents=True)
    # tmp_path is not a git repo -> last_commit unknowable -> freshness check skipped
    (d / "review_rubric.json").write_text(json.dumps(
        [_rubric_entry(True, datetime.now(timezone.utc))]), encoding="utf-8")
    assert _reviewer_gate(tmp_path, "wi-x") is None
