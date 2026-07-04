"""Tests for the research→try→fail→retry→succeed→learn loop (attempts.py),
the poka-yoke input validators (F1), and the schema-reality gate (D1)."""
import json

import pytest

from bc_agentic_mcp import attempts
from bc_agentic_mcp.validation import (
    validate_covers,
    validate_evidence_layer,
    validate_validation_mode,
)


# --- error classification is deterministic ---
def test_classify_error_taxonomy():
    assert attempts.classify_error("error AL1024: A package with publisher ...") == "dependency-symbol"
    assert attempts.classify_error("error AL0185: Table 'Post Code' is missing") == "missing-object"
    assert attempts.classify_error("No license file has been uploaded") == "license"
    assert attempts.classify_error("401 Unauthorized") == "auth"
    assert attempts.classify_error("Transaction was rolled back by the server") == "container"
    assert attempts.classify_error("something novel") == "other"


# --- fingerprints: stable, order-independent, secret-free ---
def test_param_fingerprint_stable_and_order_independent():
    a = attempts.param_fingerprint("bc_run_tests", {"container_name": "c1", "tenant": "default"})
    b = attempts.param_fingerprint("bc_run_tests", {"tenant": "default", "container_name": "c1"})
    assert a == b
    c = attempts.param_fingerprint("bc_run_tests", {"container_name": "c2", "tenant": "default"})
    assert a != c


def test_param_fingerprint_ignores_volatile_keys_and_hides_values():
    a = attempts.param_fingerprint("bc_implement", {"code": "secretcontent", "idempotency_key": "k1"})
    b = attempts.param_fingerprint("bc_implement", {"code": "secretcontent", "idempotency_key": "k2"})
    assert a == b  # re-keying is not a new approach
    assert "secretcontent" not in a


# --- TRY/FAIL/RETRY: identical approach refused after 2 failures ---
def test_doom_loop_guard_refuses_third_identical_attempt(tmp_path):
    kwargs = {"container_name": "c1", "test_extension_id": "e1"}
    check = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)
    assert check["allowed"] is True
    fp = check["fingerprint"]

    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp, "error AL1024: symbols missing")
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["allowed"] is True

    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp, "error AL1024: symbols missing")
    refused = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)
    assert refused["allowed"] is False
    assert "dependency-symbol" in refused["refusal"]["error_classes"]
    assert "Change the approach" in refused["refusal"]["reason"]


def test_changed_params_are_a_new_approach(tmp_path):
    kwargs1 = {"container_name": "c1", "scope": "Global"}
    fp1 = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs1)["fingerprint"]
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp1, "publish failed")
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp1, "publish failed")
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs1)["allowed"] is False
    # Different param value (e.g. dev-endpoint scope) => different fingerprint => allowed.
    kwargs2 = {"container_name": "c1", "scope": "Tenant"}
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs2)["allowed"] is True


# --- SUCCEED/LEARN: any success resets streaks; recovery is reported ---
def test_any_success_resets_streaks_and_reports_recovery(tmp_path):
    kwargs = {"container_name": "c1"}
    fp = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["fingerprint"]
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp, "error AL1024")
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp, "error AL1024")
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["allowed"] is False

    recovery = attempts.record_success(str(tmp_path), "s", "bc_run_tests", fp)
    assert recovery["recovered"] is True
    assert recovery["recovered_from"][0]["error_class"] == "dependency-symbol"
    # After the success, the identical call is allowed again (world changed).
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["allowed"] is True


def test_success_of_other_tool_clears_streak(tmp_path):
    kwargs = {"container_name": "c1"}
    fp = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["fingerprint"]
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp, "publish failed")
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp, "publish failed")
    # A DIFFERENT tool succeeds (e.g. bc_implement fixed the code) => world changed.
    other_fp = attempts.param_fingerprint("bc_implement", {"task": "t1"})
    attempts.record_success(str(tmp_path), "s", "bc_implement", other_fp)
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["allowed"] is True


