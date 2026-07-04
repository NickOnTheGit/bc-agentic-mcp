"""H4 — feature refinement: AL parsing, claims extraction, code-reality confrontation."""
import asyncio
import json
from pathlib import Path

from bc_agentic_mcp import feature_refine
from bc_agentic_mcp.tools.feature import handle_refine_feature
from bc_agentic_mcp.workspace import specs_root

_TABLE_AL = """namespace Zig.Property;

table 11024121 RealtyObjectFacilityFDN
{
    Caption = 'Lettable Object Facilities';

    fields
    {
        field(1; RealtyObjectNo; Code[20])
        {
            Caption = 'Lettable Object No.';
            TableRelation = OGE;
        }
        field(13; NoOfSharedAccommodations; Integer)
        {
            Caption = 'No. of Shared Accommodations';
        }
        field(14; NoOfAddresses; Integer)
        {
            Caption = 'No. of Addresses';
        }
        field(15; FacilityDescription; Text[250])
        {
            CalcFormula = lookup(FacilityFDN.Description where(Code = field(RealtyObjectNo)));
            FieldClass = FlowField;
            Editable = false;
        }
    }
}
"""


def _repo(tmp_path):
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    (src / "RealtyObjectFacility.Table.al").write_text(_TABLE_AL, encoding="utf-8")
    return tmp_path


# --- parsing ---

def test_parse_table_fields(tmp_path):
    repo = _repo(tmp_path)
    fields = feature_refine.parse_table_fields(
        str(repo / "extensions" / "BaseApp" / "src" / "RealtyObjectFacility.Table.al"))
    by_id = {f["id"]: f for f in fields}
    assert by_id[14]["name"] == "NoOfAddresses"
    assert by_id[15]["flowfield"] is True and by_id[15]["editable"] is False
    assert by_id[1]["has_relation"] is True


def test_build_object_index(tmp_path):
    repo = _repo(tmp_path)
    index = feature_refine.build_object_index(repo)
    assert index["table 11024121"]["name"] == "RealtyObjectFacilityFDN"
    assert index["realtyobjectfacilityfdn"]["kind"] == "table"


# --- claims ---

def test_extract_claims():
    text = ("Add new fields in table RealtyObjectFacilityFDN (11024121). "
            "Field 220: SpaceEntryNo (Integer). Field 221: SpaceDescription (FlowField). "
            "Field 'NoOfAddresses' (14) of the new record should be filled.")
    claims = feature_refine.extract_claims("240032", text)
    assert claims["tables"][0]["number"] == "11024121"
    assert {"id": 220, "name": "SpaceEntryNo"} in claims["new_fields"]
    assert {"name": "NoOfAddresses", "id": 14} in claims["cited_fields"]


# --- confrontation ---

def test_cross_check_verifies_and_catches(tmp_path):
    repo = _repo(tmp_path)
    index = feature_refine.build_object_index(repo)
    claims = [
        # good claim: free ids + correctly cited existing field
        feature_refine.extract_claims("240032",
            "table RealtyObjectFacilityFDN (11024121) Field 220: SpaceEntryNo. "
            "Field 'NoOfAddresses' (14) is filled."),
        # bad claims: id collision (14), wrong cited id (NoOfSharedAccommodations is 13 not 99),
        # duplicate name (NoOfAddresses)
        feature_refine.extract_claims("265204",
            "table RealtyObjectFacilityFDN (11024121) Field 14: SpaceFoo. "
            "Field 220: NoOfAddresses. "
            "Field 'NoOfSharedAccommodations' (99) must sync."),
    ]
    findings = feature_refine.cross_check(claims, index)
    joined = "\n".join(findings["mismatches"])
    assert "id 14 is ALREADY USED" in joined
    assert "WRONG ID in PBI" in joined
    assert any("already exists" in r for r in findings["redundancies"])
    assert any("field id 220 'SpaceEntryNo' is FREE" in v for v in findings["verified"])
    assert any("data-model change" in e for e in findings["empiric_required"])


def test_cross_check_flags_missing_table_and_conflicts(tmp_path):
    repo = _repo(tmp_path)
    index = feature_refine.build_object_index(repo)
    claims = [
        feature_refine.extract_claims("1", "table GhostTable (99999999) Field 1: X"),
        feature_refine.extract_claims("2", "table RealtyObjectFacilityFDN (11024121) Field 220: SpaceEntryNo"),
        feature_refine.extract_claims("3", "table RealtyObjectFacilityFDN (11024121) Field 221: SpaceEntryNo"),
    ]
    findings = feature_refine.cross_check(claims, index)
    assert any("NOT FOUND in source" in m for m in findings["mismatches"])
    assert any("proposed by multiple items" in c for c in findings["conflicts"])


