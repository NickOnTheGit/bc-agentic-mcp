"""bc_mine_precedents — ADO delivery-history mining + the precedents wall.

Contracts:
- deterministic: same fake ADO responses -> byte-identical precedents.json (two runs)
- ranking: BM25 score DESC, id ASC on ties; zero-score candidates never surface
- chain: WIQL pool -> titles -> relations (PR artifacts) -> iteration changes -> shape
- distillation: object kinds by suffix (TableExt never counts as Table), upgrade/
  permission/test/xlf flags, aggregate percentages
- THE WALL: bc_plan_design fail-closes (blocked_precedents_due) for ADO-backed specs
  until evidence exists; mined-empty opens it; explicit skip+reason opens it;
  skip WITHOUT reason is refused; non-ADO specs (no captured identity) are exempt
"""
import asyncio
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import precedents
from bc_agentic_mcp.tools.mine_precedents import handle_mine_precedents


# ---------------------------------------------------------------------------
# Fake ADO: minimal deterministic REST surface
# ---------------------------------------------------------------------------

def _fake_ado(pr_paths_by_item=None):
    """Returns (get, post) fetchers over a tiny canned ADO."""
    pr_paths_by_item = pr_paths_by_item or {}

    def post(url, headers, body):
        if "wit/wiql" in url:
            return 200, json.dumps({"workItems": [
                {"id": 301}, {"id": 302}, {"id": 303}, {"id": 304}]})
        if "workitemsbatch" in url:
            return 200, json.dumps({"value": [
                {"id": 301, "fields": {"System.Title": "Facilities per space toggle on contract"}},
                {"id": 302, "fields": {"System.Title": "Facilities per space maintenance page"}},
                {"id": 303, "fields": {"System.Title": "Completely unrelated invoicing rounding"}},
                {"id": 304, "fields": {"System.Title": "Facilities per space toggle on contract"}},
            ]})
        return 404, ""

    def get(url, headers):
        for item_id, cfg in pr_paths_by_item.items():
            if f"wit/workitems/{item_id}?" in url:
                return 200, json.dumps({"relations": [
                    {"rel": "ArtifactLink",
                     "url": f"vstfs:///Git/PullRequestId/proj%2F{cfg['repo']}%2F{cfg['pr']}"},
                    {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": "vstfs:///other"},
                ]})
            if f"pullRequests/{cfg['pr']}/iterations?" in url and cfg["repo"] in url:
                return 200, json.dumps({"value": [{"id": 1}, {"id": 2}]})
            if f"pullRequests/{cfg['pr']}/iterations/2/changes" in url and cfg["repo"] in url:
                return 200, json.dumps({"changeEntries": [
                    {"item": {"path": p}} for p in cfg["paths"]]})
        if "wit/workitems/" in url:
            return 200, json.dumps({"relations": []})
        return 404, ""

    return get, post


_PATHS_301 = [
    "/extensions/BaseApp/src/Housing/Contract.Table.al",
    "/extensions/BaseApp/src/Housing/Contract.Page.al",
    "/extensions/BaseApp/src/Housing/UpgradeFacilities.Codeunit.al",
    "/extensions/BaseApp/src/Permissions/Housing.PermissionSet.al",
    "/extensions/TestApp/src/Housing/ContractTests.Codeunit.al",
    "/extensions/BaseApp/Translations/Base.nl-NL.xlf",
]


def _mine_kwargs(**over):
    base = dict(org_url="https://dev.azure.com/org", project="Proj",
                item_id="400", item_type="Product Backlog Item",
                title="Facilities per space toggle", pat="fake")
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Core mining
# ---------------------------------------------------------------------------

def test_ranking_score_desc_id_asc_zero_dropped():
    get, post = _fake_ado()
    result = precedents.mine(**_mine_kwargs(get=get, post=post))
    ids = [p["id"] for p in result["precedents"]]
    # 301 and 304 have IDENTICAL titles (tie) -> lower id first; 303 has zero
    # overlap with the query -> never surfaces.
    assert 303 not in ids
    assert ids.index(301) < ids.index(304)
    assert result["candidate_pool"] == 4


def test_full_chain_distills_delivery_shape():
    get, post = _fake_ado({301: {"repo": "a1b2c3d4-0000-1111-2222-333344445555", "pr": "9001",
                                 "paths": _PATHS_301}})
    result = precedents.mine(**_mine_kwargs(get=get, post=post))
    p301 = next(p for p in result["precedents"] if p["id"] == 301)
    assert p301["pr_ids"] == [9001]
    assert p301["files_changed"] == 6
    kinds = dict(p301["object_kinds"])
    assert kinds["Codeunit"] == 2 and kinds["Table"] == 1 and kinds["PermissionSet"] == 1
    assert p301["has_upgrade_codeunit"] is True
    assert p301["touched_permissions"] is True
    assert p301["touched_tests"] is True
    assert p301["touched_xlf"] is True
    shape = result["delivery_shape"]
    assert shape["based_on"] == 1
    assert shape["pct_with_upgrade_codeunit"] == 100.0


def test_mining_is_deterministic_byte_identical(tmp_path):
    get, post = _fake_ado({301: {"repo": "a1b2c3d4-0000-1111-2222-333344445555", "pr": "9001",
                                 "paths": _PATHS_301}})
    runs = []
    for i in range(2):
        result = precedents.mine(**_mine_kwargs(get=get, post=post))
        result["as_of"] = "PINNED"  # the one legitimate wall-clock field
        runs.append(json.dumps(result, indent=2, sort_keys=False))
    assert runs[0] == runs[1]


def test_classify_path_suffix_precedence():
    assert precedents.classify_path("a/B.TableExt.al") == "TableExtension"
    assert precedents.classify_path("a/B.Table.al") == "Table"
    assert precedents.classify_path("a/B.PermissionSetExt.al") == "PermissionSet"
    assert precedents.classify_path("a/readme.md") is None
    assert precedents.classify_path("a/Weird.al") == "OtherAL"


def test_top_dirs_count_al_files_only():
    """Retro-test finding (218219): 977 web-test files drowned the dirs histogram.
    Non-AL paths still count in files_changed/flags but never in top_dirs."""
    paths = (
        [f"/cdsa.testing.web/pages/Page{i}.cs" for i in range(50)]
        + ["/extensions/BaseApp/src/Housing/Contract.Table.al"]
    )
    shape = precedents.distill_item(paths)
    assert shape["files_changed"] == 51
    assert shape["al_files"] == 1
    assert shape["top_dirs"] == [("extensions/baseapp", 1)]


def test_mined_empty_is_still_evidence(tmp_path):
    """Zero similar items is a valid, gate-opening answer."""
    payload = {"mined": True, "precedents": [], "delivery_shape": {"based_on": 0},
               "as_of": "t"}
    precedents.save(tmp_path, "spec-a", payload)
    ev = precedents.evidence_status(tmp_path, "spec-a")
    assert ev == {"present": True, "kind": "mined", "precedents": 0, "based_on": 0}


# ---------------------------------------------------------------------------
# The tool: identity defaults + explicit skip
# ---------------------------------------------------------------------------

def _write_manifest(root: Path, spec: str, identity: dict, item_id="400"):
    cdir = root / ".specs" / spec / "context"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "manifest.json").write_text(json.dumps({
        "spec_name": spec, "item_id": item_id, "identity": identity,
        "references": {}, "files": {}, "unresolved": [], "complete": True,
    }), encoding="utf-8")


