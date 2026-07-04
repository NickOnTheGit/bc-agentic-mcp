"""Tests for regression comparison and baseline management."""
import json
import os
from pathlib import Path

import pytest

from bc_agentic_mcp.tools.quality_check import (
    save_baseline,
    load_baseline,
    compare_diagnostics,
    _cleanup_old_baselines,
    _key,
)
from bc_agentic_mcp.workspace import specs_root


def _make_diag(code: str, message: str, file: str, line: int) -> dict:
    """Helper to create a diagnostic dict with the expected structure."""
    return {
        "code": code,
        "message": message,
        "severity": "error",
        "sourceLocation": {"file": file, "line": line},
    }


# ---------------------------------------------------------------------------
# _key() tests
# ---------------------------------------------------------------------------

def test_key_uses_code_and_message_prefix_and_fileline():
    """_key must use code, message prefix, file, and line for stable matching."""
    diag = _make_diag("AA0001", "The field X must have a suffix: FieldName", "src/X.Table.al", 42)
    k = _key(diag)
    assert "AA0001" in k
    assert "The field X must have a suffix" in k  # prefix before ':'
    assert "src/X.Table.al" in k
    assert "42" in k


def test_key_ignores_full_message_wording():
    """Two diagnostics with different wording after ':' but same prefix should match."""
    d1 = _make_diag("AA0001", "The field X must have a suffix: OldWording", "src/X.Table.al", 10)
    d2 = _make_diag("AA0001", "The field X must have a suffix: NewWording", "src/X.Table.al", 10)
    assert _key(d1) == _key(d2)


# ---------------------------------------------------------------------------
# Baseline save/load tests
# ---------------------------------------------------------------------------

def test_save_and_load_baseline(tmp_path):
    """Save a baseline and load it back."""
    baseline_dir = tmp_path / ".baselines"
    diagnostics = [_make_diag("AA0001", "Error 1", "src/X.al", 1)]

    saved_path = save_baseline(baseline_dir, diagnostics)
    assert saved_path.exists()
    assert saved_path.name.startswith("baseline_")

    loaded = load_baseline(baseline_dir)
    assert loaded is not None
    assert len(loaded["diagnostics"]) == 1
    assert loaded["diagnostics"][0]["code"] == "AA0001"


def test_load_baseline_returns_none_when_no_baselines(tmp_path):
    """load_baseline should return None when no baselines exist."""
    baseline_dir = tmp_path / ".baselines_nonexistent"
    assert load_baseline(baseline_dir) is None


# ---------------------------------------------------------------------------
# compare_diagnostics tests
# ---------------------------------------------------------------------------

def test_compare_new_error_regression():
    """compare_diagnostics should detect new errors as a regression."""
    baseline = [_make_diag("AA0001", "Old error", "src/X.al", 1)]
    current = [
        _make_diag("AA0001", "Old error", "src/X.al", 1),
        _make_diag("AA0002", "New error", "src/Y.al", 10),
    ]
    result = compare_diagnostics(baseline, current)
    assert result["regression"] is True
    assert len(result["new_errors"]) == 1
    assert result["new_errors"][0]["code"] == "AA0002"
    assert len(result["fixed_errors"]) == 0


def test_compare_fixed_error_improvement():
    """compare_diagnostics should detect fixed errors as improvement."""
    baseline = [
        _make_diag("AA0001", "Old error", "src/X.al", 1),
        _make_diag("AA0002", "Was there", "src/Y.al", 10),
    ]
    current = [_make_diag("AA0001", "Old error", "src/X.al", 1)]
    result = compare_diagnostics(baseline, current)
    assert result["regression"] is False
    assert len(result["new_errors"]) == 0
    assert len(result["fixed_errors"]) == 1
    assert result["fixed_errors"][0]["code"] == "AA0002"


def test_compare_identical_no_regression():
    """compare_diagnostics should report no regression when identical."""
    baseline = [_make_diag("AA0001", "Error", "src/X.al", 1)]
    current = [_make_diag("AA0001", "Error", "src/X.al", 1)]
    result = compare_diagnostics(baseline, current)
    assert result["regression"] is False
    assert len(result["new_errors"]) == 0
    assert len(result["fixed_errors"]) == 0


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------

def test_cleanup_old_baselines_keeps_only_five(tmp_path):
    """_cleanup_old_baselines should keep only the 5 most recent baselines."""
    baseline_dir = tmp_path / ".baselines"
    baseline_dir.mkdir()

    # Create 7 baselines
    for i in range(7):
        (baseline_dir / f"baseline_20250101_{i:04d}.json").write_text(
            json.dumps({"timestamp": f"2025-01-01T00:0{i}:00", "diagnostics": []})
        )

    assert len(list(baseline_dir.glob("baseline_*.json"))) == 7

    _cleanup_old_baselines(baseline_dir, keep=5)

    remaining = sorted(baseline_dir.glob("baseline_*.json"))
    assert len(remaining) == 5


@pytest.mark.asyncio
async def test_quality_check_scopes_diagnostics_to_spec_allowed_files(tmp_path):
    (tmp_path / "app.json").write_text(
        json.dumps({"name": "T", "publisher": "Zig", "version": "1.0.0.0", "idRanges": [{"from": 11015500, "to": 11015999}]}),
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "Scoped.Page.al").write_text(
        "page 11015600 ScopedApi\n{\n    PageType = API;\n    APIPublisher = 'cegeka';\n    APIGroup = 'housing';\n    APIVersion = 'v1.0';\n    EntityName = 'ok';\n    EntitySetName = 'oks';\n    ODataKeyFields = SystemId;\n    DelayedInsert = true;\n    SourceTable = Customer;\n}\n",
        encoding="utf-8",
    )
    (src / "UnrelatedBroken.Codeunit.al").write_text("codeunit 11015601 Broken { ", encoding="utf-8")

    external_specs = tmp_path / "_specs_external"
    os.environ["BC_AGENTIC_SPECS_ROOT"] = str(external_specs)
    try:
        sdir = specs_root(tmp_path) / "wi-scope"
        sdir.mkdir(parents=True)
        (sdir / "spec.json").write_text(
            json.dumps({"scope_boundaries": {"allowed_files": ["src/Scoped.Page.al"]}}),
            encoding="utf-8",
        )

        result = await __import__("bc_agentic_mcp.tools.quality_check", fromlist=["handle_quality_check"]).handle_quality_check(
            project_root=str(tmp_path), spec_name="wi-scope"
        )
    finally:
        os.environ.pop("BC_AGENTIC_SPECS_ROOT", None)

    assert result["errors"] == 0
    assert not [d for d in result["diagnostics"] if d.get("code") == "V0001"]