def test_cross_check_deterministic(tmp_path):
    repo = _repo(tmp_path)
    index = feature_refine.build_object_index(repo)
    claims = [feature_refine.extract_claims("240032",
        "table RealtyObjectFacilityFDN (11024121) Field 220: SpaceEntryNo")]
    assert feature_refine.cross_check(claims, index) == feature_refine.cross_check(claims, index)


# --- handler ---

def test_handle_refine_feature_flow(tmp_path):
    repo = _repo(tmp_path)
    cdir = specs_root(repo) / "feature-x" / "context"
    cdir.mkdir(parents=True)
    (cdir / "feature.json").write_text(json.dumps({
        "feature": {"id": 239584, "title": "Link facility to space"},
        "children": [
            {"id": 240032, "state": "Approved", "title": "Add fields",
             "description": "table RealtyObjectFacilityFDN (11024121) Field 220: SpaceEntryNo. "
                            "Field 'NoOfAddresses' (14)."},
            {"id": 240436, "state": "Removed", "title": "dead",
             "description": "table RealtyObjectFacilityFDN (11024121) Field 14: Clash"},
        ],
    }), encoding="utf-8")
    out = handle_refine_feature(str(repo), "feature-x", critique="Design holds.")
    assert out["status"] == "feature_refined"
    assert out["mismatches"] == 0  # Removed child excluded -> no collision
    assert out["verified"] >= 2
    assert Path(out["refinement_path"]).exists()
    text = Path(out["refinement_path"]).read_text(encoding="utf-8")
    assert "Design holds." in text
    assert out["next_action"]["tool"] == "bc_plan_feature"
    assert out["index_stats"]["parsed"] >= 1


def test_handle_refine_blocked_without_capture(tmp_path):
    out = handle_refine_feature(str(tmp_path), "feature-x")
    assert out["status"] == "blocked_no_capture"


def test_refine_item_flow_and_incremental_index(tmp_path):
    from bc_agentic_mcp import object_index
    from bc_agentic_mcp.tools.feature import handle_refine_item

    repo = _repo(tmp_path)
    cdir = specs_root(repo) / "wi240032" / "context"
    cdir.mkdir(parents=True)
    (cdir / "manifest.json").write_text(json.dumps({"item_id": "240032"}), encoding="utf-8")
    (cdir / "item-240032.md").write_text(
        "Add fields in table RealtyObjectFacilityFDN (11024121). "
        "Field 220: SpaceEntryNo. Field 'NoOfAddresses' (99) must be filled.",
        encoding="utf-8")
    out = handle_refine_item(str(repo), "wi240032")
    assert out["status"] == "item_refined"
    # wrong cited id (99 vs 14) -> mismatch -> next action is correction checkpoint
    assert out["mismatches"] == 1
    assert "WRONG ID" in out["mismatches_detail"][0]
    assert out["next_action"]["tool"] == "bc_checkpoint"
    assert Path(out["refinement_path"]).name == "ITEM-REFINEMENT.md"
    # index cache: second run parses 0 files (all reused), still same verdict
    first_stats = out["index_stats"]
    out2 = handle_refine_item(str(repo), "wi240032")
    assert out2["index_stats"]["parsed"] == 0
    assert out2["index_stats"]["reused"] == first_stats["files"]
    assert out2["mismatches"] == 1
    # touching a file invalidates only that file
    al = repo / "extensions" / "BaseApp" / "src" / "RealtyObjectFacility.Table.al"
    al.write_text(al.read_text(encoding="utf-8") + "\n// touch\n", encoding="utf-8")
    out3 = handle_refine_item(str(repo), "wi240032")
    assert out3["index_stats"]["parsed"] == 1


def test_refine_item_blocked_without_capture(tmp_path):
    from bc_agentic_mcp.tools.feature import handle_refine_item
    out = handle_refine_item(str(tmp_path), "wi-x")
    assert out["status"] == "blocked_no_capture"
    assert out["next_action"]["tool"] == "bc_capture_item_context"


def test_claims_are_deduplicated():
    text = ("table RealtyObjectFacilityFDN (11024121) ... table RealtyObjectFacilityFDN (11024121) "
            "Field 220: SpaceEntryNo Field 220: SpaceEntryNo "
            "field 'NoOfAddresses' (14) field 'NoOfAddresses' (14)")
    claims = feature_refine.extract_claims("1", text)
    assert len(claims["tables"]) == 1
    assert len(claims["new_fields"]) == 1
    assert len(claims["cited_fields"]) == 1


