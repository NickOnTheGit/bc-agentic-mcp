import json
from pathlib import Path

from bc_agentic_mcp.guideline_manifest_check import validate_manifest_sync


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_manifest_check_reports_missing_codes(tmp_path: Path):
    _write(
        tmp_path / "src" / "bc_agentic_mcp" / "guidelines_policy.py",
        "GL-AN001\nGL-API001\n",
    )
    _write(
        tmp_path / ".specs" / "policy" / "guideline_rule_manifest.json",
        json.dumps({"rules": [{"code": "GL-AN001", "applicability": "x"}]}),
    )

    result = validate_manifest_sync(tmp_path)
    assert result["ok"] is False
    assert "GL-API001" in result["missing_in_manifest"]


def test_manifest_check_reports_extra_codes(tmp_path: Path):
    _write(
        tmp_path / "src" / "bc_agentic_mcp" / "guidelines_policy.py",
        "GL-AN001\n",
    )
    _write(
        tmp_path / ".specs" / "policy" / "guideline_rule_manifest.json",
        json.dumps(
            {
                "rules": [
                    {"code": "GL-AN001", "applicability": "x"},
                    {"code": "GL-API099", "applicability": "y"},
                ]
            }
        ),
    )

    result = validate_manifest_sync(tmp_path)
    assert result["ok"] is False
    assert "GL-API099" in result["extra_in_manifest"]


def test_manifest_check_ok_when_synced(tmp_path: Path):
    _write(
        tmp_path / "src" / "bc_agentic_mcp" / "guidelines_policy.py",
        "GL-AN001\nGL-API001\n",
    )
    _write(
        tmp_path / ".specs" / "policy" / "guideline_rule_manifest.json",
        json.dumps(
            {
                "rules": [
                    {"code": "GL-AN001", "applicability": "x"},
                    {"code": "GL-API001", "applicability": "y"},
                ]
            }
        ),
    )

    result = validate_manifest_sync(tmp_path)
    assert result["ok"] is True
    assert result["missing_in_manifest"] == []
    assert result["extra_in_manifest"] == []