def test_tool_skip_requires_reason(tmp_path):
    out = asyncio.run(handle_mine_precedents(str(tmp_path), "s1", skip=True))
    assert out["status"] == "blocked_skip_needs_reason"
    out = asyncio.run(handle_mine_precedents(
        str(tmp_path), "s1", skip=True, reason="greenfield module, no history exists"))
    assert out["status"] == "skipped" and out["recorded"] is True
    ev = precedents.evidence_status(tmp_path, "s1")
    assert ev["kind"] == "skipped"


def test_tool_blocks_without_identity_or_ado(tmp_path, monkeypatch):
    out = asyncio.run(handle_mine_precedents(str(tmp_path), "s2"))
    assert out["status"] == "blocked_no_identity"
    _write_manifest(tmp_path, "s2", {"type": "Bug", "title": "Rounding error"})
    for var in ("AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_EXT_PAT"):
        monkeypatch.delenv(var, raising=False)
    out = asyncio.run(handle_mine_precedents(str(tmp_path), "s2"))
    assert out["status"] == "blocked_no_ado_access"


# ---------------------------------------------------------------------------
# THE WALL in bc_plan_design
# ---------------------------------------------------------------------------

_GROUNDED_SPEC = {
    "schema_version": "2.0",
    "spec_id": "s3",
    "spec_name": "s3",
    "summary": {"goal": "test", "in_scope": [], "out_of_scope": []},
    "objects_to_create": [
        {"type": "Table", "name": "TestTable", "target": "src/TestTable.Table.al"},
    ],
    "objects_to_modify": [],
    "requirements": [
        {"id": "REQ-001", "statement": "test", "acceptance_tests": ["AT-001"]}
    ],
    "acceptance_tests": [
        {"id": "AT-001", "requirement_ref": "REQ-001", "statement": "test"}
    ],
    "business_rules": [
        {"id": "BR-001", "description": "expose the toggle"}
    ],
    "event_subscribers": [],
    "scope_boundaries": {
        "allowed_extensions": ["src"],
        "allowed_files": ["src/TestTable.Table.al"],
        "scope_mode": "strict",
    },
    "traceability": {
        "requirement_to_test": {"REQ-001": ["AT-001"]},
        "requirement_to_object": {"REQ-001": ["OBJ-001"]},
        "field_to_object": {},
    },
}


