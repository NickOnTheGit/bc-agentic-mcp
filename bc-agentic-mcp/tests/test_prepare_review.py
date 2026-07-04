"""Tests for the single-entry review workflow."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from bc_agentic_mcp.tools.prepare_review import (
    handle_prepare_review,
    _run_spec_analysis,
    _source_freshness_findings,
)


def _git(cwd, *args) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env)


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_source_freshness_flags_behind_checkout(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    (work / "a.txt").write_text("1", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "c1")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    # Advance the remote via a second clone so `work` falls one commit behind.
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    (other / "b.txt").write_text("2", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "c2")
    _git(other, "push", "origin", "main")

    findings = _source_freshness_findings(work)
    assert any(f["code"] == "BC-STALE-SOURCE" for f in findings)
    assert any(f["severity"] == "error" for f in findings)


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
def test_source_freshness_silent_without_upstream(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    (work / "a.txt").write_text("1", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "c1")
    # No remote / upstream configured -> cannot determine staleness -> stay silent.
    assert _source_freshness_findings(work) == []


def test_source_freshness_silent_outside_git(tmp_path):
    # A plain directory with no git working tree must never block.
    assert _source_freshness_findings(tmp_path) == []


@pytest.mark.skipif(not shutil.which("git"), reason="git not available")
@pytest.mark.asyncio
async def test_stale_source_is_not_recorded_as_a_durable_lesson(tmp_path):
    from bc_agentic_mcp import lessons as lessons_store

    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _write_app_json(work, [{"from": 11015500, "to": 11015999}])
    (work / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
    (work / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
        _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
    )
    _git(work, "add", ".")
    _git(work, "commit", "-m", "c1")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    (other / "x.txt").write_text("1", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "c2")
    _git(other, "push", "origin", "main")

    result = await handle_prepare_review(
        project_root=str(work),
        spec_name="wi-stale",
        human_bullets=_BC_HUMAN,
        idempotency_key="k-stale",
    )
    failures = " ".join(result.get("quality_gate", {}).get("failures", []))
    assert "BC-STALE-SOURCE" in failures  # fresh finding present at the gate
    # ...but a point-in-time git-state finding must NOT become a replayable lesson.
    codes = {l["code"] for l in lessons_store.load_lessons(Path(work))}
    assert "BC-STALE-SOURCE" not in codes


def _write_rental_mutation_examples(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "RentalMutation.Table.al").write_text(
        """
table 11282361 RentalMutationHSG
{
    fields
    {
        field(100; LeavingTenantSubProcCompleted; Boolean) { }
        field(101; NewRentalSubProcCompleted; Boolean) { }
        field(102; AssignedUserId; Guid) { }
        field(103; AssignmentViaTeamCode; Code[20]) { }
    }
}
""".strip(),
        encoding="utf-8",
    )
    (src / "rentalMutation.Page.al").write_text(
        """
page 11015853 rentalMutationTAI
{
    layout
    {
        area(Content)
        {
            repeater(General)
            {
                field("leavingTenantSubProcCompleted"; Rec."LeavingTenantSubProcCompleted") { }
                field("newRentalSubProcCompleted"; Rec."NewRentalSubProcCompleted") { }
                field("assignedUserId"; Rec."AssignedUserId") { }
            }
        }
    }
}
""".strip(),
        encoding="utf-8",
    )


def _write_rental_mutation_completion_logic(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "RentalMutationPopulateData.CodeUnit.al").write_text(
        """
codeunit 123456 RentalMutationPopulateDataHSG
{
    procedure UpdateCompletion(var RentalMutation: Record RentalMutationHSG)
    begin
        SetLeavingTenantSubProcCompletedWhenMilestonesAreCompleted(RentalMutation);
        if not RentalMutation.LeavingTenantSubProcCompleted then
            exit;
        if not RentalMutation.NewRentalSubProcCompleted then
            exit;
    end;

    procedure SetLeavingTenantSubProcCompletedWhenMilestonesAreCompleted(var RentalMutation: Record RentalMutationHSG)
    begin
        RentalMutation.LeavingTenantSubProcCompleted := true;
        RentalMutation.NewRentalSubProcCompleted := true;
    end;
}
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_prepare_review_returns_questions_for_ambiguous_description():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_rental_mutation_examples(root)
        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="test-spec",
            human_bullets="Notify the user when the date changes and run in background",
            idempotency_key="key-1",
        )

        assert result["status"] == "needs_clarification"
        assert result["questions"]
        # Item-AGNOSTIC questions only: the old hardcoded
        # SubprocessLeavingTenantOnHoldTill question (another item's scaffolding) was
        # deleted after firing on unrelated items (observed live on wi267598).
        assert not any("SubprocessLeavingTenantOnHoldTill" in q["question"]
                       for q in result["questions"])
        assert result["code_examples"]
        assert Path(result["file_path"]).exists()
        content = Path(result["file_path"]).read_text(encoding="utf-8")
        assert "Existing Code Examples to Reuse" in content
        assert "LeavingTenantSubProcCompleted" in content


