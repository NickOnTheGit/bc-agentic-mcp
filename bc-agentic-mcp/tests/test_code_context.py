"""Tests for repo_state guard and the deterministic code_context finder."""
from bc_agentic_mcp import repo_state, code_context


def _fake_runner(mapping):
    def run(args, cwd):
        key = " ".join(args)
        for k, v in mapping.items():
            if k in key:
                return v
        return (0, "", "")
    return run


def test_repo_status_parses_dirty_and_behind():
    runner = _fake_runner({
        "rev-parse --abbrev-ref HEAD": (0, "main\n", ""),
        "status --porcelain": (0, " M a.al\n?? b.al\n", ""),
        "symbolic-full-name": (0, "origin/main\n", ""),
        "rev-list": (0, "0\t3\n", ""),
    })
    st = repo_state.status(".", runner)
    assert st["branch"] == "main"
    assert st["dirty"] is True
    assert st["behind"] == 3


def test_is_clean_latest_ignores_specs_dir():
    runner = _fake_runner({
        "rev-parse --abbrev-ref HEAD": (0, "main\n", ""),
        "status --porcelain": (0, " M .specs/x.json\n", ""),
        "symbolic-full-name": (0, "origin/main\n", ""),
        "rev-list": (0, "0\t0\n", ""),
    })
    assert repo_state.is_clean_latest(".", runner)["ok"] is True


def test_is_clean_latest_blocks_on_dirty_source():
    runner = _fake_runner({
        "rev-parse --abbrev-ref HEAD": (0, "main\n", ""),
        "status --porcelain": (0, " M src/x.al\n", ""),
        "symbolic-full-name": (1, "", ""),  # no upstream
    })
    res = repo_state.is_clean_latest(".", runner)
    assert res["ok"] is False
    assert res["blocking_dirty"] == ["src/x.al"]


def test_code_context_siblings_and_conventions(tmp_path):
    d = tmp_path / "extensions" / "App" / "src" / "Foo"
    d.mkdir(parents=True)
    (d / "FooTable.Table.al").write_text(
        "table 50000 FooTable\n{\n    DataPerCompany = false;\n"
        "    fields { field(1; A; Integer) { } field(10; B; Integer) { } }\n}\n",
        encoding="utf-8",
    )
    (d / "FooCard.Page.al").write_text("page 50001 FooCard { }", encoding="utf-8")
    up = tmp_path / "extensions" / "App" / "src" / "_Upgrade"
    up.mkdir(parents=True)
    (up / "AddFoo.Codeunit.al").write_text("codeunit 50002 AddFoo implements X { }", encoding="utf-8")

    code_context.object_resolver.clear_cache()
    ctx = code_context.build(
        str(tmp_path), [{"kind": "table", "name": "FooTable"}],
        work_types=["table-field", "upgrade"], require_clean_latest=False,
    )
    assert ctx["status"] == "ok"
    sibs = [s["path"] for s in ctx["similar"]["siblings"]]
    assert any("FooCard.Page.al" in s for s in sibs)
    assert ctx["conventions"]["data_per_company"] == "false"
    assert ctx["conventions"]["max_field_id"] == 10
    assert ctx["conventions"]["next_field_id_hint"] == 20
    assert ctx["similar"]["upgrade_precedents"]


def test_code_context_blocks_when_repo_dirty(tmp_path):
    runner = _fake_runner({
        "rev-parse --abbrev-ref HEAD": (0, "main\n", ""),
        "status --porcelain": (0, " M src/x.al\n", ""),
        "symbolic-full-name": (1, "", ""),
    })
    ctx = code_context.build(
        str(tmp_path), [], work_types=[], require_clean_latest=True, auto_pull=False, runner=runner,
    )
    assert ctx["status"] == "blocked_repo_not_clean_latest"


def test_stale_code_context_detector(tmp_path):
    import json
    from bc_agentic_mcp import detectors, object_resolver
    (tmp_path / "a.al").write_text("table 1 A {}", encoding="utf-8")
    object_resolver.clear_cache()
    cc_dir = tmp_path / ".specs" / "s1" / "context" / "code"
    cc_dir.mkdir(parents=True)
    (cc_dir / "code_context.json").write_text(json.dumps({"repo_index_sha": "DIFFERENT"}), encoding="utf-8")
    findings = detectors.detect(tmp_path, "s1", diagnostics=[])
    assert any(f["detector"] == "stale_code_context" for f in findings)
