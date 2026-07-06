"""Golden-fixture tests — REAL repo files, not synthetic AL.

The extends-swallowing bug lived for hours because synthetic fixtures never
exercised real headers. These four files are verbatim copies from the ERP AL
repo; every parser layer must handle them exactly.
"""
import asyncio
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import code_pack, object_index
from bc_agentic_mcp.workspace import specs_root

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


@pytest.fixture()
def golden_repo(tmp_path):
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    for f in GOLDEN.glob("*.al"):
        (src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_golden_index_all_objects(golden_repo):
    objects = object_index.refresh(golden_repo)["objects"]
    # enumextension with extends — the exact shape that broke schema 3/4
    ext = objects["featureext"]
    assert ext["kind"] == "enumextension" and ext["number"] == "11234914"
    assert ext["detail"]["extends"] == "FeatureSAN"
    # interface-implementing codeunit
    cu = objects["changenonnetrentfeature"]
    assert cu["kind"] == "codeunit" and cu["number"] == "11234919"
    assert any("IsActive" in p for p in cu["detail"]["procedures"])
    # big real table: exact known field facts
    table = objects["table 11024121"]
    fields = {f["id"]: f for f in table["detail"]["fields"]}
    assert fields[13]["name"] == "NoOfSharedAccommodations"
    assert fields[15]["name"] == "NoOfAddresses"
    assert fields[14]["name"] == "RealtyObjectAddress" and fields[14]["flowfield"]
    assert fields[210]["name"] == "DataSource" and fields[210]["has_relation"]
    # real page
    page = objects["facilitiesofrealtyobjectfdn"]
    assert page["kind"] == "page" and page["number"] == "11030034"


def test_golden_signatures_render_from_index(golden_repo):
    objects = object_index.refresh(golden_repo)["objects"]
    sig = code_pack.render_signature(objects["table 11024121"])
    assert "field(15; NoOfAddresses; Integer)" in sig
    assert "field(14; RealtyObjectAddress; Code[50])  [FlowField, NotEditable]" in sig
    sig = code_pack.render_signature(objects["featureext"])
    assert sig.startswith("enumextension 11234914 FeatureExt")


def test_golden_refinement_cross_check(golden_repo):
    from bc_agentic_mcp import feature_refine
    index = object_index.refresh(golden_repo)["objects"]
    claims = [feature_refine.extract_claims(
        "265204",
        "table RealtyObjectFacilityFDN (11024121): field 'NoOfAddresses' (14) must sync.")]
    findings = feature_refine.cross_check(claims, index)
    assert any("WRONG ID" in m and "id 15" in m for m in findings["mismatches"])


# --- P0.4: feature-tier plan gate ---

def _feature_folder(tmp_path, *, refinement=True, critique="ok", mismatches=0):
    d = specs_root(tmp_path) / "feature-x"
    d.mkdir(parents=True)
    (d / "approvals").mkdir()
    (d / "approvals" / "plan.md").write_text("**Status:** pending\n- [ ] approve\n", encoding="utf-8")
    (d / "FEATURE-PLAN.md").write_text("# plan", encoding="utf-8")
    (d / "feature_plan.json").write_text(json.dumps(
        {"generated_at": "2099-01-02T00:00:00"}), encoding="utf-8")
    if refinement:
        (d / "feature_refinement.json").write_text(json.dumps({
            "findings": {"counts": {"mismatches": mismatches, "conflicts": 0}},
            "critique": critique, "generated_at": "2099-01-02T00:00:00",
        }), encoding="utf-8")
    (d / "context").mkdir()
    (d / "context" / "manifest.json").write_text(json.dumps(
        {"captured_at": "2099-01-01T00:00:00"}), encoding="utf-8")
    return d


def test_feature_plan_gate_approves_with_native_artifacts(tmp_path):
    from bc_agentic_mcp.tools.approval import handle_submit_decision
    _feature_folder(tmp_path)
    out = asyncio.run(handle_submit_decision(
        str(tmp_path), "feature-x", "plan", "approve", feedback="ok"))
    assert out["status"] == "approve"
    assert "evidence_override" not in out  # NO override needed anymore


def test_feature_plan_gate_blocks_without_refinement(tmp_path):
    from bc_agentic_mcp.tools.approval import handle_submit_decision
    _feature_folder(tmp_path, refinement=False)
    out = asyncio.run(handle_submit_decision(
        str(tmp_path), "feature-x", "plan", "approve"))
    assert out["status"] == "blocked"
    assert any("refinement missing" in b for b in out["blockers"])


def test_feature_plan_gate_blocks_unjudged_mismatches(tmp_path):
    from bc_agentic_mcp.tools.approval import handle_submit_decision
    _feature_folder(tmp_path, critique="", mismatches=3)
    out = asyncio.run(handle_submit_decision(
        str(tmp_path), "feature-x", "plan", "approve"))
    assert out["status"] == "blocked"
    assert any("NO recorded judgment" in b for b in out["blockers"])


# --- P0.3: refinement claims are authoritative grounding ---

def test_spec_grounds_from_refinement_claims(golden_repo):
    from bc_agentic_mcp.tools.write_spec import handle_write_spec
    spec_dir = specs_root(golden_repo) / "wi-x"
    spec_dir.mkdir(parents=True)
    (spec_dir / "item_refinement.json").write_text(json.dumps({
        "claims": [{"item_id": "1", "tables": [
            {"name": "RealtyObjectFacilityFDN", "number": "11024121"}],
            "new_fields": [], "cited_fields": []}],
        "findings": {"counts": {}}, "critique": "",
    }), encoding="utf-8")
    # bullets deliberately DO NOT name the table in extractable form
    out = asyncio.run(handle_write_spec(
        project_root=str(golden_repo), spec_name="wi-x",
        human_bullets="- Add a new field 220 SpaceEntryNo to the facility table per the item.",
        idempotency_key="k1"))
    spec = json.loads(Path(out["machine_spec_path"]).read_text(encoding="utf-8"))
    assert spec.get("status") == "grounded"
    assert any(o.get("name") == "RealtyObjectFacilityFDN" and o.get("target")
               for o in spec.get("objects_to_modify", []))
# --- data-model merge blocker (schema changes need a second developer's sign-off) ---

def _schema_spec_dir(tmp_path, *, with_approval=None):
    d = specs_root(tmp_path) / "wi-dm"
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps({
        "feature_name": "wi-dm", "work_types": ["table-field"],
        "objects_to_modify": [{"type": "Table", "name": "X", "target": "src/X.Table.al"}],
    }), encoding="utf-8")
    if with_approval is not None:
        (d / "data_model_approval.json").write_text(json.dumps(with_approval), encoding="utf-8")
    return d