@pytest.mark.asyncio
async def test_prepare_review_blocks_description_without_acceptance_criteria():
    # Standard (enforced hook): a description that yields no measurable requirements/acceptance
    # criteria is NOT review-ready — it must be sent back for clarification, never approved empty.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_rental_mutation_examples(root)
        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="test-spec",
            human_bullets="Add on-hold fields to Rental Mutation and expose them on both API pages",
            idempotency_key="key-2",
        )

        assert result["status"] != "ready_for_review"


@pytest.mark.asyncio
async def test_prepare_review_skips_subprocess_completion_question_when_inferred():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_rental_mutation_examples(root)
        _write_rental_mutation_completion_logic(root)

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="test-spec",
            human_bullets=(
                "Define subprocess completed behavior and completion status for rental mutation\n"
                "TEST happy: GIVEN a subprocess WHEN it completes THEN the completion status is set.\n"
                "TEST negative: WHEN completion is requested for an invalid subprocess THEN an error is raised.\n"
                "TEST edge: WHEN completion runs twice THEN the status is unchanged (idempotent).\n"
            ),
            idempotency_key="key-3",
        )

        assert result["status"] == "ready_for_review"
        inferred = result["inferred_rules"]
        assert "subprocess_completion" in inferred
        assert inferred["subprocess_completion"]["rule"] == "subprocess_completion_derived_from_milestones"

        review_content = Path(result["review_path"]).read_text(encoding="utf-8")
        assert "subprocess_completion_derived_from_milestones" in review_content
        assert "Deterministic Enforcement" in review_content


@pytest.mark.asyncio
async def test_prepare_review_fails_closed_when_quality_gate_fails():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "OtherApi.Page.al").write_text(
            """
page 12345 OtherApiPage
{
    layout
    {
        area(Content)
        {
            repeater(General)
            {
                field("id"; Rec."SystemId") { }
            }
        }
    }
}
""".strip(),
            encoding="utf-8",
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="test-spec-gate",
            human_bullets="""Extend the following API:
rentalMutation

SubprocessLeavingTenantRemark
SubprocessNewRentalRemark
""",
            idempotency_key="key-4",
        )

        assert result["status"] == "needs_clarification"
        assert "quality_gate" in result
        assert result["quality_gate"]["pass"] is False
        assert Path(result["quality_gate_path"]).exists()
        assert Path(result["file_path"]).exists()
        content = Path(result["file_path"]).read_text(encoding="utf-8")
        assert "Deterministic Quality Gate Failures" in content


@pytest.mark.asyncio
async def test_prepare_review_prefers_api_targeted_examples():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "rentalMutation.Page.al").write_text(
            """
page 11015853 rentalMutationTAI
{
    layout
    {
        area(Content)
        {
            repeater(General)
            {
                field("subprocessLeavingTenantRemark"; Rec."SubprocessLeavingTenantRemark") { }
            }
        }
    }
}
""".strip(),
            encoding="utf-8",
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="test-spec-targeted",
            human_bullets="""Extend the following API:
rentalMutation

SubprocessLeavingTenantRemark
""",
            idempotency_key="key-5",
        )

        assert result["status"] == "ready_for_review"
        assert result["code_examples"]
        assert Path(result["quality_gate_path"]).exists()
        assert any("rentalMutation" in item["file"] for item in result["code_examples"])


@pytest.mark.asyncio
async def test_prepare_review_enforces_version_coverage_in_examples():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src" / "v10" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v21" / "Housing").mkdir(parents=True, exist_ok=True)

        for ver in ("v10", "v20", "v21"):
            (root / "src" / ver / "Housing" / "rentalMutation.Page.al").write_text(
                f"""
page 1101{ver[1:]} rentalMutationTAI
{{
    layout
    {{
        area(Content)
        {{
            repeater(General)
            {{
                field(\"subprocessLeavingTenantRemark\"; Rec.\"SubprocessLeavingTenantRemark\") {{ }}
                field(\"subprocessNewRentalRemark\"; Rec.\"SubprocessNewRentalRemark\") {{ }}
            }}
        }}
    }}
}}
""".strip(),
                encoding="utf-8",
            )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="test-spec-version-coverage",
            human_bullets="""Extend the following API:
rentalMutation

SubprocessLeavingTenantRemark
SubprocessNewRentalRemark
""",
            idempotency_key="key-6",
        )

        assert result["status"] == "ready_for_review"
        checks = result["quality_gate"]["checks"]
        assert checks["version_coverage_in_examples"] is True