def test_unguarded_tools_and_missing_spec_are_never_blocked(tmp_path):
    assert attempts.check_attempt(str(tmp_path), "s", "bc_status", {})["allowed"] is True
    assert attempts.check_attempt(None, None, "bc_run_tests", {})["allowed"] is True


def test_red_evidence_runs_get_a_longer_leash(tmp_path):
    """The fix-retest loop legitimately re-issues the IDENTICAL evidence run after
    out-of-ledger fixes (dependency published, sibling spec's file changed) —
    observed live three times on feature 239584 where the 2-strike guard forced
    param-noise workarounds. Infra failures keep the strict threshold."""
    kwargs = {"container_name": "c1", "test_codeunit": "68900"}
    fp = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["fingerprint"]
    for _ in range(4):
        attempts.record_failure(
            str(tmp_path), "s", "bc_run_tests", fp,
            "evidence run red: passed=6/7 exit=0")
        assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)["allowed"] is True
    attempts.record_failure(
        str(tmp_path), "s", "bc_run_tests", fp, "evidence run red: passed=6/7 exit=0")
    refused = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs)
    assert refused["allowed"] is False  # 5 red runs with NO progress is a real loop
    # A single infra-class failure in the streak keeps the strict threshold.
    kwargs2 = {"container_name": "c2", "test_codeunit": "68900"}
    fp2 = attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs2)["fingerprint"]
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp2, "evidence run red: passed=1/3 exit=1")
    attempts.record_failure(str(tmp_path), "s", "bc_run_tests", fp2, "publish failed")
    assert attempts.check_attempt(str(tmp_path), "s", "bc_run_tests", kwargs2)["allowed"] is False


# --- structured-result failures count as failures; gate blocks do NOT ---
def test_result_failure_signal_detects_failures_but_not_gate_blocks():
    # Gate blocks direct to a prerequisite; the SAME call must stay retryable
    # after the environment is fixed — so they are NOT failed approaches.
    assert attempts.result_failure_signal({"status": "blocked_needs_approval"}) is None
    assert attempts.result_failure_signal({"blocked": True, "reason": "gate"}) is None
    assert attempts.result_failure_signal({"status": "blocked_env_preflight", "blocked": True}) is None
    # Genuine failures ARE recorded.
    assert attempts.result_failure_signal(
        {"executed": True, "all_passed": False, "passed": 1, "total": 3, "exit_code": 1}
    ) is not None
    assert attempts.result_failure_signal({"isError": True}) is not None
    assert attempts.result_failure_signal({"executed": True, "all_passed": True}) is None
    assert attempts.result_failure_signal({"status": "scaffold_generated"}) is None


# --- F1 poka-yoke validators ---
def test_validate_validation_mode():
    assert validate_validation_mode("item") == "item"
    assert validate_validation_mode("REGRESSION") == "regression"
    assert validate_validation_mode("") == "item"  # default
    with pytest.raises(ValueError):
        validate_validation_mode("smoke")


def test_validate_evidence_layer():
    assert validate_evidence_layer("al-unit") == "al-unit"
    assert validate_evidence_layer("AL-Regression") == "al-regression"
    assert validate_evidence_layer("") == ""
    with pytest.raises(ValueError):
        validate_evidence_layer("unit-ish")


def test_validate_covers():
    validate_covers("all")
    validate_covers([1, 2, 3])
    for bad in ("some", [], [0], [1, "2"], None):
        with pytest.raises(ValueError):
            validate_covers(bad)