def test_data_model_gate_blocks_missing_approval(tmp_path):
    from bc_agentic_mcp.tools.approval import _data_model_gate
    d = _schema_spec_dir(tmp_path)
    assert "MISSING" in _data_model_gate(tmp_path, d)


def test_data_model_gate_blocks_ungranted(tmp_path):
    from bc_agentic_mcp.tools.approval import _data_model_gate
    d = _schema_spec_dir(tmp_path, with_approval={"approved": False, "approver": "dev2"})
    assert "NOT GRANTED" in _data_model_gate(tmp_path, d)


def test_data_model_gate_passes_when_granted(tmp_path):
    from bc_agentic_mcp.tools.approval import _data_model_gate
    d = _schema_spec_dir(tmp_path, with_approval={"approved": True, "approver": "dev2"})
    assert _data_model_gate(tmp_path, d) is None


def test_data_model_gate_ignores_non_schema_spec(tmp_path):
    from bc_agentic_mcp.tools.approval import _data_model_gate
    d = specs_root(tmp_path) / "wi-plain"
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps({
        "feature_name": "wi-plain", "work_types": ["report"],
        "objects_to_modify": [{"type": "Report", "name": "R", "target": "src/R.Report.al"}],
    }), encoding="utf-8")
    assert _data_model_gate(tmp_path, d) is None


def test_prepare_pr_warns_but_does_not_block_on_pending_data_model(tmp_path, monkeypatch):
    """The invariant is 'the PR cannot MERGE until granted' — creation is the review
    vehicle (the second developer reviews the schema change AS the PR diff), so a
    pending sign-off must produce a DO-NOT-MERGE section, not a refusal (observed
    live on Bug 267600: the creation block forced the schema review out-of-band)."""
    import asyncio
    from datetime import datetime, timezone
    from bc_agentic_mcp.tools.pr import handle_prepare_pr
    from bc_agentic_mcp import verification
    _schema_spec_dir(tmp_path)  # schema spec, NO approval artifact
    monkeypatch.setattr(verification, "gate", lambda root, spec: {
        "passed": True,
        "blockers": [],
        "digest": {"rows": [], "coverage_pct": 100, "criteria_count": 1,
                   "required_strength_label": "container", "tests_recorded": 1},
    })
    # satisfy the mandatory reviewer gate
    (specs_root(tmp_path) / "wi-dm" / "review_rubric.json").write_text(json.dumps([{
        "ts": datetime.now(timezone.utc).isoformat(),
        "scores": {"grounding": 1.0, "coverage": 1.0, "conventions": 1.0, "risk": 1.0},
        "overall": 1.0, "passed": True, "verdict": "approve", "note": "fixture",
    }]), encoding="utf-8")
    out = asyncio.run(handle_prepare_pr(str(tmp_path), "wi-dm"))
    assert out["status"] == "pr_prepared"
    assert out["data_model_approval_pending"]
    assert "data-model sign-off pending" in out["description"]
    # granted sign-off removes the warning entirely
    (specs_root(tmp_path) / "wi-dm" / "data_model_approval.json").write_text(
        json.dumps({"approved": True, "approver": "dev2"}), encoding="utf-8")
    out2 = asyncio.run(handle_prepare_pr(str(tmp_path), "wi-dm"))
    assert out2["status"] == "pr_prepared"
    assert "data_model_approval_pending" not in out2
    assert "DO NOT MERGE" not in out2["description"]

