"""Tests for the new deterministic capabilities (evidence, runner, api, schema, consumers, lessons)."""
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import verification, al_runner, api_contract, schema, consumers, lessons
from bc_agentic_mcp import checkpoints as memory


# --- #1 evidence-enforced verification ---
def test_evidence_strength_downgrades_empiric_without_evidence():
    assert verification.evidence_strength("empiric-runtime", "log.txt") == 4
    assert verification.evidence_strength("empiric-runtime", "") == 1  # no evidence -> claim
    assert verification.evidence_strength("heuristic", "") == 1


def test_required_strength_for_operations():
    assert verification.required_strength_for_operations({"update": True}) == 4
    assert verification.required_strength_for_operations({"read": True}) == 3
    assert verification.required_strength_for_operations({}) == 1


def test_build_verification_flags_weak_evidence(tmp_path):
    root = tmp_path
    memory.write_charter(root, "s", purpose="p", operations={"update": True},
                         acceptance_criteria=["c1"])
    # A passing test with no evidence on a mutation item -> covered but below the bar.
    verification.record_test(root, "s", name="t1", result="pass", covers=[1],
                             layer="empiric-runtime", evidence="")
    d = verification.build_verification(root, "s")
    assert d["fully_validated"] is True          # coverage met
    assert d["required_strength"] == 4
    assert d["fully_validated_strict"] is False   # evidence bar not met
    assert d["evidence_gaps"] == ["c1"]

    verification.record_test(root, "s", name="t2", result="pass", covers=[1],
                             layer="empiric-runtime", evidence="container run 20/20 exit=0")
    d2 = verification.build_verification(root, "s")
    assert d2["fully_validated_strict"] is True


# --- #2 container runner (pure command + parser) ---
def test_build_run_tests_command_has_no_secret_literal():
    cmd = al_runner.build_run_tests_command(container_name="c1", test_extension_id="abc",
                                            credential_env="MY_PW", user="devadmin")
    joined = " ".join(cmd)
    assert "c1" in joined and "abc" in joined
    assert "MY_PW" in joined            # referenced by env name
    assert "BC_LICENSE_FILE" in joined  # optional explicit override
    assert "license.bclicense" in joined
    assert "LICENSE_RECOVERY" in joined
    assert "Welkom" not in joined       # no secret value


def test_parse_test_results():
    out = """
LICENSE_RECOVERY: imported C:\\ProgramData\\BcContainerHelper\\Extensions\\acctest\\my\\license.bclicense
  Codeunit 50000 SubprocOnHoldRunTest Success (0.1 seconds)
    Testfunction AllFieldsPersist Success (0.02 seconds)
    Testfunction PastDateRejected Failure (0.01 seconds)
      Error:
        Assert.AreEqual failed. Expected:<0> Actual:<1>.
      Call Stack:
        PastDateRejected line 12
ALL_TESTS_PASSED: False
"""
    r = al_runner.parse_test_results(out)
    assert r["total"] == 2 and r["passed"] == 1 and r["failed"] == 1
    assert r["all_passed"] is False
    assert "imported" in (r["license_recovery"] or "")
    assert r["codeunits"][0]["id"] == 50000
    # -detailed failure lines are attached so a failure is diagnosable without a re-run
    assert r["failures"][0]["test"] == "PastDateRejected"
    assert "Assert.AreEqual failed" in r["failures"][0]["error"]


def test_run_container_tests_with_injected_runner():
    class FakeProc:
        stdout = "  Codeunit 1 X Success\n    Testfunction A Success\nALL_TESTS_PASSED: True"
        returncode = 0
    r = al_runner.run_container_tests(container_name="c", test_extension_id="e",
                                      runner=lambda cmd: FakeProc())
    assert r["executed"] and r["all_passed"] and r["passed"] == 1


# --- #3 api contract (plan + classify + injected fetcher) ---
def test_build_contract_plan_derives_from_fields():
    fields = [{"name": "OnHoldTill", "al_type": "Date"}, {"name": "Remark", "al_type": "Text[250]"}]
    plan = api_contract.build_contract_plan(fields, {"update": True}, entity="rentalMutations")
    ids = " ".join(c["id"] for c in plan)
    assert "read" in ids
    assert any(c["expect"] == "reject" for c in plan)      # negatives exist
    assert any(c["method"] == "POST" for c in plan)        # insert blocked (update-only)
    assert any("date:" in c["id"] for c in plan)           # date edge cases present


def test_classify_response():
    assert api_contract.classify_response("accept", 200)["passed"] is True
    assert api_contract.classify_response("accept", 400)["passed"] is False
    assert api_contract.classify_response("reject", 400)["passed"] is True
    assert api_contract.classify_response("reject", 200)["passed"] is False
    # 405 MethodNotAllowed IS a refusal: read-only API pages reject writes this way
    assert api_contract.classify_response("reject", 405)["passed"] is True