@pytest.mark.asyncio
async def test_prepare_review_emits_bulletproof_machine_spec():
    import json as _json

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for ver in ("v10", "v20", "v21"):
            (root / "src" / ver / "Housing").mkdir(parents=True, exist_ok=True)
            (root / "src" / ver / "Housing" / "rentalMutation.Page.al").write_text(
                'page 1 rentalMutationTAI { layout { area(Content) { repeater(g) {'
                ' field("subprocessLeavingTenantOnHoldTill"; Rec."SubprocessLeavingTenantOnHoldTill") { }'
                ' field("subprocessNewRentalOnHoldTill"; Rec."SubprocessNewRentalOnHoldTill") { }'
                ' } } } }',
                encoding="utf-8",
            )

        human = """As external application I want to be able to read and update the values of the fields regarding rental mutation subprocesses, so that I can inform the inspector whether a subprocess is on hold

Extend the following API:
rentalMutation

SubprocessLeavingTenantOnHoldTill
SubprocessLeavingTenantOnHoldIndication
SubprocessNewRentalOnHoldTill
SubprocessNewRentalOnHoldIndication
"""

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-bp",
            human_bullets=human,
            idempotency_key="key-bp",
        )

        assert result["status"] == "ready_for_review"
        spec = _json.loads(Path(result["spec_path"]).read_text(encoding="utf-8"))

        assert spec["schema_version"] == "2.0"
        assert spec["spec_type"] == "feature"
        assert spec["source"]["description_sha256"]
        assert spec["user_stories"] and spec["user_stories"][0]["as_a"]

        assert spec["requirements"]
        assert all(r["ears_type"] for r in spec["requirements"])
        assert all(r["acceptance_tests"] for r in spec["requirements"])
        assert all(r["evidence_refs"] for r in spec["requirements"])

        till = next(d for d in spec["data_model"] if d["field"].endswith("OnHoldTill"))
        assert till["al_type"] == "Date"
        assert till["update"] is True
        assert till["api_attribute"] == "subprocessLeavingTenantOnHoldTill"

        assert spec["acceptance_tests"]
        assert spec["evidence"]
        assert spec["assumptions"]
        assert spec["traceability"]["field_to_object"]

        checks = result["quality_gate"]["checks"]
        assert checks["every_requirement_is_ears"] is True
        assert checks["every_requirement_has_test"] is True
        assert checks["every_field_mapped_to_object"] is True
        assert checks["every_object_has_evidence"] is True
        assert checks["traceability_complete"] is True
        assert checks["no_placeholders_remaining"] is True

        rationale_path = Path(result["rationale_path"])
        assert rationale_path.exists()
        rationale = rationale_path.read_text(encoding="utf-8")
        assert "Decision Rationale (Human Chain of Thought)" in rationale
        assert "How we understood the request" in rationale
        assert "Decision log" in rationale
        review = Path(result["review_path"]).read_text(encoding="utf-8")
        assert "Decision Rationale (Human Chain of Thought)" in review


def _write_app_json(root: Path, ranges) -> None:
    import json as _json

    (root / "app.json").write_text(
        _json.dumps({"name": "TestApi", "publisher": "Zig", "version": "1.0.0.0", "idRanges": ranges}),
        encoding="utf-8",
    )


def _api_page(object_id: int, name: str, read_only: bool) -> str:
    props = (
        "    DataAccessIntent = ReadOnly;\n"
        "    ModifyAllowed = false;\n"
        '    Permissions = tabledata "RentalMutationHSG" = R;\n'
        if read_only
        else '    Permissions = tabledata "RentalMutationHSG" = RIMD;\n'
    )
    return (
        f"page {object_id} {name}\n{{\n"
        "    PageType = API;\n"
        "    APIVersion = 'v2.0';\n"
        "    EntityName = 'rentalMutation';\n"
        '    SourceTable = "RentalMutationHSG";\n'
        f"{props}"
        "    layout { area(Content) { repeater(g) {\n"
        '        field("subprocessLeavingTenantOnHoldTill"; Rec."SubprocessLeavingTenantOnHoldTill") { }\n'
        '        field("subprocessNewRentalOnHoldTill"; Rec."SubprocessNewRentalOnHoldTill") { }\n'
        "    } } }\n}\n"
    )


