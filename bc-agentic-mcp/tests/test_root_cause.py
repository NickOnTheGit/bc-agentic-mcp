"""Bugfix lane tests: bc_root_cause, the root_cause enforcement engine, lane detection,
bug-aware capture, the bugfix spec regression requirement, and the archive learning loop."""
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import advance, enforcement, item_context, lessons, workflow_policy
from bc_agentic_mcp.tools.archive import handle_archive
from bc_agentic_mcp.tools.root_cause import handle_root_cause
from bc_agentic_mcp.tools.write_spec import handle_write_spec


@pytest.fixture(autouse=True)
def _colocated(monkeypatch):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    yield


def _capture_bug(tmp: Path, spec: str = "bug-1", *, lane_type: str = "Bug") -> Path:
    """Materialize a captured-context manifest with a Bug identity (bugfix lane)."""
    cdir = tmp / ".specs" / spec / "context"
    cdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "spec_name": spec,
        "item_id": "267600",
        "identity": {"id": "267600", "type": lane_type, "title": "It broke",
                     **({"lane": "bugfix"} if lane_type == "Bug" else {})},
        "captured_at": "2026-01-01T00:00:00+00:00",
        "files": [], "unresolved": [], "complete": True,
    }
    (cdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (cdir / "item-267600.md").write_text("Bug: wrong behavior.\n\n## Repro Steps\nDo X.", encoding="utf-8")
    return tmp / ".specs" / spec


def _seed_al_repo(tmp: Path) -> Path:
    """A minimal AL repo the object index can resolve evidence against."""
    al = tmp / "extensions" / "BaseApp" / "src" / "MyTestTable.Table.al"
    al.parent.mkdir(parents=True, exist_ok=True)
    al.write_text('table 50100 MyTestTableFDN\n{\n  fields { field(1; "No."; Code[20]) { } }\n}\n',
                  encoding="utf-8")
    return al


# ---------------------------------------------------------------------------
# bc_root_cause handler
# ---------------------------------------------------------------------------

def test_root_cause_blocked_without_capture(tmp_path):
    res = handle_root_cause(str(tmp_path), "bug-x", "s", "rc", ["a.al"], "fix")
    assert res["blocked"] is True
    assert res["status"] == "blocked_no_capture"
    assert res["next_action"]["tool"] == "bc_capture_item_context"


def test_root_cause_blocked_on_missing_judgment_fields(tmp_path):
    _capture_bug(tmp_path)
    res = handle_root_cause(str(tmp_path), "bug-1", "", "rc", ["a.al"], "fix")
    assert res["status"] == "blocked_incomplete"


def test_root_cause_blocked_without_evidence(tmp_path):
    _capture_bug(tmp_path)
    res = handle_root_cause(str(tmp_path), "bug-1", "symptom", "cause", [], "fix")
    assert res["status"] == "blocked_no_evidence"


def test_root_cause_fails_closed_on_unverifiable_evidence(tmp_path):
    _capture_bug(tmp_path)
    _seed_al_repo(tmp_path)
    res = handle_root_cause(str(tmp_path), "bug-1", "symptom", "cause",
                            ["does/not/Exist.al", "table 99999"], "fix")
    assert res["status"] == "blocked_evidence_unverified"
    assert set(res["unverified"]) == {"does/not/Exist.al", "table 99999"}
    assert res["next_action"]["tool"] == "bc_root_cause"


def test_root_cause_happy_path_writes_artifacts(tmp_path):
    sdir = _capture_bug(tmp_path)
    al = _seed_al_repo(tmp_path)
    rel = str(al.relative_to(tmp_path))
    res = handle_root_cause(
        str(tmp_path), "bug-1",
        "Job queue fails on July 1st",
        "Feature codeunit still schedules the obsolete valuation update",
        [rel, "table 50100", "MyTestTableFDN"],
        "Remove the feature registration and its scheduler hook",
        regression_risk="Other feature registrations in the same codeunit",
    )
    assert res["status"] == "root_cause_recorded"
    assert res["evidence_verified"] == 3
    assert res["next_action"]["tool"] == "bc_write_spec"
    data = json.loads((sdir / "root_cause.json").read_text(encoding="utf-8"))
    assert data["lane"] == "bugfix"
    assert all(e["verified"] for e in data["evidence"])
    md = (sdir / "ROOT-CAUSE.md").read_text(encoding="utf-8")
    assert "[VERIFIED]" in md and "regression requirement" in md


# ---------------------------------------------------------------------------
# Lane detection + bug-aware capture
# ---------------------------------------------------------------------------

def test_lane_defaults_to_pbi_without_context(tmp_path):
    assert item_context.lane(str(tmp_path), "nope") == "pbi"


def test_lane_derived_from_bug_identity(tmp_path):
    _capture_bug(tmp_path, "bug-1")
    assert item_context.lane(str(tmp_path), "bug-1") == "bugfix"


def test_lane_pbi_for_non_bug_identity(tmp_path):
    _capture_bug(tmp_path, "pbi-1", lane_type="Product Backlog Item")
    assert item_context.lane(str(tmp_path), "pbi-1") == "pbi"


def test_fetch_work_item_composes_bug_repro_fields():
    def fake_fetcher(url, headers):
        return 200, json.dumps({
            "fields": {
                "System.Title": "Bug title",
                "System.Description": "",
                "Microsoft.VSTS.TCM.ReproSteps": "<div>1. Open page<br/>2. Boom</div>",
                "Microsoft.VSTS.TCM.SystemInfo": "<p>BC26 NL</p>",
            },
            "relations": [],
        })
    wi = item_context.fetch_work_item(org_url="https://dev.azure.com/x", project="P",
                                      item_id="267600", fetcher=fake_fetcher)
    assert wi["fetched"] is True
    assert "## Repro Steps" in wi["description"]
    assert "2. Boom" in wi["description"]
    assert "## System Info" in wi["description"]


# ---------------------------------------------------------------------------
# root_cause enforcement engine (lane-conditional)
# ---------------------------------------------------------------------------

def test_engine_green_for_pbi_lane(tmp_path):
    _capture_bug(tmp_path, "pbi-1", lane_type="Product Backlog Item")
    st = enforcement.engine_status(tmp_path, "pbi-1")
    assert st["engines"]["root_cause"]["ok"] is True
    assert st["engines"]["root_cause"]["detail"]["required"] is False


def test_engine_blocks_bug_without_root_cause(tmp_path):
    _capture_bug(tmp_path, "bug-1")
    st = enforcement.engine_status(tmp_path, "bug-1")
    eng = st["engines"]["root_cause"]
    assert eng["ok"] is False
    assert eng["next_action"]["tool"] == "bc_root_cause"
    assert any(b.startswith("root_cause") for b in st["blocking"])


def test_engine_green_after_root_cause_recorded(tmp_path):
    _capture_bug(tmp_path, "bug-1")
    al = _seed_al_repo(tmp_path)
    handle_root_cause(str(tmp_path), "bug-1", "s", "rc",
                      [str(al.relative_to(tmp_path))], "fix")
    eng = enforcement.engine_status(tmp_path, "bug-1")["engines"]["root_cause"]
    assert eng["ok"] is True
    assert eng["detail"]["lane"] == "bugfix"


def test_engine_flags_stale_root_cause_after_recapture(tmp_path):
    sdir = _capture_bug(tmp_path, "bug-1")
    al = _seed_al_repo(tmp_path)
    handle_root_cause(str(tmp_path), "bug-1", "s", "rc",
                      [str(al.relative_to(tmp_path))], "fix")
    manifest_path = sdir / "context" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["captured_at"] = "2999-01-01T00:00:00+00:00"  # context re-captured later
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    eng = enforcement.engine_status(tmp_path, "bug-1")["engines"]["root_cause"]
    assert eng["ok"] is False
    assert "stale" in eng["reason"]


# ---------------------------------------------------------------------------
# Bugfix spec: spec_type + mandatory symptom-regression requirement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_spec_bug_lane_emits_regression_requirement(tmp_path):
    _capture_bug(tmp_path, "bug-1")
    al = _seed_al_repo(tmp_path)
    handle_root_cause(str(tmp_path), "bug-1", "valuation job runs after feature removal",
                      "obsolete registration", [str(al.relative_to(tmp_path))], "remove it")
    res = await handle_write_spec(
        str(tmp_path), "bug-1",
        human_bullets="Remove the obsolete valuation feature registration from MyTestTableFDN.",
        idempotency_key="bug-1-fix-1",
    )
    spec = json.loads((tmp_path / ".specs" / "bug-1" / "spec.json").read_text(encoding="utf-8"))
    assert spec["spec_type"] == "bugfix"
    unwanted = [r for r in spec["requirements"] if r.get("ears_type") == "unwanted"]
    assert unwanted, "bugfix spec must carry the symptom-regression requirement"
    assert "valuation job runs after feature removal" in unwanted[0]["statement"]
    at_ids = unwanted[0]["acceptance_tests"]
    ats = {t["id"]: t for t in spec["acceptance_tests"]}
    assert any("al-regression" in ats[i]["statement"] for i in at_ids)


# ---------------------------------------------------------------------------
# Removal (decommission) work — the typical bugfix shape
# ---------------------------------------------------------------------------

def test_classify_detects_removal_work():
    from bc_agentic_mcp import work_extraction as wx
    types = wx.classify("Remove feature X so it is not visible anymore. "
                        "Remove the associated codeunit 11282398 FooHSG. Not needed anymore.")
    assert "removal" in types


def test_remove_verb_wins_over_codeunit_create_heuristic():
    from bc_agentic_mcp import work_extraction as wx
    objs = wx.extract_objects(
        "Remove codeunit 11282398 CreatePropValAtFirstOfJulyHSG. "
        "Create upgrade codeunit JobQueueCleanupPropValHSG.")
    by_name = {o["name"]: o for o in objs if o.get("name")}
    assert by_name["CreatePropValAtFirstOfJulyHSG"]["action"] == "remove"
    assert by_name["JobQueueCleanupPropValHSG"]["action"] == "create"
    assert by_name["JobQueueCleanupPropValHSG"].get("subtype") == "upgrade"


def test_removing_a_value_from_an_object_is_a_modify():
    from bc_agentic_mcp import work_extraction as wx
    objs = wx.extract_objects(
        "Remove enumextension 11282361 HousingFeatureHSG value 11282361 "
        "UpdatePropertyValuationsJulyFirst so the feature is not visible anymore.")
    by_name = {o["name"]: o for o in objs if o.get("name")}
    assert by_name["HousingFeatureHSG"]["action"] == "modify"


def test_plural_enumerated_removals_extract_every_object():
    """'Remove test codeunits 51118 X and 51117 Y' names TWO objects — the plural
    form silently dropped both from scope (observed live on Bug 267600)."""
    from bc_agentic_mcp import work_extraction as wx
    objs = wx.extract_objects(
        "Remove test codeunits 51118 UpdatePropertyValFeatureHSGT and "
        "51117 CreatePropValAtFirstOfJulyHSGT (EmpireHousingTests).")
    by_name = {o["name"]: o for o in objs if o.get("name")}
    assert by_name["UpdatePropertyValFeatureHSGT"]["action"] == "remove"
    assert by_name["UpdatePropertyValFeatureHSGT"]["id"] == "51118"
    assert by_name["CreatePropValAtFirstOfJulyHSGT"]["action"] == "remove"
    assert by_name["CreatePropValAtFirstOfJulyHSGT"]["id"] == "51117"


def test_explicit_path_honored_for_hsg_affixed_name():
    from bc_agentic_mcp import work_extraction as wx
    objs = wx.extract_objects(
        "Create upgrade codeunit JobQueueCleanupPropValHSG "
        "(extensions/EmpireHousing/src/Features/JobQueueCleanupPropVal.Codeunit.al): "
        "per-company data upgrade.")
    o = next(x for x in objs if x.get("name") == "JobQueueCleanupPropValHSG")
    assert o.get("path") == "extensions\\EmpireHousing\\src\\Features\\JobQueueCleanupPropVal.Codeunit.al"


def test_prose_after_object_id_is_not_a_name():
    """'deleting entries for codeunit 11282398 are deleted' must not yield a phantom
    object named 'are' (observed live: blocked a valid spec)."""
    from bc_agentic_mcp import work_extraction as wx
    objs = wx.extract_objects(
        "Job queue entries for codeunit 11282398 are deleted by the upgrade.")
    assert all(o.get("name") != "are" for o in objs)


# ---------------------------------------------------------------------------
# bc_implement_delete — the decommission twin of the guarded write path
# ---------------------------------------------------------------------------

def _authorized_removal_spec(tmp: Path, spec: str, rel_target: str) -> Path:
    """Spec folder with an approved gating decision + fresh review + a Remove order."""
    sdir = tmp / ".specs" / spec
    (sdir / "approvals").mkdir(parents=True, exist_ok=True)
    spec_json = {
        "spec_name": spec,
        "objects_to_modify": [
            {"type": "Codeunit", "target": rel_target, "name": "DeadWorkerFDN",
             "change": "Remove DeadWorkerFDN", "resolved": True},
        ],
        "scope_boundaries": {"allowed_files": [rel_target], "allowed_extensions": ["extensions"],
                             "scope_mode": "strict"},
    }
    (sdir / "spec.json").write_text(json.dumps(spec_json), encoding="utf-8")
    (sdir / "approvals" / "implement.md").write_text("**Status:** approve\n", encoding="utf-8")
    (sdir / "quality_gate.json").write_text(json.dumps({"pass": True}), encoding="utf-8")
    (sdir / "REVIEW.md").write_text("# review\n", encoding="utf-8")
    return sdir


@pytest.mark.asyncio
async def test_implement_delete_blocked_without_approval(tmp_path):
    from bc_agentic_mcp.tools.implement import handle_implement_delete
    rel = "extensions\\BaseApp\\src\\DeadWorker.Codeunit.al"
    sdir = _authorized_removal_spec(tmp_path, "bug-1", rel)
    (sdir / "approvals" / "implement.md").write_text("**Status:** pending\n", encoding="utf-8")
    res = await handle_implement_delete(str(tmp_path), "bug-1", rel)
    assert res["status"] == "blocked_needs_approval"


@pytest.mark.asyncio
async def test_implement_delete_refuses_files_outside_the_removal_plan(tmp_path):
    from bc_agentic_mcp.tools.implement import handle_implement_delete
    rel = "extensions\\BaseApp\\src\\DeadWorker.Codeunit.al"
    _authorized_removal_spec(tmp_path, "bug-1", rel)
    other = tmp_path / "extensions" / "BaseApp" / "src" / "Innocent.Codeunit.al"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("codeunit 50999 InnocentFDN { }", encoding="utf-8")
    res = await handle_implement_delete(str(tmp_path), "bug-1",
                                        "extensions\\BaseApp\\src\\Innocent.Codeunit.al")
    assert res["status"] == "blocked_not_in_removal_plan"
    assert other.exists()


@pytest.mark.asyncio
async def test_implement_delete_removes_file_with_backup(tmp_path):
    from bc_agentic_mcp.tools.implement import handle_implement_delete
    rel = "extensions\\BaseApp\\src\\DeadWorker.Codeunit.al"
    _authorized_removal_spec(tmp_path, "bug-1", rel)
    target = tmp_path / "extensions" / "BaseApp" / "src" / "DeadWorker.Codeunit.al"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("codeunit 50101 DeadWorkerFDN { }", encoding="utf-8")
    res = await handle_implement_delete(str(tmp_path), "bug-1", rel)
    assert res["status"] == "deleted"
    assert not target.exists()
    backup = Path(res["backup"])
    assert backup.exists()
    assert "DeadWorkerFDN" in backup.read_text(encoding="utf-8")
    res2 = await handle_implement_delete(str(tmp_path), "bug-1", rel)
    assert res2["status"] == "already_absent"


@pytest.mark.asyncio
async def test_write_spec_removal_bug_grounds_removals_and_cleanup_upgrade(tmp_path):
    _capture_bug(tmp_path, "bug-1")
    al = _seed_al_repo(tmp_path)
    # A second object that the bug orders removed.
    dead = tmp_path / "extensions" / "BaseApp" / "src" / "DeadWorker.Codeunit.al"
    dead.write_text("codeunit 50101 DeadWorkerFDN\n{\n  trigger OnRun() begin end;\n}\n",
                    encoding="utf-8")
    handle_root_cause(str(tmp_path), "bug-1", "obsolete worker still scheduled",
                      "never decommissioned", ["codeunit 50101"], "remove it + cleanup job queue")
    res = await handle_write_spec(
        str(tmp_path), "bug-1",
        human_bullets=(
            "Remove codeunit 50101 DeadWorkerFDN (extensions/BaseApp/src/DeadWorker.Codeunit.al). "
            "Create upgrade codeunit JobQueueCleanupFDN "
            "(extensions/BaseApp/src/_Upgrade/JobQueueCleanup.Codeunit.al): data upgrade that "
            "deletes every matching Job Queue Entry. data target table: \"Job Queue Entry\""
        ),
        idempotency_key="bug-1-removal-1",
    )
    assert res.get("machine_spec_path"), f"spec build failed: {res}"
    spec = json.loads((tmp_path / ".specs" / "bug-1" / "spec.json").read_text(encoding="utf-8"))
    assert spec["spec_type"] == "bugfix"
    removes = [o for o in spec["objects_to_modify"] if str(o.get("change", "")).startswith("Remove")]
    assert removes and removes[0]["name"] == "DeadWorkerFDN"
    assert any("no longer contain codeunit DeadWorkerFDN" in r["statement"]
               for r in spec["requirements"])
    uc = spec["upgrade_contract"]
    assert uc["table_target"] == "Job Queue Entry"
    assert uc["required_scope"] == "per-company"


# ---------------------------------------------------------------------------
# Spec-time test pyramid + API-surface reverse lookup (the "learn and enforce" walls)
# ---------------------------------------------------------------------------

def test_extract_declared_tests_parses_shape_lines():
    from bc_agentic_mcp import work_extraction as wx
    declared = wx.extract_declared_tests(
        "Some intro.\n"
        "TEST happy: GIVEN a link WHEN set THEN description shows.\n"
        "- TEST negative: WHEN the space belongs to another object THEN an error blocks it.\n"
        "TEST edge: WHEN set twice THEN unchanged.\n"
        "TEST regression: existing flows unchanged.\n")
    shapes = [d["shape"] for d in declared]
    assert shapes == ["happy", "negative", "edge", "regression"]
    assert "another object" in declared[1]["scenario"]


def test_declared_test_lines_never_classify_as_work():
    """'TEST api: …' is proof, not work — it must not route the item into the API
    template (observed live: produced an empty spec on Bug 267600 fix-8)."""
    from bc_agentic_mcp import work_extraction as wx
    work = wx.summarize(
        "Remove codeunit 11282398 CreatePropValAtFirstOfJulyHSG.\n"
        "TEST api: GIVEN the zig/admin features API WHEN queried THEN 11282361 is absent.\n"
        "TEST negative: WHEN other types share the id THEN cleanup cannot delete them.\n")
    assert "api" not in work["work_types"]
    assert "removal" in work["work_types"]


@pytest.mark.asyncio
async def test_declared_tests_flow_into_spec_with_path_shape(tmp_path):
    _capture_bug(tmp_path, "bug-1")
    _seed_al_repo(tmp_path)
    handle_root_cause(str(tmp_path), "bug-1", "sym", "rc",
                      ["extensions/BaseApp/src/MyTestTable.Table.al"], "fix")
    await handle_write_spec(
        str(tmp_path), "bug-1",
        human_bullets=(
            "Remove codeunit 50100 MyTestTableFDN — wait, modify table MyTestTableFDN.\n"
            "TEST happy: GIVEN the fix WHEN the flow runs THEN it works.\n"
            "TEST negative: WHEN an invalid id is used THEN an error is raised.\n"
            "TEST edge: WHEN the cleanup runs twice THEN nothing changes.\n"
        ),
        idempotency_key="bug-1-shapes-1",
    )
    spec = json.loads((tmp_path / ".specs" / "bug-1" / "spec.json").read_text(encoding="utf-8"))
    declared = [t for t in spec["acceptance_tests"] if t.get("path_shape")]
    assert {t["path_shape"] for t in declared} == {"happy", "negative", "edge"}


def test_quality_gate_blocks_incomplete_declared_shapes():
    from bc_agentic_mcp.tools.prepare_review import _evaluate_quality_gates
    spec = {
        "requirements": [{"id": "REQ-001", "ears_type": "ubiquitous",
                          "statement": "The system shall do the thing.",
                          "acceptance_tests": ["AT-001"]}],
        "acceptance_tests": [{"id": "AT-001", "requirement_ref": "REQ-001",
                              "statement": "GIVEN a thing THEN it works."}],  # happy only
        "objects_to_modify": [{"name": "X", "target": "a.al"}],
        "scope_boundaries": {"allowed_files": ["a.al"]},
    }
    gate = _evaluate_quality_gates(spec, [], None, 2, 1, False)
    assert gate["checks"]["test_shapes_declared"] is False
    assert any("negative" in f and "edge" in f for f in gate["failures"])


def test_quality_gate_passes_with_all_shapes():
    from bc_agentic_mcp.tools.prepare_review import _evaluate_quality_gates
    spec = {
        "requirements": [{"id": "REQ-001", "ears_type": "ubiquitous",
                          "statement": "The system shall do the thing.",
                          "acceptance_tests": ["AT-001", "AT-002", "AT-003"]}],
        "acceptance_tests": [
            {"id": "AT-001", "requirement_ref": "REQ-001", "path_shape": "happy",
             "statement": "[happy] works"},
            {"id": "AT-002", "requirement_ref": "REQ-001", "path_shape": "negative",
             "statement": "[negative] invalid input errors"},
            {"id": "AT-003", "requirement_ref": "REQ-001", "path_shape": "edge",
             "statement": "[edge] twice is idempotent"},
        ],
        "objects_to_modify": [{"name": "X", "target": "a.al"}],
        "scope_boundaries": {"allowed_files": ["a.al"]},
    }
    gate = _evaluate_quality_gates(spec, [], None, 2, 1, False)
    assert gate["checks"]["test_shapes_declared"] is True
    assert gate["test_shape_counts"] == {"happy": 1, "negative": 1, "edge": 1}


def _seed_api_repo(tmp: Path) -> None:
    """Enum ext touched by the spec + an API page sourcing the extended enum's base."""
    base = tmp / "extensions" / "App" / "src"
    base.mkdir(parents=True, exist_ok=True)
    (base / "HousingFeature.EnumExt.al").write_text(
        'enumextension 50200 HousingFeatureHSG extends FeatureSAN\n{\n'
        '    value(50201; SomethingHSG) { }\n}\n', encoding="utf-8")
    (base / "FeatureManagementAPI.Page.al").write_text(
        'page 50210 FeatureManagementAPIOPN\n{\n'
        '    PageType = API;\n    SourceTable = FeatureSAN;\n'
        '    layout { area(Content) { } }\n}\n', encoding="utf-8")


def test_api_pages_touching_finds_surface_via_extends_target(tmp_path):
    from bc_agentic_mcp import verification
    _seed_api_repo(tmp_path)
    spec = {"objects_to_modify": [{"name": "HousingFeatureHSG",
                                   "target": "extensions\\App\\src\\HousingFeature.EnumExt.al",
                                   "change": "Extend HousingFeatureHSG"}]}
    touching = verification.api_pages_touching(tmp_path, spec)
    assert touching, "the API page sourcing the extends-target must be detected"
    assert "FeatureManagementAPIOPN" in touching[0]


def test_api_contract_class_forced_by_touching_surface(tmp_path):
    from bc_agentic_mcp import verification
    _seed_api_repo(tmp_path)
    spec = {"objects_to_modify": [{"name": "HousingFeatureHSG",
                                   "target": "extensions\\App\\src\\HousingFeature.EnumExt.al"}]}
    classes = verification.validation_class_status(tmp_path, "bug-x", [], spec)
    api = classes["api-contract"]
    assert api["required"] is True, "an API surface touching the artifact forces the class"
    assert api["ok"] is False
    assert "FeatureManagementAPIOPN" in (api.get("touching_api_pages") or [""])[0]


@pytest.mark.asyncio
async def test_multi_target_upgrade_contracts_with_index_derived_scope(tmp_path):
    """Two cleanup upgrades in one bug: per-company (base table, AL default) and
    per-database (in-repo table with DataPerCompany=false resolved via the index).
    An ENUM of the same name shadows the table in the index name keys (observed
    live: enum FeatureSAN vs table FeatureSAN) — derivation must find the TABLE."""
    _capture_bug(tmp_path, "bug-1")
    base = tmp_path / "extensions" / "App" / "src"
    base.mkdir(parents=True, exist_ok=True)
    # Sorted first -> wins the name key: the enum shadows the table on purpose here.
    (base / "Feature.Enum.al").write_text(
        'enum 50299 FeatureSAN\n{\n    value(0; None) { }\n}\n', encoding="utf-8")
    (base / "Feature.Table.al").write_text(
        'table 50300 FeatureSAN\n{\n    DataPerCompany = false;\n'
        '    fields { field(1; Id; Integer) { } }\n}\n', encoding="utf-8")
    handle_root_cause(str(tmp_path), "bug-1", "sym", "rc",
                      ["extensions/App/src/Feature.Table.al"], "fix")
    res = await handle_write_spec(
        str(tmp_path), "bug-1",
        human_bullets=(
            "- Create upgrade codeunit CleanupJobsHSG "
            "(extensions/App/src/CleanupJobs.Codeunit.al): data upgrade deleting job queue "
            "entries. data target table: \"Job Queue Entry\"\n"
            "- Create upgrade codeunit CleanupFeatureRowHSG "
            "(extensions/App/src/CleanupFeatureRow.Codeunit.al): data upgrade deleting the "
            "leftover feature record. data target table: FeatureSAN\n"
            "TEST happy: GIVEN leftovers WHEN the upgrades run THEN they are removed.\n"
            "TEST negative: WHEN other records exist THEN they are not touched (error if over-deleted).\n"
            "TEST edge: WHEN the upgrades run twice THEN nothing changes.\n"
        ),
        idempotency_key="bug-1-multi-upgrade-1",
    )
    assert res.get("machine_spec_path"), f"spec build failed: {res}"
    spec = json.loads((tmp_path / ".specs" / "bug-1" / "spec.json").read_text(encoding="utf-8"))
    contracts = {c["codeunit_target"]: c for c in spec["upgrade_contracts"]}
    jobs = contracts["extensions\\App\\src\\CleanupJobs.Codeunit.al"]
    feature = contracts["extensions\\App\\src\\CleanupFeatureRow.Codeunit.al"]
    assert jobs["required_scope"] == "per-company"          # base table -> AL default
    assert feature["required_scope"] == "per-database"      # index-resolved DataPerCompany=false
    # Per-file resolution picks the right contract for each codeunit.
    from bc_agentic_mcp.spec_loader import upgrade_contract_for_file
    governed, c = upgrade_contract_for_file(spec, "extensions/App/src/CleanupFeatureRow.Codeunit.al")
    assert governed and c["required_scope"] == "per-database"


def test_clarification_rewrite_preserves_other_answered_questions(tmp_path):
    """Writing Q-902 must not erase the answered Q-001 (observed live: two-writer
    ping-pong wiped answers and deadlocked the plan gate). Unanswered foreign
    questions keep their HEADINGS too — dropping them makes the answer tool
    report not_found on the next call."""
    from bc_agentic_mcp.tools.prepare_review import (_clarification_answer,
                                                     _write_clarification_file)
    sdir = tmp_path / ".specs" / "wi-1"
    sdir.mkdir(parents=True)
    (sdir / "clarifications.md").write_text(
        "# Clarifications for: wi-1\n\nReview these questions before implementation.\n\n"
        "## Q-001: Should deletion be allowed?\n"
        "_Answer:_ Yes, bounded to codeunit 11282398 (src/A.al).\n\n"
        "## Q-777: Unanswered but must keep its heading?\n"
        "_Answer:_ \n",
        encoding="utf-8")
    _write_clarification_file(
        specs_dir=sdir, spec_name="wi-1",
        questions=[{"id": "Q-902", "question": "Dirty tree - bypass?", "type": "text"}],
        code_examples=[], inferred_rules={})
    assert "bounded to codeunit 11282398" in _clarification_answer(sdir, "Q-001")
    assert _clarification_answer(sdir, "Q-902") == ""
    content = (sdir / "clarifications.md").read_text(encoding="utf-8")
    assert "## Q-777:" in content, "unanswered foreign heading must survive the rewrite"


@pytest.mark.asyncio
async def test_removal_target_prefers_declared_path_after_deletion(tmp_path):
    """Regenerating a removal spec AFTER the file was deleted must keep the declared
    bullet path — the resolver falls back to symbol-package internals otherwise
    (observed live: src/… paths corrupted allowed_files and blocked the commit gate)."""
    _capture_bug(tmp_path, "bug-1")
    _seed_al_repo(tmp_path)  # repo exists, but the removal target file does NOT
    handle_root_cause(str(tmp_path), "bug-1", "sym", "rc",
                      ["extensions/BaseApp/src/MyTestTable.Table.al"], "fix")
    res = await handle_write_spec(
        str(tmp_path), "bug-1",
        human_bullets=(
            "Remove codeunit 50777 AlreadyGoneFDN "
            "(extensions/BaseApp/src/AlreadyGone.Codeunit.al).\n"
            "TEST happy: GIVEN the removal WHEN compiled THEN clean.\n"
            "TEST negative: WHEN anything references it THEN an error is raised.\n"
            "TEST edge: WHEN removed twice THEN nothing changes.\n"
        ),
        idempotency_key="bug-1-gone-1",
    )
    assert res.get("machine_spec_path"), f"spec build failed: {res}"
    spec = json.loads((tmp_path / ".specs" / "bug-1" / "spec.json").read_text(encoding="utf-8"))
    removes = [o for o in spec["objects_to_modify"]
               if str(o.get("change", "")).startswith("Remove AlreadyGone")]
    assert removes
    assert removes[0]["target"] == "extensions\\BaseApp\\src\\AlreadyGone.Codeunit.al"
    assert "extensions\\BaseApp\\src\\AlreadyGone.Codeunit.al" in spec["scope_boundaries"]["allowed_files"]


def test_spaced_extension_kind_forms_are_extracted():
    """'enum extension X' (spaced, human phrasing) must extract like 'enumextension X' —
    the spaced form extracted NOTHING and three modifies fell out of scope
    (observed live on Bug 267600 fix-11: scope_violation on the enum restore).
    Suspicion sweep found the same blindness for permission set / xml port /
    report extension."""
    from bc_agentic_mcp.work_extraction import extract_objects
    objs = extract_objects(
        "Modify enum extension HousingFeatureHSG "
        "(extensions/EmpireHousing/src/Features/HousingFeature.EnumExt.al): restore value.\n"
        "Modify table extension RealtyObjectHSG with a new field.\n"
         'Modify permission set "2C-ALG-PAGINA ALLEN" to add the page.\n'
        "Create xml port SplitElementsHSG for the import.\n"
        "Modify report extension RentIncreaseDocExtHSG columns.\n"
    )
    kinds = {(o["kind"], o["name"]) for o in objs}
    assert ("enumextension", "HousingFeatureHSG") in kinds
    assert ("tableextension", "RealtyObjectHSG") in kinds
    assert ("permissionset", "2C-ALG-PAGINA ALLEN") in kinds
    assert ("xmlport", "SplitElementsHSG") in kinds
    assert ("reportextension", "RentIncreaseDocExtHSG") in kinds


# ---------------------------------------------------------------------------
# PIPELINE-TRUTH wall 3: dependent-closure build (pure parts)
# ---------------------------------------------------------------------------

def _fake_apps():
    from pathlib import Path as _P
    return {
        "Base": {"id": "1", "dir": _P("extensions/Base"), "dependencies": [], "test_app": False},
        "Housing": {"id": "2", "dir": _P("extensions/Housing"), "dependencies": ["Base"], "test_app": False},
        "HousingTests": {"id": "3", "dir": _P("extensions/HousingTests"), "dependencies": ["Housing"], "test_app": True},
        "Unrelated": {"id": "4", "dir": _P("extensions/Unrelated"), "dependencies": ["Base"], "test_app": False},
    }


def test_dependents_closure_expands_to_test_app():
    """Changing Housing must pull in HousingTests (the exact live gap: the test app
    referenced a deleted codeunit and only broke at container publish)."""
    from bc_agentic_mcp.dependent_build import dependents_closure
    got = dependents_closure(_fake_apps(), {"Housing"})
    assert got == {"Housing", "HousingTests"}


def test_build_order_puts_dependencies_first():
    from bc_agentic_mcp.dependent_build import build_order
    order = build_order(_fake_apps(), {"HousingTests", "Housing", "Base"})
    assert order.index("Base") < order.index("Housing") < order.index("HousingTests")


def test_dependent_build_gate_fails_on_dependent_errors(tmp_path, monkeypatch):
    """gate() must refuse when a DEPENDENT of a changed app has compile errors."""
    from bc_agentic_mcp import dependent_build as db
    monkeypatch.setattr(db, "discover_apps", lambda root: _fake_apps())
    monkeypatch.setattr(db, "changed_apps", lambda root, target_branch="master": {"Housing"})
    def compile_fn(app_dir):
        return ([{"message": "AL0185: Codeunit 'Gone' is missing", "severity": "error"}]
                if "HousingTests" in str(app_dir) else [])
    res = db.gate(tmp_path, compile_fn=compile_fn)
    assert not res["ok"]
    assert res["failures"][0]["app"] == "HousingTests"
    assert "AL0185" in res["failures"][0]["first_errors"][0]


def test_dependent_build_gate_truncates_but_still_checks_direct_dependents(tmp_path, monkeypatch):
    """An oversized closure must TRUNCATE (seeds + direct dependents first), never
    skip: fail-open-on-size gave EmpireHousing (20 transitive dependents) ZERO
    protection exactly where the observed bug class lives (its own test app)."""
    from pathlib import Path as _P
    from bc_agentic_mcp import dependent_build as db
    apps = _fake_apps()
    apps["HousingApi"] = {"id": "5", "dir": _P("extensions/HousingApi"),
                          "dependencies": ["Housing"], "test_app": False}
    apps["HousingApiTests"] = {"id": "6", "dir": _P("extensions/HousingApiTests"),
                               "dependencies": ["HousingApi"], "test_app": True}
    monkeypatch.setattr(db, "discover_apps", lambda root: apps)
    monkeypatch.setattr(db, "changed_apps", lambda root, target_branch="master": {"Housing"})
    def compile_fn(app_dir):
        return ([{"message": "AL0185: Codeunit 'Gone' is missing", "severity": "error"}]
                if str(app_dir).endswith("HousingTests") else [])
    # closure = {Housing, HousingTests, HousingApi, HousingApiTests} = 4 > max_apps=3
    res = db.gate(tmp_path, compile_fn=compile_fn, max_apps=3)
    assert not res["ok"], "direct dependent must still be compiled under truncation"
    assert res["failures"][0]["app"] == "HousingTests"
    assert "truncated" in res.get("note", "")
    assert "HousingApiTests" not in res["checked"], "transitive tail is the pipeline's job"


def test_namespace_wall_mirrors_team_cmdlet_exactly():
    """BC-NS-MISSING mirrors Test-cdsaMissingNamespacesIncr: first line must contain
    'namespace Zig.', paths containing 'Test' are exempt, deleted files are exempt.
    (Naive draft falsified live: our namespace-less TEST codeunit passed build 257443
    because of the Test exemption — the rule was fetched from ERP.PSModules source.)"""
    from bc_agentic_mcp.breaking_change import scan_missing_namespaces
    diff = (
        "diff --git a/extensions/EmpireHousing/src/A.Codeunit.al b/extensions/EmpireHousing/src/A.Codeunit.al\n"
        "index 111..222 100644\n@@ -1,1 +1,2 @@\n+code\n"
        "diff --git a/extensions/EmpireHousingTests/src/T.Codeunit.al b/extensions/EmpireHousingTests/src/T.Codeunit.al\n"
        "index 111..222 100644\n@@ -1,1 +1,2 @@\n+code\n"
        "diff --git a/extensions/EmpireHousing/src/Gone.Codeunit.al b/extensions/EmpireHousing/src/Gone.Codeunit.al\n"
        "deleted file mode 100644\n@@ -1,3 +0,0 @@\n-namespace Zig.X;\n"
    )
    files = {
        "extensions/EmpireHousing/src/A.Codeunit.al": "codeunit 1 X\n{\n}\n",  # NO namespace
        "extensions/EmpireHousingTests/src/T.Codeunit.al": "codeunit 2 Y\n{\n}\n",  # exempt (Test)
    }
    got = scan_missing_namespaces(diff, read_file=lambda p: files.get(p))
    assert [f["file"] for f in got] == ["extensions/EmpireHousing/src/A.Codeunit.al"]
    # compliant first line passes
    files["extensions/EmpireHousing/src/A.Codeunit.al"] = "namespace Zig.Housing.X;\n\ncodeunit 1 X\n{\n}\n"
    assert scan_missing_namespaces(diff, read_file=lambda p: files.get(p)) == []


def test_prepare_pr_renders_explicit_test_table(tmp_path, monkeypatch):
    """The golden template must LIST each executed test (name, what it validates,
    status) — an aggregate '8/8' forced reviewers to open the test files (user
    requirement 2026-07-04). Source: executed_tests on the newest test checkpoint."""
    import asyncio
    from bc_agentic_mcp.tools.pr import handle_prepare_pr
    from bc_agentic_mcp import verification
    from bc_agentic_mcp.workspace import specs_root as _sr
    d = _sr(tmp_path) / "wi-dm"
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps({
        "feature_name": "wi-dm", "work_types": ["table-field"],
        "objects_to_modify": [{"type": "Table", "name": "X", "target": "src/X.Table.al"}],
    }), encoding="utf-8")
    (d / "data_model_approval.json").write_text(
        json.dumps({"approved": True, "approver": "dev2"}), encoding="utf-8")
    monkeypatch.setattr(verification, "gate", lambda root, spec: {
        "passed": True, "blockers": [],
        "digest": {"rows": [], "coverage_pct": 100, "criteria_count": 1,
                   "required_strength_label": "container", "tests_recorded": 2},
    })
    from bc_agentic_mcp.tools import pr as pr_mod
    monkeypatch.setattr(pr_mod, "_reviewer_gate", lambda root, spec: None)
    from bc_agentic_mcp import breaking_change, dependent_build
    monkeypatch.setattr(breaking_change, "gate", lambda root, target_branch="master": {"ok": True, "findings": []})
    monkeypatch.setattr(dependent_build, "gate", lambda root, target_branch="master": {"ok": True, "failures": []})
    verification.record_test(
        tmp_path, "wi-dm", name="AL run 2/2 (acctest)", result="pass", covers="all",
        layer="al-unit", evidence="container=acctest passed=2/2",
        executed_tests=[
            {"codeunit": "XTests", "test": "JobQueueEntriesForRemovedWorkerAreDeletedByUpgrade",
             "shape": "happy", "result": "success"},
            {"codeunit": "XTests", "test": "UpgradeCannotDeleteEntriesOfOtherObjectTypes",
             "shape": "negative", "result": "success"},
        ])
    out = asyncio.run(handle_prepare_pr(str(tmp_path), "wi-dm"))
    assert out["status"] == "pr_prepared"
    desc = out["description"]
    # Description carries a POINTER (ADO 4000-char cap); the explicit table lives in
    # PR-TESTS.md and is posted as a comment thread by bc_create_pr.
    assert "first comment on this PR" in desc
    from bc_agentic_mcp import pr as _pr_core
    tests_md = _pr_core.pr_dir(tmp_path, "wi-dm") / "PR-TESTS.md"
    assert tests_md.exists()
    table = tests_md.read_text(encoding="utf-8")
    # ADO-safe plain list (no pipe tables — they do not render in comment threads),
    # human sentences, explicit status words, and NO local file references.
    assert "JobQueueEntriesForRemovedWorkerAreDeletedByUpgrade — PASSED" in table
    # Humanized sentence (camel-split, trailing period); recorded shape honored.
    assert "Validates (happy path): Job queue entries for removed worker are deleted by upgrade." in table
    assert "Validates (negative (must be refused)):" in table
    assert "|" not in table, "pipe tables do not render in ADO comment threads"
    assert "TEST-REPORT" not in table, "no local file references for the PR reviewer"


