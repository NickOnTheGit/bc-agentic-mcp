"""Tests for al_compiler — real alc.exe wiring (discover, command, error-log parsing)."""
import json
from pathlib import Path

from bc_agentic_mcp import al_compiler


def test_discover_respects_disable_env():
    # conftest sets BC_AGENTIC_DISABLE_COMPILER — compiler must report unavailable.
    assert al_compiler.discover_compiler().available is False


def test_parse_al_native_issues_format(tmp_path):
    log = tmp_path / "log.json"
    log.write_text(json.dumps({
        "version": "0.2",
        "issues": [
            {"ruleId": "AL0224",
             "locations": [{"analysisTarget": [{"uri": str(tmp_path / "src" / "Bad.al"),
                                                "region": {"startLine": 6}}]}],
             "fullMessage": "Expression expected", "properties": {"severity": "Error"}},
            {"ruleId": "AA0001",
             "locations": [{"analysisTarget": [{"uri": str(tmp_path / "src" / "Ok.al"),
                                                "region": {"startLine": 2}}]}],
             "shortMessage": "spacing", "properties": {"severity": "Warning"}},
        ],
    }), encoding="utf-8")
    diags = al_compiler.parse_sarif(log, tmp_path)
    assert diags[0]["code"] == "AL0224" and diags[0]["severity"] == "error"
    assert diags[0]["sourceLocation"]["line"] == 6
    assert diags[0]["sourceLocation"]["file"].endswith("Bad.al")
    assert diags[0]["source"] == "compiler"
    assert diags[1]["severity"] == "warning"


def test_parse_standard_sarif_fallback(tmp_path):
    log = tmp_path / "s.sarif"
    log.write_text(json.dumps({
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"rules": [{"id": "AL0432", "defaultConfiguration": {"level": "warning"}}]}},
            "results": [
                {"ruleId": "AL0432", "message": {"text": "deprecated"},
                 "locations": [{"physicalLocation": {"artifactLocation": {"uri": "file:///x/X.al"},
                                                     "region": {"startLine": 10}}}]},
            ],
        }],
    }), encoding="utf-8")
    diags = al_compiler.parse_sarif(log)
    assert diags[0]["code"] == "AL0432" and diags[0]["severity"] == "warning"
    assert diags[0]["sourceLocation"]["line"] == 10


def test_build_command_shape():
    info = al_compiler.CompilerInfo(["alc.exe"], None)
    cmd = al_compiler.build_command(
        info, Path("proj"), Path("o.app"), Path("e.sarif"),
        package_cache_paths=[Path("pc")], analyzers=["a.dll", "b.dll"],
    )
    joined = " ".join(cmd)
    assert "/project:proj" in joined and "/out:o.app" in joined and "/errorlog:e.sarif" in joined
    assert "/packagecachepath:pc" in joined
    assert "/analyzer:a.dll" in joined and "/analyzer:b.dll" in joined


def test_analyzer_dlls_only_existing(tmp_path):
    adir = tmp_path / "Analyzers"
    adir.mkdir()
    (adir / "Microsoft.Dynamics.Nav.CodeCop.dll").write_text("x")
    (adir / "Microsoft.Dynamics.Nav.UICop.dll").write_text("x")
    info = al_compiler.CompilerInfo(["alc"], adir)
    dlls = al_compiler.analyzer_dlls(info)
    names = {Path(d).name for d in dlls}
    assert names == {"Microsoft.Dynamics.Nav.CodeCop.dll", "Microsoft.Dynamics.Nav.UICop.dll"}

    extra = tmp_path / "LinterCop.dll"
    extra.write_text("x")
    assert str(extra) in al_compiler.analyzer_dlls(info, [str(extra)])


def test_compile_project_with_injected_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(al_compiler, "discover_compiler",
                        lambda: al_compiler.CompilerInfo(["alc.exe"], None, "test"))
    (tmp_path / "app.json").write_text("{}", encoding="utf-8")

    def runner(cmd, errorlog):
        Path(errorlog).write_text(json.dumps({
            "version": "0.2",
            "issues": [{"ruleId": "AL0104",
                        "locations": [{"analysisTarget": [{"uri": str(tmp_path / "src" / "X.al"),
                                                           "region": {"startLine": 3}}]}],
                        "fullMessage": "Syntax error", "properties": {"severity": "Error"}}],
        }), encoding="utf-8")
        return 1

    r = al_compiler.compile_project(str(tmp_path), runner=runner)
    assert r["available"] is True
    assert r["success"] is False
    assert r["errors"] == 1
    assert r["diagnostics"][0]["code"] == "AL0104"
    assert r["project"] == str(tmp_path.resolve())


def test_compile_project_unavailable_when_no_app_json(tmp_path, monkeypatch):
    monkeypatch.setattr(al_compiler, "discover_compiler",
                        lambda: al_compiler.CompilerInfo(["alc.exe"], None, "test"))
    r = al_compiler.compile_project(str(tmp_path))  # no app.json anywhere
    assert r["available"] is False
