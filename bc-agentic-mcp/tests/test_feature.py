"""Workstream H — feature tier: capture (fetcher seam), deterministic analysis, roll-up."""
import asyncio
import json
from pathlib import Path

from bc_agentic_mcp import feature_context, feature_plan, timeline
from bc_agentic_mcp.tools.feature import (
    handle_capture_feature,
    handle_feature_status,
    handle_plan_feature,
)
from bc_agentic_mcp.workspace import specs_root


def _wi(item_id, wtype, title, state="Approved", description="", relations=None):
    return {
        "id": item_id,
        "fields": {
            "System.WorkItemType": wtype,
            "System.Title": title,
            "System.State": state,
            "System.Description": description,
        },
        "relations": relations or [],
    }


_URLS = {
    "240032": _wi(240032, "Product Backlog Item", "Add fields to RealtyObjectFacilityFDN",
                  description="Add SpaceEntryNo to table 11024121 RealtyObjectFacilityFDN. Gated by 240435.",
                  relations=[{"rel": "System.LinkTypes.Hierarchy-Reverse",
                              "url": "https://x/_apis/wit/workItems/239584"}]),
    "239584": _wi(239584, "Feature", "Link facility to space",
                  relations=[
                      {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://x/_apis/wit/workItems/240032"},
                      {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://x/_apis/wit/workItems/240435"},
                      {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://x/_apis/wit/workItems/240436"},
                  ]),
    "240435": _wi(240435, "Product Backlog Item", "Add Empire feature Facilities per Space",
                  description="Add feature toggle. Enables page 11030034 changes."),
    "240436": _wi(240436, "Product Backlog Item", "Copy facilities (dead)", state="Removed",
                  description="References table 11024121 too."),
}


def _fetcher(url, headers):
    for key, payload in _URLS.items():
        if f"/{key}?" in url:
            return 200, json.dumps(payload)
    return 404, ""


# --- capture ---

def test_capture_resolves_parent_from_child(tmp_path):
    result = feature_context.capture_feature(
        str(tmp_path), "feature-239584", work_item_id="240032",
        org_url="https://dev.azure.com/org", project="P", fetcher=_fetcher)
    assert result["captured"] is True
    assert result["feature_id"] == "239584" and result["child_count"] == 3
    tree = feature_context.load_tree(str(tmp_path), "feature-239584")
    assert tree["feature"]["title"] == "Link facility to space"
    child_md = specs_root(tmp_path) / "feature-239584" / "context" / "children" / "240435.md"
    assert child_md.exists()


def test_capture_fail_closed_without_pat(tmp_path, monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    result = feature_context.capture_feature(
        str(tmp_path), "f", work_item_id="1", org_url="https://x", project="P")
    assert result["captured"] is False and "AZURE_DEVOPS_EXT_PAT" in result["reason"]


# --- analysis (pure) ---

def _captured_tree(tmp_path):
    feature_context.capture_feature(
        str(tmp_path), "feature-239584", work_item_id="239584",
        org_url="https://dev.azure.com/org", project="P", fetcher=_fetcher)
    return feature_context.load_tree(str(tmp_path), "feature-239584")


def test_analyze_excludes_removed_and_finds_collisions(tmp_path):
    analysis = feature_plan.analyze(_captured_tree(tmp_path))
    assert analysis["item_count"] == 2  # 240436 Removed -> excluded
    assert analysis["excluded"][0]["id"] == 240436
    # 240032 mentions 240435 -> edge; both reference distinct objects
    assert {"from": "240032", "to": "240435"} in analysis["mention_edges"]
    # foundation-first: 240435 (referenced once) before 240032 (referenced zero times)
    assert [e["id"] for e in analysis["suggested_order"]] == ["240435", "240032"]


def test_analyze_is_deterministic(tmp_path):
    tree = _captured_tree(tmp_path)
    assert feature_plan.analyze(tree) == feature_plan.analyze(tree)


def test_extract_object_refs():
    refs = feature_plan.extract_object_refs(
        "Add field to table 11024121 RealtyObjectFacilityFDN and page (11030034)")
    assert "table 11024121" in refs["numbered"]
    assert "page 11030034" in refs["numbered"]
    assert "RealtyObjectFacilityFDN" in refs["names"]


# --- handlers ---

def test_plan_feature_blocked_without_capture(tmp_path):
    out = asyncio.run(handle_plan_feature(str(tmp_path), "feature-x"))
    assert out["status"] == "blocked_no_capture"
    assert out["next_action"]["tool"] == "bc_capture_feature"


def test_full_feature_flow(tmp_path):
    out = asyncio.run(handle_capture_feature(
        str(tmp_path), "feature-239584", work_item_id="240032",
        org_url="https://dev.azure.com/org", project="P"))
    # handler uses the default fetcher; patch by calling core directly instead:
    # (capture again with seam to overwrite)
    feature_context.capture_feature(
        str(tmp_path), "feature-239584", work_item_id="239584",
        org_url="https://dev.azure.com/org", project="P", fetcher=_fetcher)
    out = asyncio.run(handle_plan_feature(str(tmp_path), "feature-239584",
                                          notes="Wave 1: 240435+240032."))
    assert out["status"] == "feature_planned"
    assert Path(out["plan_path"]).exists()
    text = Path(out["plan_path"]).read_text(encoding="utf-8")
    assert "Wave 1: 240435+240032." in text
    assert out["next_action"]["tool"] == "bc_request_approval"
    assert out["next_action"]["params_hint"]["phase"] == "plan"


def test_feature_status_rollup_and_next(tmp_path):
    feature_context.capture_feature(
        str(tmp_path), "feature-239584", work_item_id="239584",
        org_url="https://dev.azure.com/org", project="P", fetcher=_fetcher)
    asyncio.run(handle_plan_feature(str(tmp_path), "feature-239584"))
    # no item folders yet -> next action = start the FIRST item in suggested order
    out = asyncio.run(handle_feature_status(str(tmp_path), "feature-239584"))
    assert out["all_archived"] is False
    assert out["next_action"]["tool"] == "bc_capture_item_context"
    assert out["next_action"]["params_hint"]["work_item_id"] == "240435"
    # simulate item folder for 240435 at some phase -> next action advances IT
    item_dir = specs_root(tmp_path) / "wi240435" / "context"
    item_dir.mkdir(parents=True)
    (item_dir / "manifest.json").write_text(json.dumps({"item_id": "240435"}), encoding="utf-8")
    timeline.record_phase(tmp_path, "wi240435", "implemented")
    out = asyncio.run(handle_feature_status(str(tmp_path), "feature-239584"))
    assert out["next_action"]["tool"] == "bc_advance"
    assert out["next_action"]["params_hint"]["spec_name"] == "wi240435"
    # archive both items -> feature closes
    timeline.record_phase(tmp_path, "wi240435", "archived")
    item2 = specs_root(tmp_path) / "wi240032" / "context"
    item2.mkdir(parents=True)
    (item2 / "manifest.json").write_text(json.dumps({"item_id": "240032"}), encoding="utf-8")
    timeline.record_phase(tmp_path, "wi240032", "archived")
    out = asyncio.run(handle_feature_status(str(tmp_path), "feature-239584"))
    assert out["all_archived"] is True and out["next_action"]["tool"] == "bc_archive"


# --- wiring facts ---

def test_feature_phases_route_to_plan_stage():
    from bc_agentic_mcp import workflow_policy as wp
    assert wp._phase_to_stage("feature_captured") == "plan"
    assert wp._phase_to_stage("feature_planned") == "plan"
    assert timeline.TOOL_PHASE["bc_capture_feature"] == "feature_captured"
    assert timeline.TOOL_PHASE["bc_plan_feature"] == "feature_planned"