def _permission_set(object_id: int, name: str, grant: str) -> str:
    return (
        f"permissionset {object_id} {name}\n{{\n"
        "    Assignable = true;\n"
        "    Permissions =\n"
        f'        tabledata "RentalMutationHSG" = {grant};\n'
        "}\n"
    )


_BC_HUMAN = """As external application I want to be able to read and update the values of the fields regarding rental mutation subprocesses, so that I can inform the inspector whether a subprocess is on hold

Extend the following API:
rentalMutation

SubprocessLeavingTenantOnHoldTill
SubprocessNewRentalOnHoldTill
"""


@pytest.mark.asyncio
async def test_analyzer_flags_readonly_api_when_update_required():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=True), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-ro",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-ro",
        )

        assert result["status"] == "needs_clarification"
        assert result["quality_gate"]["checks"]["analysis_no_errors"] is False
        assert any("BC-READONLY" in f for f in result["quality_gate"]["failures"])
        assert Path(result["analysis_path"]).exists()


@pytest.mark.asyncio
async def test_analyzer_passes_for_writable_api_and_parses_id_ranges():
    import json as _json

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-rw",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-rw",
        )

        assert result["status"] == "ready_for_review"
        assert result["quality_gate"]["checks"]["analysis_no_errors"] is True
        assert Path(result["analysis_path"]).exists()

        spec = _json.loads(Path(result["spec_path"]).read_text(encoding="utf-8"))
        assert spec["bc_metadata"]["id_ranges"] == [{"from": 11015500, "to": 11015999}]
        obj = spec["objects_to_modify"][0]
        assert obj["object_id"] == 11015853
        assert obj["id_in_range"] is True
        assert obj["writable"] is True

        review = Path(result["review_path"]).read_text(encoding="utf-8")
        assert "Requirements Checklist (Unit Tests for English)" in review
        assert "Spec Analysis" in review


@pytest.mark.asyncio
async def test_review_packet_leads_with_intent_confirmation():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-intent",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-intent",
        )

        assert result["status"] == "ready_for_review"
        # The human gate must SEE the pinned intent first, including read+update operations.
        review = Path(result["review_path"]).read_text(encoding="utf-8")
        assert "Confirm the Intent (Charter)" in review
        assert "Operations in scope:" in review
        assert "update=yes" in review
        # Intent block must come before the requirements checklist (i.e., reviewed first).
        assert review.index("Confirm the Intent (Charter)") < review.index("Requirements Checklist")
        assert Path(result["charter_path"]).exists()


@pytest.mark.asyncio
async def test_analyzer_flags_permission_set_readonly_for_writable_api():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )
        (root / "src" / "_PermissionSet").mkdir(parents=True, exist_ok=True)
        (root / "src" / "_PermissionSet" / "TableAPI.PermissionSet.al").write_text(
            _permission_set(11015900, "TableAPI", "r"), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-perm-ro",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-perm-ro",
        )

        # Writable page -> gate still passes, but a BC-PERMISSION warning is raised
        # because the app permission set only grants read access.
        analysis = Path(result["analysis_path"]).read_text(encoding="utf-8")
        assert "BC-PERMISSION" in analysis
        assert "tabledata RentalMutationHSG = r" in analysis


@pytest.mark.asyncio
async def test_analyzer_no_permission_warning_when_modify_granted():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )
        (root / "src" / "_PermissionSet").mkdir(parents=True, exist_ok=True)
        (root / "src" / "_PermissionSet" / "TableAPI.PermissionSet.al").write_text(
            _permission_set(11015900, "TableAPI", "Rm"), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-perm-rw",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-perm-rw",
        )

        analysis = Path(result["analysis_path"]).read_text(encoding="utf-8")
        assert "BC-PERMISSION" not in analysis


@pytest.mark.asyncio
async def test_analyzer_suppresses_dataclass_for_pure_api_mapping():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-dc",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-dc",
        )

        # API-page mapping reuses existing table fields; no new table object is in
        # scope, so the DataClassification reminder must not be raised.
        analysis = Path(result["analysis_path"]).read_text(encoding="utf-8")
        assert "BC-DATACLASS" not in analysis