def test_branch_spec_match_by_work_item_number():
    """'user/bug-267600-remove-x' must match spec 'bug267600-x': the work-item
    number is the identity join key (live false negative blocked an approved commit)."""
    from bc_agentic_mcp.gate import _branch_matches_spec
    assert _branch_matches_spec(        "nicolae-catalina/bug-267600-remove-property-valuation-feature",
        "bug267600-property-valuation-feature",
    )
    # different numbers must NOT match
    assert not _branch_matches_spec("user/bug-267601-x", "bug267600-x")
    # digit run is a whole token: 26760 vs 267600 must NOT match
    assert not _branch_matches_spec("user/bug-26760-x", "bug267600-x")


@pytest.mark.asyncio
async def test_answering_one_question_never_eats_the_next_heading(tmp_path):
    """Answering a question whose CURRENT answer is empty must not delete the next
    question's heading: the old '\\s*' tail crossed newlines and [^\\n]* swallowed
    '## Q-001: ...' (observed live — answered questions vanished, plan gate ping-pong)."""
    from bc_agentic_mcp.tools.answer_clarification import handle_answer_clarification
    from bc_agentic_mcp.workspace import specs_root as _sr
    specs_dir = _sr(tmp_path) / "item-x"
    specs_dir.mkdir(parents=True)
    (specs_dir / "clarifications.md").write_text(
        "# Clarifications for: item-x\n\n"
        "## Q-902: dirty tree question\n"
        "_Answer:_ \n\n"
        "## Q-001: Should deletion be allowed?\n"
        "_Answer:_ Deletion allowed. src/A.al\n",
        encoding="utf-8",
    )
    res = await handle_answer_clarification(
        str(tmp_path), "item-x",
        answers={"Q-902": "BYPASS: item's own approved files. src/A.al"},
    )
    assert res["written"] == ["Q-902"]
    text = (specs_dir / "clarifications.md").read_text(encoding="utf-8")
    assert "## Q-001: Should deletion be allowed?" in text, "next heading was eaten"
    assert "_Answer:_ Deletion allowed. src/A.al" in text
    assert "_Answer:_ BYPASS: item's own approved files. src/A.al" in text