def test_non_editable_fields_expect_writes_rejected():
    """Editable=false flips the boundary write to 'reject' — expecting the write
    to succeed contradicts the spec (observed live on FacilityCodeFilter)."""
    fields = [{"name": "FacilityCodeFilter", "al_type": "Code[250]", "editable": False}]
    plan = api_contract.build_contract_plan(fields, {"read": True}, entity="veraSpaceDetailTypes")
    boundary = [c for c in plan if c["id"].startswith("bnd:")]
    assert boundary and all(c["expect"] == "reject" for c in boundary)
    # editable fields keep the accept expectation
    plan2 = api_contract.build_contract_plan(
        [{"name": "Remark", "al_type": "Text[250]"}], {"update": True}, entity="e")
    boundary2 = [c for c in plan2 if c["id"].startswith("bnd:")]
    assert boundary2 and all(c["expect"] == "accept" for c in boundary2)


def test_run_contract_with_injected_fetcher():
    fields = [{"name": "OnHoldTill", "al_type": "Date"}]

    def fetcher(method, url, headers, body):
        # GET -> 200; every PATCH negative -> 400; boundary/accept -> 200
        return (200, "{}") if method == "GET" else (400, "{}")

    # All negatives should be rejected (400) and pass; the boundary 'accept' PATCH gets 400 -> fails.
    r = api_contract.run_contract(base_url="http://h/api", entity="e", fields=fields,
                                  operations={"update": True}, user="u", fetcher=fetcher)
    assert r["executed"] and r["total"] > 0


# --- #4/#5 schema reconcile + upgrade preflight ---
def test_parse_odata_metadata_and_reconcile():
    xml = (
        '<edmx><EntityType Name="rentalMutation">'
        '<Property Name="entryNo"/><Property Name="onHoldTill"/></EntityType></edmx>'
    )
    parsed = schema.parse_odata_metadata(xml)
    assert parsed["rentalMutation"] == {"entryNo", "onHoldTill"}
    rec = schema.reconcile_fields(["onHoldTill", "brandNew"], ["entryNo", "onHoldTill"])
    assert rec["existing"] == ["onHoldTill"] and rec["new"] == ["brandNew"]
    assert rec["all_requested_exist"] is False


def test_reconcile_against_endpoint_injected_fetcher():
    xml = '<edmx><EntityType Name="e"><Property Name="a"/></EntityType></edmx>'
    r = schema.reconcile_against_endpoint(requested=["a"], metadata_url="http://x/$metadata",
                                          entity="e", fetcher=lambda u: xml)
    assert r["all_requested_exist"] is True and r["deployed_field_count"] == 1


def test_diff_schema_flags_removals_as_breaking():
    d = schema.diff_schema(["a", "b"], ["a", "b", "c"])
    assert d["removed_fields"] == ["c"] and d["breaking"] is True
    d2 = schema.diff_schema(["a", "b", "c"], ["a", "b"])
    assert d2["breaking"] is False and d2["added_fields"] == ["c"]


# --- #7 consumer discovery ---
def test_find_consumers(tmp_path):
    (tmp_path / "T.Table.al").write_text(
        'table 1 MyTable\n{\n  fields { field(1; MyField; Integer) { } }\n}', encoding="utf-8")
    (tmp_path / "C.Codeunit.al").write_text(
        'codeunit 2 MyCod\n{\n  procedure P() begin Rec.MyField := 1; end;\n}', encoding="utf-8")
    r = consumers.find_consumers(str(tmp_path), "MyField")
    assert r["consumer_count"] == 1
    assert r["consumers"][0]["objects"] == ["codeunit"]
    assert r["has_derived_logic"] is True
    assert len(r["definition_sites"]) == 1  # the table is the definition, not a consumer


# --- #6 shared/semantic lessons ---
def test_overlap_score_deterministic():
    s = lessons.overlap_score("rental mutation on hold date", "on hold date expiry rule")
    assert 0.0 < s <= 1.0
    assert lessons.overlap_score("abc", "xyz") == 0.0


def test_global_lessons_and_promote(tmp_path, monkeypatch):
    gpath = tmp_path / "global.json"
    monkeypatch.setenv("BC_MCP_GLOBAL_LESSONS", str(gpath))
    root = tmp_path / "proj"
    # A project lesson, then promote to global.
    proj_lesson = lessons.record_human_lesson(root, message="Always test date edge cases at the API boundary",
                                              match={"keyword": "date"})
    promoted = lessons.promote_lesson(root, proj_lesson["id"])
    assert promoted is not None and gpath.exists()
    # Global recall by semantic overlap from a DIFFERENT project with no local lessons.
    other = tmp_path / "other"
    hits = lessons.applicable_lessons(other, api="", keywords_text="date edge cases api boundary")
    assert any("date edge cases" in h["message"] for h in hits)
