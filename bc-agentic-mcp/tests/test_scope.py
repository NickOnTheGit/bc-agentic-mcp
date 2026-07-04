"""Tests for scope boundary enforcement (security)."""
import tempfile
from pathlib import Path
from bc_agentic_mcp.scope import ScopeEnforcer


def test_allows_declared_file():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "proj"
        root.mkdir()
        enf = ScopeEnforcer(allowed_files=["src/Test.Table.al"], project_root=root)
        assert enf.check_write("src/Test.Table.al") is True


def test_blocks_undeclared_file():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "proj"
        root.mkdir()
        enf = ScopeEnforcer(allowed_files=["src/Test.Table.al"], project_root=root)
        assert enf.check_write("src/Other.Table.al") is False


def test_blocks_sibling_prefix_bypass():
    """A sibling dir sharing the root's name prefix must not be considered in-scope."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        root = base / "proj"
        root.mkdir()
        (base / "proj-evil").mkdir()
        enf = ScopeEnforcer(allowed_files=[], project_root=root)
        # Relative traversal is resolved; the evil sibling is outside root.
        assert enf.check_create("../proj-evil/Hack.al") is False


def test_block_reason_outside_root():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        root = base / "proj"
        root.mkdir()
        (base / "proj-evil").mkdir()
        enf = ScopeEnforcer(allowed_files=[], project_root=root)
        reason = enf.block_reason("../proj-evil/Hack.al")
        assert "outside project root" in reason


def test_check_create_rejects_undeclared_extension():
    """check_create must validate allowed_extensions, not just project root."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        root = base / "proj"
        root.mkdir()
        (root / "EmpireRental").mkdir(parents=True)
        (root / "EmpireHousing").mkdir(parents=True)
        enf = ScopeEnforcer(
            allowed_files=[],
            project_root=root,
            allowed_extensions=["EmpireRental"],
        )
        # Creating a file under declared extension is OK
        assert enf.check_create("EmpireRental/src/Tables/Test.Table.al") is True
        # Creating a file under undeclared extension is NOT OK
        assert enf.check_create("EmpireHousing/src/Tables/Test.Table.al") is False


def test_check_create_allows_when_no_extensions_declared():
    """When allowed_extensions is empty, check_create should be permissive (backward compat)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "proj"
        root.mkdir()
        enf = ScopeEnforcer(allowed_files=[], project_root=root, allowed_extensions=[])
        assert enf.check_create("src/Anything.al") is True


def test_check_create_requires_allowed_file_when_declared():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "proj"
        root.mkdir()
        enf = ScopeEnforcer(
            allowed_files=["src/Allowed.Codeunit.al"],
            project_root=root,
            allowed_extensions=["src"],
        )
        assert enf.check_create("src/Allowed.Codeunit.al") is True
        assert enf.check_create("src/Other.Codeunit.al") is False


def test_permissive_mode_allows_write_in_allowed_extension():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "proj"
        root.mkdir()
        enf = ScopeEnforcer(
            allowed_files=["src/Allowed.Codeunit.al"],
            project_root=root,
            allowed_extensions=["src"],
            scope_mode="permissive",
        )
        assert enf.check_write("src/Other.Codeunit.al") is True


def test_permissive_mode_still_blocks_disallowed_extension():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "proj"
        root.mkdir()
        enf = ScopeEnforcer(
            allowed_files=["src/Allowed.Codeunit.al"],
            project_root=root,
            allowed_extensions=["src"],
            scope_mode="permissive",
        )
        assert enf.check_create("other/Any.Codeunit.al") is False