# ---------------------------------------------------------------------------
# Archive learning loop: bug pattern -> lessons store (recurrence -> confirmed)
# ---------------------------------------------------------------------------

def _make_state(specs_dir: Path, spec_name: str) -> None:
    state = {"active_spec": spec_name, "total_specs": 1,
             "specs": {spec_name: {"name": spec_name, "phase": "implement",
                                   "created": "2026-01-01T00:00:00+00:00",
                                   "last_activity": "2026-01-01T00:00:00+00:00"}}}
    (specs_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.mark.asyncio
async def test_archive_records_bug_pattern_lesson_and_confirms_on_recurrence(tmp_path):
    al = _seed_al_repo(tmp_path)
    rel = str(al.relative_to(tmp_path))
    specs_dir = tmp_path / ".specs"
    for i, spec in enumerate(("bug-1", "bug-2"), start=1):
        _capture_bug(tmp_path, spec)
        handle_root_cause(str(tmp_path), spec, "same symptom", "same cause", [rel], "same fix")
        _make_state(specs_dir, spec)
        res = await handle_archive(str(tmp_path), spec, outcome="merged", force=True)
        assert res["status"] == "closed"
        assert res["bug_lesson"]["hits"] == i
    stored = lessons.load_lessons(tmp_path)
    bug_lessons = [l for l in stored if l["code"] == "BUG-PATTERN"]
    assert len(bug_lessons) == 1
    assert bug_lessons[0]["status"] == "confirmed"
    assert set(bug_lessons[0]["seen_in"]) == {"bug-1", "bug-2"}
    assert res["next_action"]["tool"] == "bc_promote_lesson"


# ---------------------------------------------------------------------------
# Policy + composite driver wiring
# ---------------------------------------------------------------------------

def test_planner_may_call_root_cause():
    allowed, meta = workflow_policy.check_tool_call(
        tool_name="bc_root_cause", agent_role="planner",
        project_root=None, spec_name=None)
    assert allowed is True


def test_gatekeeper_may_not_call_root_cause():
    allowed, meta = workflow_policy.check_tool_call(
        tool_name="bc_root_cause", agent_role="gatekeeper",
        project_root=None, spec_name=None)
    assert allowed is False


def test_reflect_is_callable_in_every_stage():
    """bc_checkpoint (raises reflection_due) is common; bc_reflect (clears it) must be
    too, or the reflection loop deadlocks in planning (observed live on Bug 267600)."""
    for stage_tools in workflow_policy.STAGE_ALLOWLIST.values():
        assert "bc_reflect" in stage_tools
    for role in ("planner", "implementer", "gatekeeper", "orchestrator"):
        allowed, _ = workflow_policy.check_tool_call(
            tool_name="bc_reflect", agent_role=role, project_root=None, spec_name=None)
        assert allowed is True


def test_enforcement_remediation_tools_callable_in_implement_stage():
    """Every tool an enforcement engine prescribes must be callable in the implement
    stage — the commit gate blocks on red engines there, so a stage that forbids the
    named fix deadlocks the loop (observed live: clarifications during implement)."""
    remediation = {
        "bc_capture_item_context",  # timeline engine
        "bc_refine_item",           # refinement engine
        "bc_root_cause",            # root_cause engine (bugfix lane)
        "bc_write_spec",            # traceability engine
        "bc_read_code_context",     # code_context engine
        "bc_quality_check",         # quality engine
        "bc_answer_clarification",  # clarifications engine
    }
    implement = workflow_policy.STAGE_ALLOWLIST["implement"]
    missing = sorted(remediation - implement)
    assert missing == [], f"implement stage forbids engine remediation tools: {missing}"


def test_advance_stops_for_judgment_at_root_cause_phase():
    step = advance.seed_action("root_cause_identified", {})
    assert step["stop"] == "waiting_judgment"