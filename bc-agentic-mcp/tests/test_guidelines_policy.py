import json
import os
import re
from pathlib import Path

import bc_agentic_mcp.guidelines_policy as guidelines_policy
from bc_agentic_mcp.guidelines_policy import scan
from bc_agentic_mcp.workspace import specs_root


def test_guidelines_detect_todo_marker(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    file_path = src / "Sample.Table.al"
    file_path.write_text("// TODO remove\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "rules": [
                    {
                        "id": "GL-TEST-1",
                        "description": "No TODO",
                        "severity": "warning",
                        "pattern": "(?i)\\bTODO\\b",
                        "include": ["src/**/*.al"],
                        "exclude": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    codes = {f["code"] for f in findings}
    assert "GL-TEST-1" in codes
    matched = [f for f in findings if f["code"] == "GL-TEST-1"]
    assert matched[0]["sourceLocation"]["file"] == "src/Sample.Table.al"


def test_guidelines_disabled_returns_no_findings(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Table.al").write_text("// TODO remove\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(
        json.dumps(
            {
                "enabled": False,
                "rules": [
                    {
                        "id": "GL-TEST-1",
                        "description": "No TODO",
                        "severity": "warning",
                        "pattern": "(?i)\\bTODO\\b",
                        "include": ["src/**/*.al"],
                        "exclude": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    assert findings == []


def test_guidelines_require_mandatory_code_analyzers(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Table.al").write_text("table 50100 SampleFDN { }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(
        json.dumps({"enabled": True, "rules": []}),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-AN001" and f["severity"] == "error" for f in findings)


def test_api_page_missing_required_properties_is_flagged(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "ApiSample.Page.al").write_text(
        """
page 50100 ApiSample
{
    PageType = API;
    APIPublisher = 'cegeka';
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(
        json.dumps({"enabled": True, "rules": []}),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    codes = {f["code"] for f in findings}
    assert "GL-API002" in codes  # apigroup
    assert "GL-API003" in codes  # apiversion
    assert "GL-API004" in codes  # entityname
    assert "GL-API005" in codes  # entitysetname
    assert "GL-API006" in codes  # odatakeyfields


def test_api_page_with_required_properties_passes_required_checks(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "ApiGood.Page.al").write_text(
        """
page 50101 ApiGood
{
    PageType = API;
    APIPublisher = 'cegeka';
    APIGroup = 'housing';
    APIVersion = 'beta', 'v2.0';
    EntityName = 'contract';
    EntitySetName = 'contracts';
    ODataKeyFields = id;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(
        json.dumps({"enabled": True, "rules": []}),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    blocked_codes = {"GL-API001", "GL-API002", "GL-API003", "GL-API004", "GL-API005", "GL-API006"}
    assert blocked_codes.isdisjoint({f["code"] for f in findings})


def test_api_page_missing_delayedinsert_and_sourcetable_is_flagged(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "ApiMissing.Page.al").write_text(
        """
page 50102 ApiMissing
{
    PageType = API;
    APIPublisher = 'cegeka';
    APIGroup = 'housing';
    APIVersion = 'v2.0';
    EntityName = 'contract';
    EntitySetName = 'contracts';
    ODataKeyFields = id;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    codes = {f["code"] for f in findings}
    assert "GL-API012" in codes
    assert "GL-API013" in codes


def test_table_field_missing_dataclassification_is_flagged(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "RentalMutation.Table.al").write_text(
        """
table 50100 RentalMutation
{
    fields
    {
        field(1; SomeField; Code[20])
        {
            Caption = 'Some field';
        }
    }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-DC001" for f in findings)


def test_unit_testing_presence_check_is_flagged_without_tests(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Simple.Page.al").write_text("page 50100 Simple { }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-UT001" for f in findings)


def test_api_permission_set_presence_is_checked(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "ApiNoPerm.Page.al").write_text(
        """
page 50110 ApiNoPerm
{
    PageType = API;
    APIPublisher = 'cegeka';
    APIGroup = 'housing';
    APIVersion = 'beta', 'v1.0';
    EntityName = 'contract';
    EntitySetName = 'contracts';
    ODataKeyFields = id;
    DelayedInsert = true;
    SourceTable = Customer;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-API014" for f in findings)


def test_namespace_missing_is_flagged(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "NoNamespace.Codeunit.al").write_text(
        """
codeunit 50120 NoNamespace
{
    Access = Internal;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-NS001" for f in findings)


def test_today_usage_is_flagged(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "UsesToday.Codeunit.al").write_text(
        """
namespace Test;
codeunit 50121 UsesToday
{
    Access = Internal;
    procedure DoWork()
    var d: Date;
    begin
        d := Today();
    end;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-BH004" for f in findings)


def test_hardcoded_secret_is_error(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Secrets.Codeunit.al").write_text(
        """
namespace Test;
codeunit 50122 Secrets
{
    Access = Internal;
    procedure Run()
    begin
        apiKey := 'super-secret';
    end;
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-SC001" and f["severity"] == "error" for f in findings)


def test_human_review_gate_approved_suppresses_manual_gate_finding(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Codeunit.al").write_text("namespace T; codeunit 50123 Sample { Access = Internal; }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")
    (policy_dir / "human_review_gate.json").write_text(
        json.dumps({"approved": True, "approved_by": "QA", "approved_at": "2026-07-02T00:00:00Z"}),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    assert not any(f["code"] == "GL-MN001" for f in findings)


def test_guideline_coverage_contract_flags_missing_page(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Codeunit.al").write_text("namespace T; codeunit 50124 Sample { Access = Internal; }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")
    (policy_dir / "guidelines_sync_report.json").write_text(
        json.dumps({"pages": [{"path": "/ERP/Guidelines/Tooltips"}, {"path": "/ERP/Guidelines/Affixes"}]}),
        encoding="utf-8",
    )
    (policy_dir / "guidelines_coverage_contract.json").write_text(
        json.dumps({"machine_enforced_pages": ["/ERP/Guidelines/Tooltips"], "manual_review_pages": []}),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-COV001" and f["severity"] == "error" for f in findings)


def test_scoped_scan_skips_global_analyzer_and_manifest_checks(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Scoped.Page.al").write_text(
        "page 50100 Scoped\n{\n    PageType = List;\n}\n",
        encoding="utf-8",
    )

    external_specs = tmp_path / "_specs_external"
    os.environ["BC_AGENTIC_SPECS_ROOT"] = str(external_specs)
    try:
        sdir = specs_root(tmp_path) / "wi-scope"
        sdir.mkdir(parents=True)
        (sdir / "spec.json").write_text(
            json.dumps({"scope_boundaries": {"allowed_files": ["src/Scoped.Page.al"]}}),
            encoding="utf-8",
        )

        findings = scan(tmp_path, spec_name="wi-scope")
    finally:
        os.environ.pop("BC_AGENTIC_SPECS_ROOT", None)

    codes = {f["code"] for f in findings}
    assert "GL-AN001" not in codes
    assert "GL-RM001" not in codes


def test_unscoped_scan_still_requires_manifest_when_policy_present(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Page.al").write_text("page 50100 Sample { }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-RM001" and f["severity"] == "error" for f in findings)


def test_guideline_coverage_contract_passes_when_all_pages_covered(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Codeunit.al").write_text("namespace T; codeunit 50125 Sample { Access = Internal; }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")
    (policy_dir / "guidelines_sync_report.json").write_text(
        json.dumps({"pages": [{"path": "/ERP/Guidelines/Tooltips"}, {"path": "/ERP/Guidelines/Affixes"}]}),
        encoding="utf-8",
    )
    (policy_dir / "guidelines_coverage_contract.json").write_text(
        json.dumps(
            {
                "machine_enforced_pages": ["/ERP/Guidelines/Tooltips"],
                "manual_review_pages": [{"path": "/ERP/Guidelines/Affixes", "approved": True}],
            }
        ),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    assert not any(f["code"] in {"GL-COV001", "GL-COV002"} for f in findings)


def test_data_model_approval_not_required_for_non_schema_spec(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Page.al").write_text("namespace T; page 50126 Sample { }\n", encoding="utf-8")

    sdir = tmp_path / ".specs" / "spec-no-dm"
    sdir.mkdir(parents=True)
    (sdir / "spec.json").write_text(
        json.dumps({"work_types": ["page"], "objects_to_modify": [{"type": "page"}], "data_model": []}),
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path, spec_name="spec-no-dm")
    assert not any(f["code"] == "GL-DM001" for f in findings)


def test_data_model_approval_required_for_schema_spec(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.TableExt.al").write_text("namespace T; tableextension 50127 SampleExt extends Customer { }\n", encoding="utf-8")

    sdir = tmp_path / ".specs" / "spec-dm"
    sdir.mkdir(parents=True)
    (sdir / "spec.json").write_text(
        json.dumps({
            "work_types": ["table-field"],
            "objects_to_modify": [{"type": "tableextension"}],
            "data_model": [{"field": "SomeField", "source_table": "Customer"}],
        }),
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path, spec_name="spec-dm")
    assert any(f["code"] == "GL-DM001" for f in findings)


def test_rule_manifest_missing_is_flagged(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Codeunit.al").write_text("namespace T; codeunit 50130 Sample { Access = Internal; }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    findings = scan(tmp_path)
    assert any(f["code"] == "GL-RM001" and f["severity"] == "error" for f in findings)


def test_rule_manifest_present_blocks_manifest_error(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Codeunit.al").write_text("namespace T; codeunit 50131 Sample { Access = Internal; }\n", encoding="utf-8")

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")
    (policy_dir / "human_review_gate.json").write_text(json.dumps({"approved": True}), encoding="utf-8")
    (policy_dir / "guideline_rule_manifest.json").write_text(
        json.dumps(
            {
                "rules": [
                    {"code": "GL-AN001", "applicability": "settings"},
                    {"code": "GL-AC001", "applicability": "codeunits"},
                    {"code": "GL-AC002", "applicability": "procedures"},
                    {"code": "GL-UT001", "applicability": "tests required"},
                    {"code": "GL-NS001", "applicability": "source files"},
                    {"code": "GL-MN001", "applicability": "manual gate"},
                    {"code": "GL-RM001", "applicability": "manifest contract"},
                ]
            }
        ),
        encoding="utf-8",
    )

    findings = scan(tmp_path)
    assert not any(f["code"] == "GL-RM001" for f in findings)


def test_scan_reports_gl_scan002_when_size_guard_skips_files(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Sample.Codeunit.al").write_text(
        "namespace T; codeunit 50140 Sample { Access = Internal; procedure Run() begin end; }\n",
        encoding="utf-8",
    )

    policy_dir = tmp_path / ".specs" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "coding_guidelines.json").write_text(json.dumps({"enabled": True, "rules": []}), encoding="utf-8")

    monkeypatch.setattr(guidelines_policy, "DEFAULT_MAX_SCAN_BYTES", 1)

    findings = scan(tmp_path)
    scan_warning = next((f for f in findings if f["code"] == "GL-SCAN002"), None)
    assert scan_warning is not None
    assert "Scan safety guard skipped some files" in scan_warning["message"]

    m = re.search(r"too_large=(\d+)", scan_warning["message"])
    assert m is not None and int(m.group(1)) > 0
