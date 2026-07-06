"""Enforcement tests: the verification gate is actually consulted at the approval chokepoint."""
import json
import pytest

from bc_agentic_mcp import verification
from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import timeline
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision


def _art(tmp_path):
    """Real artifact file: the presentation wall refuses non-existent artifact paths
    (approval on air is void — user catch 2026-07-04, wi267598)."""
    p = tmp_path / "art.md"
    if not p.exists():
        p.write_text("# review artifact\ncontent for the human\n", encoding="utf-8")
    return str(p)


def _charter(root, spec="s"):
    memory.write_charter(root, spec, purpose="p", operations={"update": True},
                         acceptance_criteria=["c1", "c2"])
    ctx = root / ".specs" / spec / "context" / "code"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "code_context.json").write_text("{}", encoding="utf-8")


def _record_regression(root, spec="s"):
    verification.record_test(
        root,
        spec,
        name="regression",
        result="pass",
        covers="all",
        layer="al-regression",
        evidence="container=acctest passed=3/3 exit=0 mode=regression paths=happy:1,negative:1,edge:1",
    )


def _record_test_lifecycle(root, spec="s"):
    timeline.record_phase(root, spec, "tests_generated")
    timeline.record_phase(root, spec, "tests_run")


def _api_row(name: str, validates: str = "generic check") -> dict:
    return {
        "name": name,
        "scenarioDescription": f"Scenario {name}",
        "validates": validates,
        "method": "PATCH",
        "endpoint": "http://localhost/api/companies(x)/rentalMutations(y)",
        "body": "{}",
        "expected": "2xx",
        "actual": "200",
        "statusCode": 200,
        "passed": True,
        "responseMessage": "success",
        "responseBody": "{}",
    }


# --- gate() logic ---
def test_gate_blocks_when_uncovered(tmp_path):
    _charter(tmp_path)
    verification.record_test(tmp_path, "s", name="t", result="pass", covers=[1],
                             layer="empiric-runtime", evidence="run")
    g = verification.gate(tmp_path, "s")
    assert g["passed"] is False
    assert any("Uncovered" in b for b in g["blockers"])


def test_gate_blocks_on_weak_evidence(tmp_path):
    _charter(tmp_path)
    for i in (1, 2):
        verification.record_test(tmp_path, "s", name=f"t{i}", result="pass", covers=[i],
                                 layer="empiric-runtime", evidence="")  # no evidence
    g = verification.gate(tmp_path, "s")
    assert g["passed"] is False
    assert any("Weak evidence" in b for b in g["blockers"])