# --- feature model: install once, test by slice (-testCodeunit) ---

def test_run_command_includes_test_codeunit_slice():
    from bc_agentic_mcp.al_runner import build_run_tests_command
    cmd = build_run_tests_command(container_name="acctest", test_extension_id="abc",
                                  test_codeunit="66025")
    assert "-testCodeunit 66025" in cmd[-1]
    cmd = build_run_tests_command(container_name="acctest", test_extension_id="abc")
    assert "-testCodeunit" not in cmd[-1]

# --- stale-install gate: slice runs refuse when the branch moved past the install ---

def _install_env(tmp_path, sha="aaa111"):
    import bc_agentic_mcp.tools.run_tests as rt
    envd = specs_root(tmp_path) / ".env"
    envd.mkdir(parents=True, exist_ok=True)
    (envd / "acctest.json").write_text(json.dumps({
        "ok": True, "container_name": "acctest", "fingerprint": "f1",
        "checked_at": "2099-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    (envd / "acctest-install.json").write_text(json.dumps({
        "container": "acctest", "installed_sha": sha,
        "app_project_folder": str(tmp_path), "test_extension_id": "ext-1",
    }), encoding="utf-8")
    return rt


def test_slice_run_blocked_without_install_manifest(tmp_path, monkeypatch):
    import bc_agentic_mcp.tools.run_tests as rt
    envd = specs_root(tmp_path) / ".env"
    envd.mkdir(parents=True, exist_ok=True)
    (envd / "acctest.json").write_text(json.dumps({
        "ok": True, "container_name": "acctest", "fingerprint": "f1",
        "checked_at": "2099-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(rt.env_preflight, "require_fresh",
                        lambda root, c: {"ok": True, "manifest": {"fingerprint": "f1"}})
    out = asyncio.run(rt.handle_run_tests(
        str(tmp_path), "acctest", "ext-1", test_codeunit="66071"))
    assert out["status"] == "blocked_no_install"


def test_slice_run_blocked_on_stale_install(tmp_path, monkeypatch):
    rt = _install_env(tmp_path, sha="aaa111")
    monkeypatch.setattr(rt.env_preflight, "require_fresh",
                        lambda root, c: {"ok": True, "manifest": {"fingerprint": "f1"}})
    monkeypatch.setattr(rt, "_head_sha", lambda d: "bbb222")
    out = asyncio.run(rt.handle_run_tests(
        str(tmp_path), "acctest", "ext-1", test_codeunit="66071"))
    assert out["status"] == "blocked_stale_install"


def test_slice_run_blocked_on_foreign_install(tmp_path, monkeypatch):
    # A SIBLING worktree published to the container: same repo key, same manifest
    # path, different tree. The slice must refuse — it would test foreign binaries
    # (observed live: wi267598 "passed 3/3" against wt-240435's TestApp).
    import bc_agentic_mcp.tools.run_tests as rt
    caller_root = tmp_path / "wt-mine"
    caller_root.mkdir()
    foreign_root = tmp_path / "wt-theirs"
    foreign_root.mkdir()
    envd = specs_root(caller_root) / ".env"
    envd.mkdir(parents=True, exist_ok=True)
    (envd / "acctest.json").write_text(json.dumps({
        "ok": True, "container_name": "acctest", "fingerprint": "f1",
        "checked_at": "2099-01-01T00:00:00+00:00",
    }), encoding="utf-8")
    (envd / "acctest-install.json").write_text(json.dumps({
        "container": "acctest", "installed_sha": "aaa111",
        "app_project_folder": str(foreign_root), "test_extension_id": "ext-1",
    }), encoding="utf-8")
    monkeypatch.setattr(rt.env_preflight, "require_fresh",
                        lambda root, c: {"ok": True, "manifest": {"fingerprint": "f1"}})
    monkeypatch.setattr(rt, "_head_sha", lambda d: "aaa111")
    out = asyncio.run(rt.handle_run_tests(
        str(caller_root), "acctest", "ext-1", test_codeunit="66071"))
    assert out["status"] == "blocked_foreign_install"
    assert str(foreign_root) in out["reason"]


def test_slice_run_proceeds_on_matching_install(tmp_path, monkeypatch):
    rt = _install_env(tmp_path, sha="aaa111")
    monkeypatch.setattr(rt.env_preflight, "require_fresh",
                        lambda root, c: {"ok": True, "manifest": {"fingerprint": "f1"}})
    monkeypatch.setattr(rt, "_head_sha", lambda d: "aaa111")
    monkeypatch.setattr(rt.al_runner, "run_container_tests",
                        lambda **kw: {"executed": True, "all_passed": True,
                                      "passed": 4, "total": 4,
                                      "slice": kw.get("test_codeunit")})
    out = asyncio.run(rt.handle_run_tests(
        str(tmp_path), "acctest", "ext-1", test_codeunit="66071"))
    assert out["executed"] and out["slice"] == "66071"

# --- path-coverage validation class: happy AND negative AND edge required ---

def test_classify_test_paths_shapes():
    from bc_agentic_mcp.verification import classify_test_paths
    counts = classify_test_paths([
        "WhenImplementingWithPrerequisite_ExpectSetupFieldSet",          # happy
        "WhenImplementingWithoutPrerequisite_ExpectPrerequisiteError",   # negative
        "WhenImplementingTwice_ExpectIdempotentState",                   # edge
        "WhenSetupRecordMissing_ExpectIsImplementedFalse",               # edge (recordmissing)
        "WhenReadingDescription_ExpectTerminology",                      # happy
    ])
    assert counts == {"happy": 2, "negative": 2, "edge": 1}


def test_shape_of_prefers_declared_at_shape():
    # Retro wi267598: the spec DECLARED the no-lines scenario an edge case, but the
    # name classifier said negative ('refused'). Spec truth wins on a unique match;
    # ambiguity or no match falls back to the classifier.
    from bc_agentic_mcp.tools.run_tests import _shape_of, _stmt_tokens
    declared = [
        {"tokens": _stmt_tokens(
            "[edge] GIVEN a realty object with NO contract lines at all and header "
            "Status = Sold WHEN the creation check runs THEN creation is refused "
            "(the header-status fallback)."), "shape": "edge"},
        {"tokens": _stmt_tokens(
            "[negative] GIVEN a realty object whose newest contract line has "
            "Exploitation State Type = Sold WHEN the creation check runs THEN "
            "creation is refused with the existing status error."), "shape": "negative"},
    ]
    assert _shape_of("CreationCheck_NoContractLinesAndHeaderSold_CreationRefused", declared) == "edge"
    # No unique AT match -> classifier fallback still applies.
    assert _shape_of("SomethingUnrelated_Fails", declared) == "negative"
    assert _shape_of("PlainHappyFlow", declared) == "happy"


def test_prepare_pr_infers_branches_from_git(tmp_path, monkeypatch):
    # Retro wi267598: defaults produced feature/wi267598 -> main on a repo whose
    # truth was nicolae-catalina/wi-267598 -> master. Git wins; caller overrides win more.
    from bc_agentic_mcp.tools import pr as pr_mod

    def fake_run(cmd, **kw):
        class R:
            stdout = ""
        r = R()
        if "--show-current" in cmd:
            r.stdout = "nicolae-catalina/wi-267598\n"
        elif "symbolic-ref" in cmd:
            r.stdout = "origin/master\n"
        return r
    monkeypatch.setattr(pr_mod._sp, "run", fake_run)

    src, tgt = pr_mod._infer_branches(tmp_path, None, "main")
    assert src == "nicolae-catalina/wi-267598" and tgt == "master"
    # Explicit caller values always win.
    src, tgt = pr_mod._infer_branches(tmp_path, "my/branch", "release/21")
    assert src == "my/branch" and tgt == "release/21"


def test_path_coverage_class_blocks_happy_only(tmp_path):
    from bc_agentic_mcp import verification
    d = specs_root(tmp_path) / "wi-paths"
    d.mkdir(parents=True)
    verification.record_test(
        tmp_path, "wi-paths", name="AL run 3/3 (acctest)", result="pass", covers=[1],
        layer="al-unit",
        evidence="container=acctest ext=e passed=3/3 exit=0 mode=item paths=happy:3,negative:0,edge:0")
    classes = verification.validation_class_status(tmp_path, "wi-paths",
        [c for c in _load_tests(tmp_path, "wi-paths")], None)
    assert classes["path-coverage"]["ok"] is False
    assert "negative:0" in classes["path-coverage"]["reason"]


def test_path_coverage_class_passes_full_pyramid(tmp_path):
    from bc_agentic_mcp import verification
    d = specs_root(tmp_path) / "wi-paths2"
    d.mkdir(parents=True)
    verification.record_test(
        tmp_path, "wi-paths2", name="AL run 12/12 (acctest)", result="pass", covers=[1],
        layer="al-unit",
        evidence="container=acctest ext=e passed=12/12 exit=0 mode=item paths=happy:5,negative:3,edge:4")
    classes = verification.validation_class_status(tmp_path, "wi-paths2",
        [c for c in _load_tests(tmp_path, "wi-paths2")], None)
    assert classes["path-coverage"]["ok"] is True


def _load_tests(root, spec):
    from bc_agentic_mcp import checkpoints as memory
    return [c for c in memory.load_checkpoints(Path(root).resolve(), spec)
            if c.get("kind") == "test"]

# --- write-time guards born from the container failures ---

def test_v0030_identifier_length_cap(tmp_path):
    from bc_agentic_mcp.al_validator import validate_project
    src = tmp_path / "src"
    src.mkdir()
    (src / "Long.Codeunit.al").write_text(
        "codeunit 50000 FacilitiesPerSpaceRegressionFDNT\n{\n}\n", encoding="utf-8")
    diags = validate_project(tmp_path)
    assert any(d["code"] == "V0030" and d["severity"] == "error" for d in diags)


def test_interface_call_issues_detects_non_member(tmp_path, monkeypatch):
    from bc_agentic_mcp.tools import implement
    fake_index = {"objects": {"featurev3san": {
        "kind": "interface", "number": "", "name": "FeatureV3SAN",
        "detail": {"procedures": ["procedure IsImplemented(): Boolean",
                                   "procedure ImplementFeature() ReloadFeatureList: Boolean"]},
    }}}
    from bc_agentic_mcp import object_index
    monkeypatch.setattr(object_index, "refresh", lambda root, max_age_seconds=0: fake_index)
    code = ("codeunit 66131 T\n{\n  procedure P()\n  var\n    Feature: Interface FeatureV3SAN;\n"
            "  begin\n    Feature.IsActive();\n    Feature.IsImplemented();\n  end;\n}\n")
    issues = implement._interface_call_issues(tmp_path, code)
    assert len(issues) == 1 and "IsActive" in issues[0]


def test_repo_map_free_id_mode(tmp_path, monkeypatch):
    import asyncio
    from bc_agentic_mcp.tools import feature as feature_tools
    fake = {"objects": {
        "a": {"kind": "codeunit", "number": "66000", "name": "A"},
        "b": {"kind": "codeunit", "number": "66001", "name": "B"},
        "c": {"kind": "codeunit", "number": "66003", "name": "C"},
    }}
    monkeypatch.setattr(feature_tools.object_index, "refresh",
                        lambda root, max_age_seconds=60: fake)
    out = feature_tools.handle_repo_map(
        str(tmp_path), free_id_range="66000-66005", kind="codeunit", limit=3)
    assert out["status"] == "free_ids"
    assert out["free"] == [66002, 66004, 66005]

def test_traceability_grain_warns_on_umbrella_requirement(tmp_path, monkeypatch):
    """GL-COV002: '100% coverage' against ONE umbrella requirement is a grain illusion
    (observed live on wi240435) — quality check must surface it as a warning."""
    import json
    import bc_agentic_mcp.tools.quality_check as qc
    sd = tmp_path / ".specs" / "demo"
    sd.mkdir(parents=True)
    monkeypatch.setattr(qc, "specs_root", lambda p: tmp_path / ".specs")
    sd.joinpath("spec.json").write_text(json.dumps({"requirements": [
        {"id": "REQ-001", "statement": "The system shall implement the change described for demo."}]}),
        encoding="utf-8")
    findings = qc._traceability_grain_findings(tmp_path, "demo")
    assert len(findings) == 1
    assert findings[0]["code"] == "GL-COV002"
    assert findings[0]["severity"] == "warning"
    # Itemized specs stay silent.
    sd.joinpath("spec.json").write_text(json.dumps({"requirements": [
        {"id": "REQ-001", "statement": "The system shall expose field X on table T."},
        {"id": "REQ-002", "statement": "The system shall display X on page P."}]}),
        encoding="utf-8")
    assert qc._traceability_grain_findings(tmp_path, "demo") == []

# --- One-approval feature model (human decision 2026-07-04) ---

def _feature_with_children(tmp_path, authored, gap_item=None):
    """Feature folder + children; authored ids get full planning artifacts.

    A child with ADO state 'Removed' (103) ALWAYS exists — with a fully authored
    spec folder — to prove the state filter wins over folder existence.
    gap_item: an authored id whose acceptance tests cover ONLY the happy bucket.
    """
    d = _feature_folder(tmp_path)
    children = [{"id": cid, "title": f"Item {cid}", "state": "Approved"}
                for cid in ("101", "102")]
    children.append({"id": "103", "title": "Item 103", "state": "Removed"})
    (d / "context" / "feature.json").write_text(json.dumps(
        {"feature": {"id": 9, "title": "Mega Feature"}, "children": children}),
        encoding="utf-8")
    full_tests = lambda cid: [
        {"id": "AT-001", "requirement_ref": "REQ-001",
         "statement": f"GIVEN setup WHEN thing {cid} runs THEN it succeeds."},
        {"id": "AT-002", "requirement_ref": "REQ-001",
         "statement": "WHEN the prerequisite is absent THEN the system shows an error."},
        {"id": "AT-003", "requirement_ref": "REQ-001",
         "statement": "WHEN it is run twice THEN the result is unchanged."},
        {"id": "AT-004", "requirement_ref": "REQ-001",
         "statement": "Regression: the precedent flow behaves exactly as before."},
    ]
    happy_only = [{"id": "AT-001", "requirement_ref": "REQ-001",
                   "statement": "GIVEN setup WHEN it runs THEN it succeeds."}]
    for cid in list(authored) + ["103"]:
        item = specs_root(tmp_path) / (f"wi{cid}-thing" if cid != "103" else "wi103-removed")
        (item / "context").mkdir(parents=True)
        (item / "context" / "manifest.json").write_text(json.dumps({"item_id": cid}), encoding="utf-8")
        (item / "spec.json").write_text(json.dumps({
            "requirements": [{"id": "REQ-001", "statement": f"The system shall do thing {cid}."}],
            "acceptance_tests": happy_only if cid == gap_item else full_tests(cid),
            "scope_boundaries": {"allowed_files": [f"src/Thing{cid}.al"]}}), encoding="utf-8")
        (item / "DESIGN.md").write_text(f"# design {cid}", encoding="utf-8")
        (item / "TASKS.md").write_text("- [ ] T-001 do it\n", encoding="utf-8")
    return d


def test_prepare_feature_review_blocks_on_unauthored_children(tmp_path):
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    _feature_with_children(tmp_path, authored=["101"])
    out = handle_prepare_feature_review(str(tmp_path), "feature-x")
    assert out["status"] == "blocked_items_incomplete"
    assert any("102" in gap for gap in out["items_incomplete"])
    assert out["items_covered"] == 1


def test_prepare_feature_review_builds_mega_packet(tmp_path):
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    d = _feature_with_children(tmp_path, authored=["101", "102"])
    out = handle_prepare_feature_review(str(tmp_path), "feature-x")
    assert out["status"] == "feature_review_ready"
    text = (d / "FEATURE-REVIEW.md").read_text(encoding="utf-8")
    assert "101 — Item 101" in text and "102 — Item 102" in text
    assert "The system shall do thing 101." in text
    assert "src/Thing102.al" in text
    assert "Test matrix" in text and "Execution contract" in text
    assert out["next_action"]["tool"] == "bc_request_approval"
    # every authored item plans all four buckets
    for row in out["test_matrix"]:
        assert row["happy"] and row["negative"] and row["edge"] and row["regression"]


def test_prepare_feature_review_excludes_removed_items(tmp_path):
    """ADO state 'Removed' wins over an existing spec folder — never planned/reviewed."""
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    d = _feature_with_children(tmp_path, authored=["101", "102"])
    out = handle_prepare_feature_review(str(tmp_path), "feature-x")
    text = (d / "FEATURE-REVIEW.md").read_text(encoding="utf-8")
    assert "Item 103" not in text and "wi103-removed" not in text
    assert all(r["id"] != "103" for r in out["test_matrix"])
    assert not any("103" in gap for gap in out["items_incomplete"] + out["test_gaps"])


def test_prepare_feature_review_blocks_on_test_gaps(tmp_path):
    """An item whose plan misses negative/edge/regression buckets blocks the ONE gate."""
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    _feature_with_children(tmp_path, authored=["101", "102"], gap_item="102")
    out = handle_prepare_feature_review(str(tmp_path), "feature-x")
    assert out["status"] == "blocked_items_incomplete"
    assert any("102" in g and "negative" in g for g in out["test_gaps"])
    assert any("regression" in g for g in out["test_gaps"])


def test_feature_plan_gate_demands_mega_review_once_authoring_started(tmp_path):
    from bc_agentic_mcp.tools.approval import handle_submit_decision
    _feature_with_children(tmp_path, authored=["101"])
    out = asyncio.run(handle_submit_decision(
        str(tmp_path), "feature-x", "plan", "approve"))
    assert out["status"] == "blocked"
    assert any("feature-wide review" in b for b in out["blockers"])


def test_feature_approval_cascades_to_all_children(tmp_path):
    from bc_agentic_mcp.tools.approval import handle_submit_decision
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    from bc_agentic_mcp import authorization
    _feature_with_children(tmp_path, authored=["101", "102"])
    assert handle_prepare_feature_review(str(tmp_path), "feature-x")["status"] == "feature_review_ready"
    out = asyncio.run(handle_submit_decision(
        str(tmp_path), "feature-x", "plan", "approve", feedback="one gate for all"))
    assert out["status"] == "approve"
    assert sorted(out["cascaded_plan_approvals"]) == ["wi101-thing", "wi102-thing"]
    for spec in ("wi101-thing", "wi102-thing"):
        assert authorization.read_decision(tmp_path, spec, "plan") == "approve"
        assert authorization.implementation_authorized(tmp_path, spec)

def test_write_spec_parses_explicit_test_plan_lines(golden_repo):
    """TEST <bucket>: lines in bullets become acceptance criteria with deterministic buckets."""
    from bc_agentic_mcp.tools.write_spec import handle_write_spec
    from bc_agentic_mcp import verification
    out = asyncio.run(handle_write_spec(
        str(golden_repo), "wi-testplan", idempotency_key="tp-1",
        human_bullets=(
            "Add field 220 SpaceEntryNo (Integer) to table 11024121 RealtyObjectFacilityFDN "
            "in src/RealtyObjectFacility.Table.al.\n"
            "TEST happy: GIVEN a facility WHEN SpaceEntryNo is set THEN the space is linked.\n"
            "TEST negative: WHEN the space belongs to another realty object THEN validation blocks it.\n"
            "TEST edge: WHEN the same space is set twice THEN the link is unchanged.\n"
            "TEST regression: existing facility flows behave exactly as before.\n")))
    spec = json.loads((specs_root(golden_repo) / "wi-testplan" / "spec.json").read_text(encoding="utf-8"))
    tests = spec["acceptance_tests"]
    shapes = [str(t.get("path_shape", "")).lower() for t in tests]
    assert shapes.count("regression") == 1, tests
    for needed in ("happy", "negative", "edge"):
        assert needed in shapes, shapes
    # no duplicate parsing: each declared TEST line lands exactly once
    declared = [t for t in tests if t.get("path_shape")]
    assert len(declared) == 4, declared
    # every planned test is attached to a real requirement
    req_ids = {r["id"] for r in spec["requirements"]}
    assert all(t["requirement_ref"] in req_ids for t in tests)

def test_alias_line_renames_ticket_objects(golden_repo):
    """ALIAS: <ticket-name> = <real-name> corrects wrong ticket names before resolution."""
    from bc_agentic_mcp.tools.write_spec import handle_write_spec
    out = asyncio.run(handle_write_spec(
        str(golden_repo), "wi-alias", idempotency_key="al-1",
        human_bullets=(
            "MODIFY page WrongTicketNameFDN: add a column.\n"
            "ALIAS: WrongTicketNameFDN = FacilitiesOfRealtyObjectFDN\n")))
    spec = json.loads((specs_root(golden_repo) / "wi-alias" / "spec.json").read_text(encoding="utf-8"))
    names_created = [o.get("name") for o in spec.get("objects_to_create", [])]
    assert "WrongTicketNameFDN" not in names_created
    assert any(o.get("name") == "FacilitiesOfRealtyObjectFDN"
               for o in spec.get("objects_to_modify", []))


def test_unresolved_modify_never_becomes_phantom_create(golden_repo):
    from bc_agentic_mcp.tools.write_spec import handle_write_spec
    out = asyncio.run(handle_write_spec(
        str(golden_repo), "wi-phantom", idempotency_key="ph-1",
        human_bullets="MODIFY page TotallyUnknownPageFDN: change something.\n"))
    # blocked as needs_grounding (open question) OR grounded without the phantom -
    # but NEVER a create without target for the unknown page.
    spec_path = specs_root(golden_repo) / "wi-phantom" / "spec.json"
    if spec_path.exists():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        assert not any(o.get("name") == "TotallyUnknownPageFDN" and not o.get("target")
                       for o in spec.get("objects_to_create", []))
    else:
        assert out.get("status") in ("needs_grounding", "blocked_invalid_generated_spec", "grounded")

def test_design_mermaid_has_edges_and_clean_labels(golden_repo):
    """The dependency graph is a SCHEMA: quoted name labels (no raw paths), item->object
    edges, and dotted test->production edges (human feedback 2026-07-04)."""
    from bc_agentic_mcp.tools.write_spec import handle_write_spec
    from bc_agentic_mcp.tools.plan_design import handle_plan_design
    asyncio.run(handle_write_spec(
        str(golden_repo), "wi-schema", idempotency_key="sch-1",
        human_bullets=(
            "Add field 220 SpaceEntryNo (Integer) to table 11024121 RealtyObjectFacilityFDN "
            "in src/RealtyObjectFacility.Table.al.\n"
            "- CREATE codeunit SchemaProbeFDNT (target: tests/SchemaProbe.Codeunit.al): test codeunit.\n"
            "TEST happy: works.\nTEST negative: fails with an error.\n"
            "TEST edge: twice is unchanged.\nTEST regression: old flows unchanged.\n")))
    asyncio.run(handle_plan_design(str(golden_repo), "wi-schema"))
    text = (specs_root(golden_repo) / "wi-schema" / "DESIGN.md").read_text(encoding="utf-8")
    graph = text.split("```mermaid")[1].split("```")[0]
    assert "-->|modifies|" in graph or "-->|creates|" in graph
    assert "-.->|tests|" in graph
    assert "\\" not in graph  # labels are names, never raw backslash paths
    assert 'item(["wi-schema"])' in graph


def test_feature_review_embeds_feature_schema(tmp_path):
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    d = _feature_with_children(tmp_path, authored=["101", "102"])
    plan = json.loads((d / "feature_plan.json").read_text(encoding="utf-8"))
    plan["analysis"] = {"suggested_order": [{"id": "101"}, {"id": "102"}],
                        "mention_edges": [{"from": "102", "to": "101"}],
                        "shared_objects": {"SharedTableFDN": ["101", "102"]},
                        "collision_warnings": [], "items": [], "excluded": []}
    (d / "feature_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    out = handle_prepare_feature_review(str(tmp_path), "feature-x")
    text = (d / "FEATURE-REVIEW.md").read_text(encoding="utf-8")
    graph = text.split("```mermaid")[1].split("```")[0]
    assert "i101 ==>|then| i102" in graph
    assert "i102 -.->|mentions| i101" in graph
    assert 'SharedTableFDN' in graph and "---|touches|" in graph

def test_feature_review_decisions_section(tmp_path):
    """The packet explains decisions in plain language: caller narrative + the
    machine-recorded checkpoint log + refinement corrections with examples."""
    from bc_agentic_mcp.tools.feature import handle_prepare_feature_review
    from bc_agentic_mcp import checkpoints
    d = _feature_with_children(tmp_path, authored=["101", "102"])
    checkpoints.append_checkpoint(tmp_path, "feature-x", kind="decision",
                                  summary="One gate for the whole feature - fewer interruptions.")
    item = specs_root(tmp_path) / "wi101-thing"
    item.joinpath("item_refinement.json").write_text(json.dumps({
        "findings": {"mismatches": [
            "#101: cites 'NoOfAddresses' as id 14 but source has id 15 - WRONG ID in PBI"]}}),
        encoding="utf-8")
    out = handle_prepare_feature_review(
        str(tmp_path), "feature-x",
        decisions="We build the flag first because everything else hides behind it.")
    text = (d / "FEATURE-REVIEW.md").read_text(encoding="utf-8")
    assert "## Decisions, in plain language" in text
    assert "We build the flag first" in text
    assert "One gate for the whole feature" in text
    assert "WRONG ID in PBI" in text and "the SPEC follows the code" in text

def test_plain_language_decisions_all_sources(golden_repo):
    """The shared renderer (used by RATIONALE.md section 8 + feature packet) tells the
    story from all three sources: checkpoints, refinement corrections, root cause."""
    from bc_agentic_mcp import checkpoints
    sdir = specs_root(golden_repo) / "wi-live"
    sdir.mkdir(parents=True, exist_ok=True)
    checkpoints.append_checkpoint(golden_repo, "wi-live", kind="decision",
                                  summary="Option A chosen: item text over precedent.")
    sdir.joinpath("item_refinement.json").write_text(json.dumps({
        "findings": {"mismatches": ["#1: cites id 14 but source has id 15 - WRONG ID"]}}),
        encoding="utf-8")
    sdir.joinpath("root_cause.json").write_text(json.dumps({
        "symptom": "the list showed stale counts",
        "root_cause": "OnValidate never propagated",
        "fix": "propagate on space change"}), encoding="utf-8")
    text = checkpoints.plain_language_decisions(golden_repo, "wi-live",
                                                narrative="We chose the flag first.")
    assert "We chose the flag first." in text
    assert "Option A chosen" in text
    assert "WRONG ID" in text and "the spec follows the code" in text
    assert "What users saw: the list showed stale counts" in text
    assert "Therefore the fix: propagate on space change" in text


def test_refinement_md_plain_language_section(tmp_path):
    from bc_agentic_mcp import feature_refine
    findings = {"counts": {"verified": 1, "mismatches": 1, "redundancies": 1,
                           "conflicts": 0, "guideline_flags": 0},
                "mismatches": ["#9: cites field id 14 but source has 15"],
                "redundancies": ["#9: field X already exists"],
                "conflicts": [], "guideline_flags": [], "empiric_required": [],
                "verified": ["table T ok"]}
    md = feature_refine.render_refinement_md({"id": 9, "title": "F"}, findings, "judged")
    assert "What this means, in plain language" in md
    assert "NOT true in the actual code" in md and "cites field id 14" in md
    assert "already exist" in md

def test_upgrade_tag_derived_from_repo_emp_tag(golden_repo):
    """The repo's real SAN upgrade tag (EMP_<wi>_<name>) named anywhere in the bullets
    wins over the generated placeholder (observed live: user's finished 239597 blocked
    because the gate grepped for 'facility-code-filter_upgrade_v1')."""
    from bc_agentic_mcp.tools.write_spec import handle_write_spec
    out = asyncio.run(handle_write_spec(
        str(golden_repo), "wi-tag", idempotency_key="tag-1",
        human_bullets=(
            "- Create upgrade codeunit ProbeUpgradeFDN (target: src/_Upgrade/ProbeUpgrade.Codeunit.al): "
            "populate the field. data target table: RealtyObjectFacilityFDN\n"
            "- MODIFY enumextension UpgradeTagsPerDatabaseHSG (target: src/_Upgrade/Tags.EnumExt.al): "
            "register tag value 123 EMP_999999_ProbeUpgradeFDN.\n"
            "TEST happy: works.\nTEST negative: fails with an error.\n"
            "TEST edge: twice unchanged.\nTEST regression: unchanged flows.\n")))
    spec = json.loads((specs_root(golden_repo) / "wi-tag" / "spec.json").read_text(encoding="utf-8"))
    tags = [c.get("idempotency_tag") for c in spec.get("upgrade_contracts", [])]
    assert tags == ["EMP_999999_ProbeUpgradeFDN"], tags

def test_permissionset_quoted_name_binds_squashed_filename(golden_repo):
    """permissionset "2C-ALG-PAGINA ALLEN" with an explicit target binds the squashed
    filename and lands as a scoped MODIFY (observed live: scope_violation on wi239944)."""
    from bc_agentic_mcp import work_extraction
    objs = work_extraction.extract_objects(
        'MODIFY permissionset "2C-ALG-PAGINA ALLEN" '
        '(target: extensions/BaseApp/src/_Permissions/ERPGeneral/2calgpaginaallen.permissionset.al): '
        'add Execute (X) on page FacilitiesWithinSpaceFDN.')
    perm = next((o for o in objs if o["kind"] == "permissionset"), None)
    assert perm is not None, objs
    assert perm["name"] == "2C-ALG-PAGINA ALLEN"
    assert perm.get("path", "").endswith("2calgpaginaallen.permissionset.al"), perm