# --- D1 schema-reality gate in bc_generate_tests ---
@pytest.mark.asyncio
async def test_generate_tests_blocked_without_reconcile_for_api_fields(tmp_path):
    from bc_agentic_mcp.tools.generate_tests import handle_generate_tests
    specs_dir = tmp_path / ".specs" / "wi-x"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.json").write_text(json.dumps({
        "spec_name": "wi-x",
        "business_rules": [{"id": "BR-1", "description": "field persists"}],
        "objects": [{"type": "API Page", "object_name": "rentalMutation"}],
        "data_model": [{"name": "OnHoldTill", "al_type": "Date"}],
    }))
    result = await handle_generate_tests(str(tmp_path), "wi-x")
    assert result["status"] == "blocked_schema_reality"
    assert result["next_action"]["tool"] == "bc_reconcile_target"
    assert "OnHoldTill" in result["fields_needing_check"]


@pytest.mark.asyncio
async def test_generate_tests_blocked_when_field_missing_from_deployed_schema(tmp_path):
    from bc_agentic_mcp.tools.generate_tests import handle_generate_tests
    specs_dir = tmp_path / ".specs" / "wi-x"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.json").write_text(json.dumps({
        "spec_name": "wi-x",
        "business_rules": [{"id": "BR-1", "description": "field persists"}],
        "objects": [{"type": "API Page", "object_name": "rentalMutation"}],
        "data_model": [{"name": "FacilityCodeFilter", "al_type": "Code[250]"}],
    }))
    # Reconcile report says the field is NOT deployed ("new").
    (specs_dir / "reconcile_report.json").write_text(json.dumps({
        "existing": [], "new": ["FacilityCodeFilter"], "all_requested_exist": False,
    }))
    result = await handle_generate_tests(str(tmp_path), "wi-x")
    assert result["status"] == "blocked_schema_reality"
    assert "FacilityCodeFilter" in result["missing_fields"]


@pytest.mark.asyncio
async def test_generate_tests_allowed_with_reconciled_or_declared_new_fields(tmp_path):
    from bc_agentic_mcp.tools.generate_tests import handle_generate_tests
    specs_dir = tmp_path / ".specs" / "wi-x"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.json").write_text(json.dumps({
        "spec_name": "wi-x",
        "business_rules": [{"id": "BR-1", "description": "field persists"}],
        "objects": [{"type": "API Page", "object_name": "rentalMutation"}],
        "data_model": [
            {"name": "OnHoldTill", "al_type": "Date"},
            {"name": "BrandNewField", "al_type": "Text[50]", "to_be_created": True},
        ],
    }))
    (specs_dir / "reconcile_report.json").write_text(json.dumps({
        "existing": ["OnHoldTill"], "new": [], "all_requested_exist": True,
    }))
    result = await handle_generate_tests(str(tmp_path), "wi-x")
    assert result["status"] == "scaffold_generated"


@pytest.mark.asyncio
async def test_generate_tests_gate_not_applicable_for_non_api_items(tmp_path):
    from bc_agentic_mcp.tools.generate_tests import handle_generate_tests
    specs_dir = tmp_path / ".specs" / "wi-x"
    specs_dir.mkdir(parents=True)
    (specs_dir / "spec.json").write_text(json.dumps({
        "spec_name": "wi-x",
        "business_rules": [{"id": "BR-1", "description": "table data persists"}],
        "objects": [{"type": "Table", "object_name": "VeraSpaceDetailType"}],
        "data_model": [],
    }))
    result = await handle_generate_tests(str(tmp_path), "wi-x")
    assert result["status"] == "scaffold_generated"


# --- reconcile report persistence ---
@pytest.mark.asyncio
async def test_reconcile_target_persists_report_for_spec(tmp_path):
    from bc_agentic_mcp.tools.schema_tools import handle_reconcile_target
    result = await handle_reconcile_target(
        str(tmp_path), requested=["a", "b"], deployed=["a"], spec_name="wi-x",
    )
    assert result["existing"] == ["a"] and result["new"] == ["b"]
    report = json.loads((tmp_path / ".specs" / "wi-x" / "reconcile_report.json").read_text())
    assert report["new"] == ["b"]
    assert report["generated_at"]