def _write_spec(root: Path, spec_name: str):
    sdir = root / ".specs" / spec_name
    sdir.mkdir(parents=True, exist_ok=True)
    spec = dict(_GROUNDED_SPEC, spec_name=spec_name, spec_id=spec_name)
    (sdir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")


def test_plan_design_blocks_ado_backed_spec_without_precedents(tmp_path):
    from bc_agentic_mcp.tools.plan_design import handle_plan_design
    _write_spec(tmp_path, "s3")
    _write_manifest(tmp_path, "s3", {"type": "Product Backlog Item", "title": "Toggle"})
    out = asyncio.run(handle_plan_design(str(tmp_path), "s3"))
    assert out["status"] == "blocked_precedents_due"
    assert out["next_action"]["tool"] == "bc_mine_precedents"
    # Evidence recorded -> the wall opens.
    precedents.save(tmp_path, "s3", {"mined": True, "precedents": [],
                                     "delivery_shape": {"based_on": 0}, "as_of": "t"})
    out = asyncio.run(handle_plan_design(str(tmp_path), "s3"))
    assert out.get("status") != "blocked_precedents_due"


def test_plan_design_accepts_explicit_skip(tmp_path):
    from bc_agentic_mcp.tools.plan_design import handle_plan_design
    _write_spec(tmp_path, "s4")
    _write_manifest(tmp_path, "s4", {"type": "Bug", "title": "Crash"})
    precedents.save(tmp_path, "s4", {"skipped": True, "reason": "brand-new module",
                                     "as_of": "t"})
    out = asyncio.run(handle_plan_design(str(tmp_path), "s4"))
    assert out.get("status") != "blocked_precedents_due"


def test_plan_design_exempts_non_ado_specs(tmp_path):
    """No captured ADO identity -> no history to mine -> no wall (fixtures, ad-hoc work)."""
    from bc_agentic_mcp.tools.plan_design import handle_plan_design
    _write_spec(tmp_path, "s5")
    out = asyncio.run(handle_plan_design(str(tmp_path), "s5"))
    assert out.get("status") != "blocked_precedents_due"
