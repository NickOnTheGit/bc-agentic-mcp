"""Schema-3 index: table of contents, precomputed detail, index-only rendering."""
import json
from pathlib import Path

from bc_agentic_mcp import code_pack, object_index
from bc_agentic_mcp.tools.feature import handle_repo_map

_TABLE = """table 11024121 RealtyObjectFacilityFDN
{
    Caption = 'Lettable Object Facilities';
    DataClassification = CustomerContent;
    LookupPageId = FacilitiesOfRealtyObjectFDN;

    fields
    {
        field(1; RealtyObjectNo; Code[20]) { TableRelation = OGE; }
        field(14; NoOfAddresses; Integer) { }
        field(15; FacilityDescription; Text[250])
        {
            FieldClass = FlowField;
            Editable = false;
        }
    }
    keys
    {
        key(PK; RealtyObjectNo, EntryNo) { }
    }

    trigger OnInsert()
    begin
    end;

    local procedure UpdateCounts(SpaceEntryNo: Integer): Boolean
    begin
    end;
}
"""


def _repo(tmp_path):
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    (src / "RealtyObjectFacility.Table.al").write_text(_TABLE, encoding="utf-8")
    return tmp_path


def test_schema3_detail_precomputed(tmp_path):
    repo = _repo(tmp_path)
    data = object_index.refresh(repo)
    entry = data["objects"]["table 11024121"]
    d = entry["detail"]
    assert d["caption"] == "Lettable Object Facilities"
    assert any("DataClassification" in p for p in d["props"])
    assert {f["id"] for f in d["fields"]} == {1, 14, 15}
    assert d["fields"][2]["flowfield"] is True and d["fields"][2]["editable"] is False
    assert any("key(PK" in k for k in d["keys"])
    assert any("UpdateCounts" in p for p in d["procedures"])
    assert any("trigger OnInsert" in p for p in d["procedures"])


def test_signature_rendered_from_index_without_file(tmp_path):
    repo = _repo(tmp_path)
    entry = object_index.refresh(repo)["objects"]["table 11024121"]
    # delete the source: schema-3 rendering must NOT need it
    Path(entry["file"]).unlink()
    sig = code_pack.render_signature(entry)
    assert "field(15; FacilityDescription; Text[250])  [FlowField, NotEditable]" in sig
    assert "local procedure UpdateCounts" in sig


def test_schema_bump_forces_full_rebuild(tmp_path):
    repo = _repo(tmp_path)
    object_index.refresh(repo)
    cache = object_index.index_path(repo)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["schema"] = 1  # simulate an old cache
    cache.write_text(json.dumps(payload), encoding="utf-8")
    data = object_index.refresh(repo)
    assert data["stats"]["parsed"] == data["stats"]["files"]  # full reparse


def test_toc_search_by_name_and_caption(tmp_path):
    repo = _repo(tmp_path)
    objects = object_index.refresh(repo)["objects"]
    by_name = object_index.toc_search(objects, "facility")
    assert by_name and "RealtyObjectFacilityFDN" in by_name[0]["object"]
    by_caption = object_index.toc_search(objects, "lettable object")
    assert by_caption and by_caption[0]["caption"] == "Lettable Object Facilities"
    assert object_index.toc_search(objects, "nonexistentxyz") == []
    assert object_index.toc_search(objects, "facility", kind="page") == []


def test_handle_repo_map_modes(tmp_path):
    repo = _repo(tmp_path)
    out = handle_repo_map(str(repo), query="facility")
    assert out["status"] == "repo_map" and out["match_count"] == 1
    out = handle_repo_map(str(repo), object_key="table 11024121")
    assert "Caption = 'Lettable Object Facilities'" in out["signature"]
    out = handle_repo_map(str(repo), object_key="table 999")
    assert out["status"] == "not_found"


# --- disk/memory caching behaviour ---

def test_ttl_fast_path_skips_walk(tmp_path):
    repo = _repo(tmp_path)
    object_index.refresh(repo)  # build + memo
    out = object_index.refresh(repo, max_age_seconds=3600)
    assert out["stats"].get("fast_path") is True
    assert out["stats"]["parsed"] == 0
    assert "table 11024121" in out["objects"]
    # TTL=0 always reconciles with the filesystem (no fast_path marker)
    out = object_index.refresh(repo, max_age_seconds=0)
    assert "fast_path" not in out["stats"]


def test_cache_not_rewritten_when_unchanged(tmp_path):
    repo = _repo(tmp_path)
    object_index.refresh(repo)
    cache = object_index.index_path(repo)
    first_mtime = cache.stat().st_mtime_ns
    object_index.refresh(repo)  # no changes -> no rewrite
    assert cache.stat().st_mtime_ns == first_mtime
    # touching a source file DOES rewrite the cache
    al = repo / "extensions" / "BaseApp" / "src" / "RealtyObjectFacility.Table.al"
    al.write_text(al.read_text(encoding="utf-8") + "\n// touch\n", encoding="utf-8")
    object_index.refresh(repo)
    assert cache.stat().st_mtime_ns != first_mtime


def test_edges_memoized_per_cache_generation(tmp_path):
    repo = _repo(tmp_path)
    data = object_index.refresh(repo)
    e1 = object_index.edges_for(repo, data)
    e2 = object_index.edges_for(repo, data)
    assert e1 is e2  # same object: built once, reused
    # a repo change invalidates the memoized graph
    al = repo / "extensions" / "BaseApp" / "src" / "RealtyObjectFacility.Table.al"
    al.write_text(al.read_text(encoding="utf-8") + "\n// touch\n", encoding="utf-8")
    data2 = object_index.refresh(repo)
    e3 = object_index.edges_for(repo, data2)
    assert e3 is not e1


# --- extension-object indexing (the 'FeatureExt extends FeatureSAN' bug) ---

_ENUMEXT = '''namespace Zig.Foundation;

enumextension 11234914 FeatureExt extends FeatureSAN
{
    value(11234915; RentIncreaseAmountCapping)
}
'''


def test_extension_objects_index_with_clean_name(tmp_path):
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    (src / "Feature.EnumExt.al").write_text(_ENUMEXT, encoding="utf-8")
    data = object_index.refresh(tmp_path)
    hit = data["objects"].get("featureext")
    assert hit is not None, "extension object must be findable by its CLEAN name"
    assert hit["kind"] == "enumextension" and hit["number"] == "11234914"
    assert hit["detail"]["extends"] == "FeatureSAN"
    # the polluted alias must NOT exist
    assert "featureext extends featuresan" not in data["objects"]


def test_resolver_grounds_extension_via_index(tmp_path):
    from bc_agentic_mcp import object_resolver
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    (src / "Feature.EnumExt.al").write_text(_ENUMEXT, encoding="utf-8")
    object_index.refresh(tmp_path)
    resolved = object_resolver.resolve(tmp_path, [{"name": "FeatureExt", "kind": "enumextension"}])
    assert resolved[0]["resolved"] is True
    assert resolved[0]["object_id"] == 11234914