def test_gate_passes_with_full_runtime_evidence(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    verification.record_test(tmp_path, "s", name="t", result="pass", covers="all",
                             layer="empiric-runtime", evidence="container=acctest passed=20/20 exit=0 mode=item paths=happy:1,negative:1,edge:1")
    _record_regression(tmp_path)
    g = verification.gate(tmp_path, "s")
    assert g["passed"] is True and g["blockers"] == []


# --- enforcement at the approval chokepoint ---
@pytest.mark.asyncio
async def test_approval_blocked_without_evidence(tmp_path):
    _charter(tmp_path)
    await handle_request_approval(str(tmp_path), "s", "implement", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "implement", "approve")
    assert res["status"] == "blocked"
    assert res["blockers"]
    assert "missing validation classes" in res["message"].lower()
    assert "empiric-item" in res["missing_validation_classes"]
    # The approval file must NOT have been flipped to approved.
    approval = (tmp_path / ".specs" / "s" / "approvals" / "implement.md").read_text()
    assert "**Status:** pending" in approval


@pytest.mark.asyncio
async def test_approval_allowed_with_evidence(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    verification.record_test(tmp_path, "s", name="t", result="pass", covers="all",
                             layer="empiric-runtime", evidence="container=acctest passed=2/2 exit=0 mode=item paths=happy:1,negative:1,edge:1")
    _record_regression(tmp_path)
    await handle_request_approval(str(tmp_path), "s", "implement", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "implement", "approve")
    assert res["status"] == "approve"
    assert res["audit_entry"]["gate_passed"] is True


@pytest.mark.asyncio
async def test_approval_override_is_loud_and_audited(tmp_path):
    _charter(tmp_path)  # no tests -> gate fails
    await handle_request_approval(str(tmp_path), "s", "implement", _art(tmp_path), "sum", "idem-1")
    # An agent-only override (no confirm_human) must be refused outright.
    refused = await handle_submit_decision(str(tmp_path), "s", "implement", "approve",
                                           override_reason="hotfix; runtime env unavailable")
    assert refused["status"] == "blocked_override_needs_human"
    # The HUMAN-confirmed override goes through, loudly audited.
    res = await handle_submit_decision(str(tmp_path), "s", "implement", "approve",
                                       override_reason="hotfix; runtime env unavailable",
                                       confirm_human=True)
    assert res["status"] == "approve"
    assert res["evidence_override"] is True
    assert res["audit_entry"]["override_reason"].startswith("hotfix")
    assert res["audit_entry"]["overridden_blockers"]


@pytest.mark.asyncio
async def test_non_verified_phase_is_not_gated(tmp_path):
    _charter(tmp_path)  # no tests
    await handle_request_approval(str(tmp_path), "s", "spec", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "spec", "approve")
    assert res["status"] == "approve"  # spec phase does not require the evidence gate


@pytest.mark.asyncio
async def test_complete_phase_blocked_without_local_container_evidence(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence="live run passed",  # no local-container marker
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "blocked"
    assert "empiric-item" in res["missing_validation_classes"]


@pytest.mark.asyncio
async def test_complete_phase_allowed_with_local_container_evidence(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    artifact = tmp_path / "api-test-evidence.json"
    artifact.write_text(json.dumps({"scenarios": [_api_row("s1", "rule")]}, ensure_ascii=True), encoding="utf-8")
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence=(
            "acctest container; endpoint http://172.26.136.111:7048/BC/api/zig/contract/v2.0; "
            f"artifact {artifact}"
        ),
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "approve"
    assert res["audit_entry"]["local_testing"]["ok"] is True


@pytest.mark.asyncio
async def test_complete_phase_blocked_when_local_evidence_has_no_full_refs(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence="acctest container api patch passed",  # no endpoint URL or artifact path
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "blocked"
    assert "empiric-item" in res["missing_validation_classes"]


@pytest.mark.asyncio
async def test_complete_phase_non_api_item_not_blocked_when_api_scenario_docs_are_incomplete(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    artifact = tmp_path / "api-test-evidence.json"
    artifact.write_text('{"scenarios":[{"name":"s1"}]}', encoding="utf-8")
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence=(
            "acctest container; endpoint http://172.26.136.111:7048/BC/api/zig/contract/v2.0; "
            f"artifact {artifact}"
        ),
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "approve"


@pytest.mark.asyncio
async def test_complete_phase_blocked_when_api_evidence_schema_missing_body_field(tmp_path):
    _charter(tmp_path)
    artifact = tmp_path / "api-test-evidence.json"
    row = _api_row("s1", "rule")
    row.pop("body")
    artifact.write_text(json.dumps({"scenarios": [row]}, ensure_ascii=True), encoding="utf-8")
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence=(
            "acctest container; endpoint http://172.26.136.111:7048/BC/api/zig/contract/v2.0; "
            f"artifact {artifact}"
        ),
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "blocked"


@pytest.mark.asyncio
async def test_complete_phase_allowed_with_non_api_execution_proof(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    verification.record_test(
        tmp_path,
        "s",
        name="al-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence="acctest container; AL run passed=20/20 exit=0 paths=happy:1,negative:1,edge:1",
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "approve"
    assert res["audit_entry"]["local_testing"]["ok"] is True


@pytest.mark.asyncio
async def test_complete_phase_non_api_item_not_blocked_when_exhaustive_api_taxonomy_is_incomplete(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    artifact = tmp_path / "api-test-evidence-exhaustive.json"
    artifact.write_text(
        json.dumps(
            {"scenarios": [_api_row("boundary_today_leaving", "Boundary date validation")]},
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence=(
            "acctest container; exhaustive api run; endpoint http://172.26.136.111:7048/BC/api/zig/contract/v2.0; "
            f"artifact {artifact}"
        ),
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "approve"


@pytest.mark.asyncio
async def test_complete_phase_allowed_with_exhaustive_api_taxonomy_complete(tmp_path):
    _charter(tmp_path)
    _record_test_lifecycle(tmp_path)
    _record_regression(tmp_path)
    artifact = tmp_path / "api-test-evidence-exhaustive.json"
    artifact.write_text(
        json.dumps(
            {
                "scenarios": [
                    _api_row("boundary_today_leaving", "Boundary date validation"),
                    _api_row("invalid_json_payload", "Malformed payload gives badrequest"),
                    _api_row("unauthorized_request", "Unauthorized auth check"),
                    _api_row("stale_if_match", "ETag precondition conflict"),
                    _api_row("invalid_company_id_not_found", "Resource not found for invalid id"),
                ]
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    verification.record_test(
        tmp_path,
        "s",
        name="api-runtime-local",
        result="pass",
        covers="all",
        layer="empiric-runtime",
        evidence=(
            "acctest container; exhaustive api run; endpoint http://172.26.136.111:7048/BC/api/zig/contract/v2.0; "
            f"artifact {artifact}"
        ),
    )
    await handle_request_approval(str(tmp_path), "s", "complete", _art(tmp_path), "sum", "idem-1")
    res = await handle_submit_decision(str(tmp_path), "s", "complete", "approve")
    assert res["status"] == "approve"
    assert res["audit_entry"]["local_testing"]["ok"] is True