@pytest.mark.asyncio
async def test_analyzer_suppresses_fieldlen_for_pure_api_mapping():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-fieldlen",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-fieldlen",
        )

        # _BC_HUMAN carries long API-facing names, but pure API-page mapping reuses existing
        # table fields, so table-name AL0468 checks must not fire here.
        analysis = Path(result["analysis_path"]).read_text(encoding="utf-8")
        assert "BC-FIELDLEN" not in analysis


def test_analyzer_flags_field_names_over_30_chars_when_table_in_scope():
    spec = {
        "objects_to_modify": [
            {
                "id": "OBJ-001",
                "type": "tableextension",
                "target": "src/Housing/RentalMutation.TableExt.al",
                "object_name": "RentalMutationExt",
            }
        ],
        "requirements": [],
        "data_model": [
            {
                "field": "SubprocessLeavingTenantOnHoldIndication",
                "source_table": "RentalMutationHSG",
                "update": True,
            }
        ],
        "bc_metadata": {"id_ranges": [], "mandatory_affixes": []},
        "scope_boundaries": {"allowed_files": ["src/Housing/RentalMutation.TableExt.al"]},
    }

    findings, _ = _run_spec_analysis(spec, design_text="", tasks_text="", project_root=None)
    codes = {f["code"] for f in findings}
    assert "BC-FIELDLEN" in codes


def test_analyzer_fieldlen_uses_source_field_when_present():
    spec = {
        "objects_to_modify": [
            {
                "id": "OBJ-001",
                "type": "tableextension",
                "target": "src/Housing/RentalMutation.TableExt.al",
                "object_name": "RentalMutationExt",
            }
        ],
        "requirements": [],
        "data_model": [
            {
                "field": "SubprocessLeavingTenantOnHoldIndication",
                "source_field": "SubprocLeavTenantOnHoldIndic",
                "source_table": "RentalMutationHSG",
                "update": True,
            }
        ],
        "bc_metadata": {"id_ranges": [], "mandatory_affixes": []},
        "scope_boundaries": {"allowed_files": ["src/Housing/RentalMutation.TableExt.al"]},
    }

    findings, _ = _run_spec_analysis(spec, design_text="", tasks_text="", project_root=None)
    assert not any(f["code"] == "BC-FIELDLEN" for f in findings)


@pytest.mark.asyncio
async def test_auto_improver_confirms_lesson_after_two_hits():
    from bc_agentic_mcp import lessons as lessons_store

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=True), encoding="utf-8"
        )

        for spec_name, key in (("wi-l1", "k-l1"), ("wi-l2", "k-l2")):
            await handle_prepare_review(
                project_root=str(root),
                spec_name=spec_name,
                human_bullets=_BC_HUMAN,
                idempotency_key=key,
            )

        lessons = lessons_store.load_lessons(root)
        readonly = [l for l in lessons if l["code"] == "BC-READONLY"]
        assert readonly, "expected a recorded BC-READONLY lesson"
        assert readonly[0]["hits"] >= 2
        assert readonly[0]["status"] == "confirmed"
        assert len(readonly[0]["seen_in"]) == 2


@pytest.mark.asyncio
async def test_auto_improver_applies_human_lesson():
    from bc_agentic_mcp import lessons as lessons_store

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        (root / "src" / "v20" / "Housing").mkdir(parents=True, exist_ok=True)
        (root / "src" / "v20" / "Housing" / "rentalMutation.Page.al").write_text(
            _api_page(11015853, "rentalMutationTAI", read_only=False), encoding="utf-8"
        )

        lessons_store.record_human_lesson(
            root,
            message="rentalMutation changes must be reviewed by the Housing team.",
            match={"api": "rentalmutation"},
            severity="warning",
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-human-lesson",
            human_bullets=_BC_HUMAN,
            idempotency_key="k-human",
        )

        assert result["status"] == "ready_for_review"
        analysis = Path(result["analysis_path"]).read_text(encoding="utf-8")
        assert "[learned] rentalMutation changes must be reviewed by the Housing team." in analysis


