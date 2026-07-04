"""Tests for the permission-coverage checker (table-level grants + include resolution)."""
from bc_agentic_mcp import permissions

# Modeled on the real EmpireOpen rentalMutation permission sets.
_LEZ = '''namespace Zig.AccessControl;
permissionset 11235651 "API-EMP-VRH-MUT-LEZ"
{
    IncludedPermissionSets = LOGIN;
    Permissions = page EmpRentalMutationOPN = X,
                  page EmpRentalMutationv20OPN = X,
                  tabledata RentalMutationHSG = r;
}'''

_WIJZ = '''namespace Zig.AccessControl;
permissionset 11235650 "API-EMP-VRH-MUT-WIJZ"
{
    IncludedPermissionSets = "API-EMP-VRH-MUT-LEZ";
    Permissions = tabledata RentalMutationHSG = m,
                  tabledata RentalMutAttentionPointHSG = r;
}'''


def test_parse_permission_set():
    p = permissions.parse_permission_set(_WIJZ)
    assert p["name"] == "API-EMP-VRH-MUT-WIJZ" and p["id"] == 11235650
    assert p["included"] == ["API-EMP-VRH-MUT-LEZ"]
    assert p["tabledata"]["rentalmutationhsg"] == "M"


def test_effective_grants_merge_includes():
    sets = {
        permissions._norm("API-EMP-VRH-MUT-LEZ"): permissions.parse_permission_set(_LEZ),
        permissions._norm("API-EMP-VRH-MUT-WIJZ"): permissions.parse_permission_set(_WIJZ),
    }
    eff = permissions.effective_tabledata(sets, "API-EMP-VRH-MUT-WIJZ")
    # WIJZ grants M directly + R via included LEZ -> effective RM at table level.
    assert eff["rentalmutationhsg"] == "MR" or set(eff["rentalmutationhsg"]) == {"M", "R"}


def test_covers():
    assert permissions.covers("MR", "R") is True
    assert permissions.covers("MR", "RM") is True
    assert permissions.covers("R", "M") is False


def test_find_coverage_says_no_change_needed(tmp_path):
    (tmp_path / "lez.PermissionSet.al").write_text(_LEZ, encoding="utf-8")
    (tmp_path / "wijz.PermissionSet.al").write_text(_WIJZ, encoding="utf-8")
    # Adding fields for UPDATE: WIJZ already grants M at table level -> no permission change.
    r = permissions.find_coverage(str(tmp_path), "RentalMutationHSG", "M")
    assert r["covered"] is True
    assert r["permission_change_needed"] is False
    assert any(c["permission_set"] == "API-EMP-VRH-MUT-WIJZ" for c in r["covering_sets"])
    # Read is covered by both LEZ and (via include) WIJZ.
    r2 = permissions.find_coverage(str(tmp_path), "RentalMutationHSG", "R")
    assert r2["covered"] is True


def test_find_coverage_flags_missing_grant(tmp_path):
    (tmp_path / "lez.PermissionSet.al").write_text(_LEZ, encoding="utf-8")
    # Only read (LEZ) present; a required MODIFY is NOT covered -> change needed.
    r = permissions.find_coverage(str(tmp_path), "RentalMutationHSG", "M")
    assert r["covered"] is False and r["permission_change_needed"] is True
    # A different table not granted at all.
    r2 = permissions.find_coverage(str(tmp_path), "SomeOtherTable", "R")
    assert r2["covered"] is False
