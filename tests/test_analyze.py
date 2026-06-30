"""Tests for bc_analyze_module."""
import json
import tempfile
from pathlib import Path
import pytest
from bc_agentic_mcp.tools.analyze import (
    scan_al_files,
    extract_naming_conventions,
    analyze_module,
)


@pytest.fixture
def sample_al_project():
    """Create a minimal AL project structure for testing."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        app_json = root / "app.json"
        app_json.write_text(
            json.dumps(
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "name": "TestExtension",
                    "publisher": "TestPublisher",
                    "version": "1.0.0.0",
                    "idRanges": [{"from": 50000, "to": 50099}],
                }
            )
        )
        src = root / "src"
        src.mkdir()
        tables = src / "Tables"
        tables.mkdir()
        (tables / "TestTable.Table.al").write_text(
            'table 50000 "Test Table" { fields { field(1; "Name"; Text[50]) } }'
        )
        codeunits = src / "Codeunits"
        codeunits.mkdir()
        (codeunits / "TestMgt.Codeunit.al").write_text(
            'codeunit 50001 "TestMgt" { trigger OnRun() begin end; }'
        )
        yield root


class TestScanAlFiles:
    def test_finds_objects(self, sample_al_project):
        objects = scan_al_files(sample_al_project)
        assert len(objects) == 2
        obj_types = {o["type"] for o in objects}
        assert "Table" in obj_types
        assert "Codeunit" in obj_types

    def test_extracts_names(self, sample_al_project):
        objects = scan_al_files(sample_al_project)
        names = {o["name"] for o in objects}
        assert "Test Table" in names
        assert "TestMgt" in names


class TestExtractNamingConventions:
    def test_table_naming(self, sample_al_project):
        objects = scan_al_files(sample_al_project)
        conventions = extract_naming_conventions(objects)
        assert "Table" in conventions.get("suffixes", {})
        assert conventions["suffixes"]["Table"] == ".Table.al"

    def test_codeunit_naming(self, sample_al_project):
        objects = scan_al_files(sample_al_project)
        conventions = extract_naming_conventions(objects)
        assert conventions["suffixes"]["Codeunit"] == ".Codeunit.al"


class TestAnalyzeModule:
    def test_returns_module_summary(self, sample_al_project):
        result = analyze_module(sample_al_project)
        assert result["module_summary"]["object_count"] == 2
        assert "objects" in result
        assert "naming_conventions" in result
        assert "patterns" in result
