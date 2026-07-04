"""Tests for the self-contained AL validator and standalone quality check."""
import json
from pathlib import Path

import pytest

from bc_agentic_mcp.al_validator import validate_project
from bc_agentic_mcp.tools.quality_check import handle_quality_check


def _app_json(root: Path, ranges) -> None:
    (root / "app.json").write_text(
        json.dumps({"name": "T", "publisher": "Zig", "version": "1.0.0.0", "idRanges": ranges}),
        encoding="utf-8",
    )


def test_validator_flags_id_out_of_range_and_duplicates(tmp_path):
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.Page.al").write_text(
        "page 99999999 AOutOfRange { PageType = API; EntityName = 'x'; "
        "ODataKeyFields = SystemId; }",
        encoding="utf-8",
    )
    (src / "B1.Page.al").write_text("page 11015501 Dup { }", encoding="utf-8")
    (src / "B2.Page.al").write_text("page 11015501 DupTwo { }", encoding="utf-8")

    diags = validate_project(tmp_path)
    codes = {d["code"] for d in diags}
    assert "V0002" in codes  # out of range
    assert "V0003" in codes  # duplicate id


def test_validator_flags_api_missing_odata_key(tmp_path):
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "Api.Page.al").write_text(
        "page 11015600 ApiPage\n{\n    PageType = API;\n    EntityName = 'rentalMutation';\n}\n",
        encoding="utf-8",
    )
    diags = validate_project(tmp_path)
    assert any(d["code"] == "V0061" for d in diags)


def test_validator_flags_long_table_field_names(tmp_path):
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "R.Table.al").write_text(
        "table 11015700 RTable\n{\n"
        "    fields\n    {\n"
        "        field(1; SubprocessLeavingTenantOnHoldIndication; Boolean) { }\n"  # 39 chars
        "        field(2; SubprocNewRentalOnHoldTill; Date) { }\n"  # 26 chars, OK
        "    }\n}\n",
        encoding="utf-8",
    )
    diags = validate_project(tmp_path)
    long_diags = [d for d in diags if d["code"] == "V0070"]
    assert len(long_diags) == 1
    assert "SubprocessLeavingTenantOnHoldIndication" in long_diags[0]["message"]
    assert long_diags[0]["severity"] == "warning"


def test_validator_does_not_flag_page_field_controls_for_length(tmp_path):
    # Page field controls (name; sourceexpr) must NOT trip the table-field length rule.
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "Api.Page.al").write_text(
        "page 11015600 ApiPage\n{\n    PageType = API;\n    EntityName = 'rentalMutation';\n"
        "    ODataKeyFields = SystemId;\n"
        "    layout { area(Content) { repeater(g) {\n"
        '        field(subprocessLeavingTenantOnHoldIndication; Rec."X") { }\n'
        "    } } }\n}\n",
        encoding="utf-8",
    )
    diags = validate_project(tmp_path)
    assert not [d for d in diags if d["code"] == "V0070"]


def test_validator_clean_project_has_no_errors(tmp_path):
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "Api.Page.al").write_text(
        "page 11015600 ApiPage\n{\n    PageType = API;\n    EntityName = 'rentalMutation';\n"
        "    ODataKeyFields = SystemId;\n}\n",
        encoding="utf-8",
    )
    diags = validate_project(tmp_path)
    assert not [d for d in diags if d["severity"] == "error"]


@pytest.mark.asyncio
async def test_quality_check_runs_self_contained_without_external_tool(tmp_path):
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "Bad.Page.al").write_text("page 99999999 OutOfRange { ", encoding="utf-8")

    result = await handle_quality_check(project_root=str(tmp_path), spec_name="wi-x")
    assert result["mode"] == "self"
    assert result["errors"] >= 1
    assert any(d["code"] in {"V0001", "V0002"} for d in result["diagnostics"])


def _upgrade_pair(root: Path, data_per_company: str, implements: str) -> None:
    """Write a table (with a DataPerCompany value) + an upgrade codeunit modifying it."""
    _app_json(root, [{"from": 11015500, "to": 11015999}])
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "Shared.Table.al").write_text(
        "table 11015700 SharedFDN\n{\n"
        f"    DataPerCompany = {data_per_company};\n"
        "    fields { field(1; Code; Code[10]) { } }\n}\n",
        encoding="utf-8",
    )
    (src / "Upg.Codeunit.al").write_text(
        f"codeunit 11015800 UpgSharedFDN implements {implements}\n"
        "{\n"
        "    Permissions = tabledata SharedFDN = rm;\n"
        "    Subtype = Upgrade;\n"
        "}\n",
        encoding="utf-8",
    )


def test_validator_flags_per_company_upgrade_of_shared_table(tmp_path):
    # DataPerCompany=false + per-company upgrade => V0100.
    _upgrade_pair(tmp_path, "false", "UpgradePerCompanySAN")
    diags = validate_project(tmp_path)
    v0100 = [d for d in diags if d["code"] == "V0100"]
    assert len(v0100) == 1
    assert v0100[0]["severity"] == "warning"
    assert "SharedFDN".lower() in v0100[0]["message"].lower()


def test_validator_flags_per_database_upgrade_of_per_company_table(tmp_path):
    # DataPerCompany=true + per-database upgrade => V0101.
    _upgrade_pair(tmp_path, "true", "UpgradePerDatabaseSAN")
    diags = validate_project(tmp_path)
    assert any(d["code"] == "V0101" for d in diags)


def test_validator_accepts_per_database_upgrade_of_shared_table(tmp_path):
    # DataPerCompany=false + per-database upgrade => correct, no scope diagnostic.
    _upgrade_pair(tmp_path, "false", "UpgradePerDatabaseSAN")
    diags = validate_project(tmp_path)
    assert not [d for d in diags if d["code"] in {"V0100", "V0101"}]


def test_validator_upgrade_scope_skips_unknown_tables(tmp_path):
    # Table not in the scanned set (dependency) => no false positive.
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "Upg.Codeunit.al").write_text(
        "codeunit 11015800 UpgExternalFDN implements UpgradePerCompanySAN\n"
        "{\n    Permissions = tabledata ExternalDepTable = rm;\n    Subtype = Upgrade;\n}\n",
        encoding="utf-8",
    )
    diags = validate_project(tmp_path)
    assert not [d for d in diags if d["code"] in {"V0100", "V0101"}]


def test_validator_include_files_scopes_checks(tmp_path):
    _app_json(tmp_path, [{"from": 11015500, "to": 11015999}])
    src = tmp_path / "src"
    src.mkdir()
    (src / "Good.Page.al").write_text(
        "page 11015600 GoodApi\n{\n    PageType = API;\n    EntityName = 'x';\n    ODataKeyFields = SystemId;\n}\n",
        encoding="utf-8",
    )
    (src / "Bad.Codeunit.al").write_text("codeunit 11015601 Bad { ", encoding="utf-8")

    diags = validate_project(tmp_path, include_files=["src/Good.Page.al"])
    assert not [d for d in diags if d["severity"] == "error"]