def test_context_pack_targets_and_dependency_ring(tmp_path):
    repo = _repo(tmp_path)
    # add a referenced object so rank-2 resolution has something to find
    src = repo / "extensions" / "BaseApp" / "src"
    (src / "Facility.Table.al").write_text(
        'table 11024000 FacilityFDN\n{\n    fields\n    {\n        field(1; Code; Code[10]) { }\n    }\n}\n',
        encoding="utf-8")
    from bc_agentic_mcp import object_index
    index = object_index.refresh(repo)["objects"]
    claims = [feature_refine.extract_claims(
        "240032", "table RealtyObjectFacilityFDN (11024121) Field 220: SpaceEntryNo")]
    pack = feature_refine.context_pack(claims, index)
    assert pack[0]["rank"] == "1" and "RealtyObjectFacilityFDN" in pack[0]["object"]
    # the table's source references FacilityFDN -> rank 2
    assert any(p["rank"] == "2" and "FacilityFDN" in p["object"] for p in pack)


# --- enforcement engine ---

def test_refinement_engine_blocks_then_passes(tmp_path):
    from bc_agentic_mcp import enforcement
    from bc_agentic_mcp.tools.feature import handle_refine_item

    repo = _repo(tmp_path)
    cdir = specs_root(repo) / "wi240032" / "context"
    cdir.mkdir(parents=True)
    (cdir / "manifest.json").write_text(
        json.dumps({"item_id": "240032", "captured_at": "2026-07-03T00:00:00+00:00"}),
        encoding="utf-8")
    (cdir / "item-240032.md").write_text(
        "table RealtyObjectFacilityFDN (11024121) Field 220: SpaceEntryNo. "
        "Field 'NoOfAddresses' (99).", encoding="utf-8")

    # 1) never ran -> blocked with bc_refine_item as next action
    st = enforcement.engine_status(repo, "wi240032")
    assert st["engines"]["refinement"]["ok"] is False
    assert st["next_actions"][0]["tool"] in {"bc_refine_item", "bc_capture_item_context"}

    # 2) ran with mismatches and NO critique -> still blocked (silence != acceptance)
    handle_refine_item(str(repo), "wi240032")
    st = enforcement.engine_status(repo, "wi240032")
    assert st["engines"]["refinement"]["ok"] is False
    assert "NO recorded judgment" in st["engines"]["refinement"]["reason"]

    # 3) ran with critique addressing findings -> engine passes
    handle_refine_item(str(repo), "wi240032",
                       critique="Cited id 99 is wrong (source id 14): corrected in spec inputs.")
    st = enforcement.engine_status(repo, "wi240032")
    assert st["engines"]["refinement"]["ok"] is True


def test_refinement_engine_stale_after_recapture(tmp_path):
    from bc_agentic_mcp import enforcement
    from bc_agentic_mcp.tools.feature import handle_refine_item

    repo = _repo(tmp_path)
    cdir = specs_root(repo) / "wi-x" / "context"
    cdir.mkdir(parents=True)
    (cdir / "manifest.json").write_text(
        json.dumps({"item_id": "7", "captured_at": "2026-07-03T00:00:00+00:00"}), encoding="utf-8")
    (cdir / "item-7.md").write_text("table RealtyObjectFacilityFDN (11024121)", encoding="utf-8")
    handle_refine_item(str(repo), "wi-x")
    # re-capture LATER than the refinement -> stale
    (cdir / "manifest.json").write_text(
        json.dumps({"item_id": "7", "captured_at": "2099-01-01T00:00:00+00:00"}), encoding="utf-8")
    st = enforcement.engine_status(repo, "wi-x")
    assert st["engines"]["refinement"]["ok"] is False
    assert "stale" in st["engines"]["refinement"]["reason"]


def test_plan_warns_without_refinement(tmp_path):
    from bc_agentic_mcp.tools.feature import handle_plan_feature
    cdir = specs_root(tmp_path) / "feature-y" / "context"
    cdir.mkdir(parents=True)
    (cdir / "feature.json").write_text(json.dumps({
        "feature": {"id": 1, "title": "F"},
        "children": [{"id": 2, "state": "Approved", "title": "t", "description": "d"}],
    }), encoding="utf-8")
    out = asyncio.run(handle_plan_feature(str(tmp_path), "feature-y"))
    assert "UNVERIFIED claims" in out["warning"]
