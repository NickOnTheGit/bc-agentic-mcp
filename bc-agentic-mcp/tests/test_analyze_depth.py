"""Tests for bc_analyze_module depth handling."""
import json
import tempfile
from pathlib import Path

from bc_agentic_mcp.tools.analyze import analyze_module


def _write_al_project(root: Path, name: str) -> Path:
    project = root / name
    src = project / "src"
    src.mkdir(parents=True)
    (project / "app.json").write_text(
        json.dumps(
            {
                "id": name,
                "name": name,
                "publisher": "Test",
                "version": "1.0.0.0",
                "idRanges": [{"from": 50000, "to": 50099}],
            }
        ),
        encoding="utf-8",
    )
    (src / "Sample.Table.al").write_text(
        'table 50000 "Sample" { fields { field(1; "Code"; Code[20]) { } } }',
        encoding="utf-8",
    )
    (src / "Sample.Page.al").write_text(
        'page 50001 "Sample Card" { }',
        encoding="utf-8",
    )
    return project


def test_basic_depth_skips_similar_module_scan():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        current = _write_al_project(root, "Current")
        _write_al_project(root, "Sibling")

        result = analyze_module(current, depth="basic")

        assert result["module_summary"]["object_count"] == 2
        assert result["similar_implementations"] == []


def test_full_depth_includes_similar_modules():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        current = _write_al_project(root, "Current")
        _write_al_project(root, "Sibling")

        result = analyze_module(current, depth="full")

        assert result["similar_implementations"]
        assert result["similar_implementations"][0]["path"] == "Sibling"


def test_sibling_budget_disables_similarity_scan():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        current = _write_al_project(root, "Current")
        _write_al_project(root, "Sibling")

        result = analyze_module(current, depth="full", max_sibling_modules=0)

        assert result["similar_implementations"] == []