def test_lessons_decay_stops_surfacing_stale_lessons():
    from datetime import datetime, timezone, timedelta
    from bc_agentic_mcp import lessons as lessons_store

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # Seed a confirmed analyzer lesson by observing it twice.
        for _ in range(2):
            lessons_store.record_observation(
                root,
                code="BC-READONLY",
                message="read-only conflict",
                severity="error",
                match={"api": "rentalmutation"},
                spec_name="wi-x",
            )
        confirmed = [l for l in lessons_store.load_lessons(root) if l["code"] == "BC-READONLY"][0]
        assert confirmed["status"] == "confirmed"

        # Force last_seen far in the past, then decay.
        lessons = lessons_store.load_lessons(root)
        lessons[0]["last_seen"] = (
            datetime.now(timezone.utc) - timedelta(days=200)
        ).isoformat()
        lessons_store._save(root, lessons)

        decayed = lessons_store.apply_decay(root, ttl_days=90)
        assert decayed == 1
        assert not lessons_store.applicable_lessons(
            root, api="rentalmutation", keywords_text=""
        )


def test_lessons_summary_counts():
    from bc_agentic_mcp import lessons as lessons_store

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        lessons_store.record_human_lesson(
            root, message="human rule", match={"api": "rentalmutation"}, severity="warning"
        )
        lessons_store.record_observation(
            root,
            code="BC-READONLY",
            message="ro",
            severity="error",
            match={"api": "rentalmutation"},
            spec_name="wi-x",
        )

        summary = lessons_store.summarize_lessons(root)
        assert summary["total"] == 2
        assert summary["by_severity"].get("warning") == 1
        assert summary["by_severity"].get("error") == 1
        rendered = lessons_store.render_lessons_summary(root)
        assert "Lessons Learned Summary" in rendered
        assert "Top recurring" in rendered


def _entity_api_page(object_id: int, name: str) -> str:
    return (
        f"page {object_id} {name}\n{{\n"
        "    PageType = API;\n"
        "    APIVersion = 'v2.0';\n"
        "    EntityName = 'rentalMutation';\n"
        '    SourceTable = "RentalMutationHSG";\n'
        "    ODataKeyFields = SystemId;\n"
        '    Permissions = tabledata "RentalMutationHSG" = RIMD;\n'
        "    layout { area(Content) { repeater(g) {\n"
        '        field("subprocessLeavingTenantOnHoldTill"; Rec."SubprocessLeavingTenantOnHoldTill") { }\n'
        '        field("subprocessNewRentalOnHoldTill"; Rec."SubprocessNewRentalOnHoldTill") { }\n'
        "    } } }\n}\n"
    )


@pytest.mark.asyncio
async def test_planner_recognizes_multi_version_mirroring_pattern():
    import json as _json

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_app_json(root, [{"from": 11015500, "to": 11015999}])
        # Same entity exposed across three versions + a different entity that must be ignored.
        for ver, oid in (("v10", 11015853), ("v20", 11015854), ("v21", 11015855)):
            p = root / "src" / ver / "Housing"
            p.mkdir(parents=True, exist_ok=True)
            (p / "rentalMutation.Page.al").write_text(_entity_api_page(oid, f"rentalMutation{ver}TAI"), encoding="utf-8")
        other = root / "src" / "v20" / "Housing" / "reasonRentalMutation.Page.al"
        other.write_text(
            "page 11015860 reasonRentalMutationTAI\n{\n    PageType = API;\n"
            "    EntityName = 'reasonRentalMutation';\n    ODataKeyFields = SystemId;\n"
            '    Permissions = tabledata "X" = R;\n    layout { area(Content) { repeater(g) { } } }\n}\n',
            encoding="utf-8",
        )

        result = await handle_prepare_review(
            project_root=str(root),
            spec_name="wi-mirror",
            human_bullets=_BC_HUMAN,
            idempotency_key="key-mirror",
        )

        assert result["status"] == "ready_for_review"
        spec = _json.loads(Path(result["spec_path"]).read_text(encoding="utf-8"))

        # Entity-precise targeting: exactly the 3 rentalMutation pages, NOT reasonRentalMutation.
        targets = spec["scope_boundaries"]["allowed_files"]
        assert len(targets) == 3
        assert all("reasonRentalMutation" not in t for t in targets)
        assert sorted(spec["bc_metadata"]["api_versions"]) == ["v10", "v20", "v21"]

        # Mapping style recognized per field.
        till = next(dm for dm in spec["data_model"] if dm["field"].endswith("OnHoldTill"))
        assert till["api_mapping_expr"] == 'Rec."SubprocessLeavingTenantOnHoldTill"'

        patterns = _json.loads((Path(result["patterns_path"]).parent / "patterns.json").read_text(encoding="utf-8"))
        kinds = {p["pattern"] for p in patterns}
        assert "multi_version_mirroring" in kinds
        assert "api_mapping_style" in kinds
        review = Path(result["review_path"]).read_text(encoding="utf-8")
        assert "Recognized Patterns" in review





