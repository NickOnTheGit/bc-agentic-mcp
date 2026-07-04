"""Tests for symbols — authoritative grounding from AL .app symbol packages."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from bc_agentic_mcp import symbols, object_resolver


def _make_app(app_path: Path, symref: dict) -> None:
    """Write a synthetic AL .app: 40-byte header + a zip holding SymbolReference.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SymbolReference.json", json.dumps(symref))
    app_path.parent.mkdir(parents=True, exist_ok=True)
    header = b"NAVX" + b"\x00" * 36  # 40-byte header, no zip magic
    app_path.write_bytes(header + buf.getvalue())


_SYMREF = {
    "Tables": [
        {"Id": 11024288, "Name": "VeraSpaceDetailTypeFDN",
         "ReferenceSourceFileName": "src/Housing/VeraSpaceDetailType.Table.al"},
    ],
    "Pages": [
        {"Id": 11024487, "Name": "VeraSpaceDetailTypesFDN",
         "ReferenceSourceFileName": "src/Housing/VeraSpaceDetailTypes.Page.al"},
    ],
}


@pytest.fixture(autouse=True)
def _clear():
    symbols.clear_cache()
    object_resolver.clear_cache()
    yield
    symbols.clear_cache()
    object_resolver.clear_cache()


def test_read_app_symbols_parses_json(tmp_path):
    app = tmp_path / ".alpackages" / "Pub_App_1.0.0.0.app"
    _make_app(app, _SYMREF)
    parsed = symbols.read_app_symbols(app)
    assert parsed["Tables"][0]["Name"] == "VeraSpaceDetailTypeFDN"


def test_read_app_symbols_none_on_garbage(tmp_path):
    bad = tmp_path / "x.app"
    bad.write_bytes(b"not a zip at all")
    assert symbols.read_app_symbols(bad) is None


def test_build_index_and_lookup(tmp_path):
    _make_app(tmp_path / ".alpackages" / "App.app", _SYMREF)
    lookup = symbols.make_symbol_lookup(tmp_path)

    tbl = lookup("table", "VeraSpaceDetailTypeFDN")
    assert tbl["object_id"] == 11024288
    assert tbl["target"].endswith("VeraSpaceDetailType.Table.al")

    pg = lookup("page", "VeraSpaceDetailTypesFDN")
    assert pg["object_id"] == 11024487
    assert lookup("table", "DoesNotExist") is None


def test_resolver_uses_symbols_even_without_al_files(tmp_path):
    _make_app(tmp_path / ".alpackages" / "App.app", _SYMREF)
    lookup = symbols.make_symbol_lookup(tmp_path)
    objs = [{"name": "VeraSpaceDetailTypeFDN", "kind": "table", "action": "modify"}]
    resolved = object_resolver.resolve(tmp_path, objs, symbol_lookup=lookup)
    assert resolved[0]["resolved"] is True
    assert resolved[0]["object_id"] == 11024288


def test_no_alpackages_yields_empty_index_and_fallback(tmp_path):
    lookup = symbols.make_symbol_lookup(tmp_path)  # no .alpackages
    assert lookup("table", "Anything") is None


def test_symbol_index_disk_cache_avoids_reparse(tmp_path, monkeypatch):
    _make_app(tmp_path / ".alpackages" / "App.app", _SYMREF)
    idx1 = symbols.build_symbol_index(tmp_path)
    assert ("table", "veraspacedetailtypefdn") in idx1
    symbols.clear_cache()  # drop in-memory; the disk cache stays valid (unchanged .app)

    def _boom(_):
        raise AssertionError("disk cache miss: should not re-parse .app packages")

    monkeypatch.setattr(symbols, "read_app_symbols", _boom)
    idx2 = symbols.build_symbol_index(tmp_path)
    assert idx2[("table", "veraspacedetailtypefdn")]["object_id"] == 11024288


def test_symbol_index_respects_time_budget(tmp_path, monkeypatch):
    _make_app(tmp_path / ".alpackages" / "App.app", _SYMREF)
    monkeypatch.setenv("BC_MCP_SYMBOL_BUDGET_S", "-1")  # exhaust the budget immediately
    symbols.clear_cache()
    idx = symbols.build_symbol_index(tmp_path, use_disk_cache=False)
    assert idx == {}  # best-effort: nothing parsed within an exhausted budget
