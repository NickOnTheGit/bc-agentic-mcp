"""Tests for the verification / coverage-proof feature."""
import pytest

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import security, verification
from bc_agentic_mcp.tools.verify import handle_verify, handle_record_test


def _charter(tmp_path):
    memory.write_charter(
        tmp_path,
        "wi-x",
        purpose="Read and update on-hold fields",
        operations={"read": True, "update": True},
        acceptance_criteria=[
            "The API exposes the on-hold fields (read).",
            "A GET returns current values.",
            "A PATCH persists the values.",
        ],
    )


def test_verification_not_fully_validated_without_tests(tmp_path):
    _charter(tmp_path)
    digest = verification.build_verification(tmp_path, "wi-x")
    assert digest["criteria_count"] == 3
    assert digest["validated_count"] == 0
    assert digest["fully_validated"] is False
    assert len(digest["uncovered"]) == 3


def test_verification_full_coverage_when_every_criterion_has_a_passing_test(tmp_path):
    _charter(tmp_path)
    verification.record_test(tmp_path, "wi-x", name="GET exposes fields", result="pass", covers=[1, 2], layer="runtime")
    verification.record_test(tmp_path, "wi-x", name="PATCH persists", result="pass", covers=[3], layer="runtime")
    digest = verification.build_verification(tmp_path, "wi-x")
    assert digest["validated_count"] == 3
    assert digest["fully_validated"] is True
    assert digest["uncovered"] == []


def test_record_test_persists_failure_detail(tmp_path):
    """WHY a run failed travels WITH the evidence — 'passed=6/7' alone forced a
    blind re-run just to learn the assert message (observed live on 66188)."""
    _charter(tmp_path)
    verification.record_test(
        tmp_path, "wi-x", name="AL run 6/7", result="fail", covers="all",
        layer="al-unit", evidence="container=acctest passed=6/7",
        failures=[{"test": "WhenX_ExpectY", "error": "Assert.IsTrue failed. " + "x" * 500}],
    )
    tests = [c for c in memory.load_checkpoints(tmp_path, "wi-x") if c.get("kind") == "test"]
    fl = (tests[-1]["details"] or {}).get("failures")
    assert fl and fl[0]["test"] == "WhenX_ExpectY"
    assert len(fl[0]["error"]) <= 400  # capped, never a log dump


def test_verification_reports_validation_classes_and_non_api_does_not_require_api(tmp_path):
    memory.write_charter(
        tmp_path,
        "wi-x",
        purpose="Update Vera table data",
        operations={"update": True},
        acceptance_criteria=["The table data is updated."],
    )
    # Minimal code-context grounding so heuristic can pass.
    ctx = tmp_path / ".specs" / "wi-x" / "context" / "code"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "code_context.json").write_text("{}", encoding="utf-8")
    verification.record_test(
        tmp_path,
        "wi-x",
        name="item test",
        result="pass",
        covers="all",
        layer="al-unit",
        evidence="container=acctest passed=7/7 exit=0 mode=item",
    )
    verification.record_test(
        tmp_path,
        "wi-x",
        name="regression test",
        result="pass",
        covers="all",
        layer="al-regression",
        evidence="container=acctest passed=3/3 exit=0 mode=regression",
    )
    digest = verification.build_verification(tmp_path, "wi-x")
    classes = digest["validation_classes"]
    assert classes["heuristic"]["required"] is True
    assert classes["empiric-item"]["required"] is True
    assert classes["regression"]["required"] is True
    assert classes["api-contract"]["required"] is False


def test_verification_failing_test_does_not_count_as_covered(tmp_path):
    _charter(tmp_path)
    verification.record_test(tmp_path, "wi-x", name="PATCH persists", result="fail", covers=[3])
    digest = verification.build_verification(tmp_path, "wi-x")
    row = next(r for r in digest["rows"] if r["index"] == 3)
    assert row["validated"] is False
    assert "A PATCH persists the values." in digest["uncovered"]


def test_verification_covers_all_marks_every_criterion(tmp_path):
    _charter(tmp_path)
    verification.record_test(tmp_path, "wi-x", name="full regression", result="pass", covers="all")
    digest = verification.build_verification(tmp_path, "wi-x")
    assert digest["fully_validated"] is True


@pytest.mark.asyncio
async def test_verify_tool_writes_report_and_returns_digest(tmp_path):
    _charter(tmp_path)
    evidence = "server-issued unit run passed=1/1 exit=0"
    receipt = security.issue_evidence(
        project_root=tmp_path,
        spec_name="wi-x",
        producer="bc_run_tests",
        name="unit tests",
        result="pass",
        covers="all",
        layer="al-unit",
        evidence=evidence,
    )
    await handle_record_test(
        project_root=str(tmp_path), spec_name="wi-x", name="unit tests", result="pass",
        covers="all", layer="al-unit", evidence=evidence, evidence_receipt=receipt,
    )
    digest = await handle_verify(project_root=str(tmp_path), spec_name="wi-x")
    assert digest["fully_validated"] is True
    from pathlib import Path
    report = Path(digest["report_path"]).read_text(encoding="utf-8")
    assert "Verification Report" in report
    assert "fully validated: YES" in report
