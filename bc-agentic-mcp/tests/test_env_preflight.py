"""Phase 2 (A1 + A3 + A4): env preflight gate, symbol cache, full-cycle runner.

Everything runs against injected seams — no docker, no network, no PowerShell.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bc_agentic_mcp import al_runner, env_preflight
from bc_agentic_mcp.tools.env_preflight import handle_env_preflight
from bc_agentic_mcp.tools.run_tests import handle_run_tests


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# check_license
# ---------------------------------------------------------------------------

def test_check_license_finds_first_candidate(monkeypatch):
    monkeypatch.delenv("BC_LICENSE_FILE", raising=False)
    hit = f"C:\\ProgramData\\BcContainerHelper\\Extensions\\acctest\\my\\license.flf"
    r = env_preflight.check_license("acctest", path_exists=lambda p: p == hit)
    assert r["ok"] is True and r["path"] == hit


def test_check_license_env_override_wins(monkeypatch):
    monkeypatch.setenv("BC_LICENSE_FILE", "D:\\lic\\custom.bclicense")
    r = env_preflight.check_license("acctest", path_exists=lambda p: p == "D:\\lic\\custom.bclicense")
    assert r["ok"] is True and r["path"] == "D:\\lic\\custom.bclicense"


def test_check_license_missing_names_exact_paths(monkeypatch):
    monkeypatch.delenv("BC_LICENSE_FILE", raising=False)
    r = env_preflight.check_license("acctest", path_exists=lambda p: False)
    assert r["ok"] is False
    assert "license.bclicense" in r["blocker"] and "BC_LICENSE_FILE" in r["blocker"]
    assert len(r["checked"]) == 6


# ---------------------------------------------------------------------------
# check_container_health / fingerprint
# ---------------------------------------------------------------------------

def test_container_health_running_and_healthy():
    r = env_preflight.check_container_health("c", runner=lambda cmd: _proc(0, "running|healthy\n"))
    assert r["ok"] is True and r["status"] == "healthy"
    r = env_preflight.check_container_health("c", runner=lambda cmd: _proc(0, "running|\n"))
    assert r["ok"] is True and r["status"] == "running"


def test_container_health_exited_blocks():
    r = env_preflight.check_container_health("c", runner=lambda cmd: _proc(0, "exited|\n"))
    assert r["ok"] is False and "exited" in r["blocker"]


def test_container_health_not_found_blocks():
    r = env_preflight.check_container_health("c", runner=lambda cmd: _proc(1, "", "No such object"))
    assert r["ok"] is False and r["status"] == "not-found"


def test_fingerprint_strips_sha_prefix():
    fp = env_preflight.get_container_fingerprint(
        "c", runner=lambda cmd: _proc(0, "sha256:abcdef0123456789deadbeef\n")
    )
    assert fp == "abcdef0123456789"
    assert env_preflight.get_container_fingerprint("c", runner=lambda cmd: _proc(1)) == "unknown"


# ---------------------------------------------------------------------------
# probe_dependency_symbols
# ---------------------------------------------------------------------------

_DEPS = [
    {"id": "11111111-1111-1111-1111-111111111111", "name": "Base App", "version": "28.0.0.0"},
    {"id": "22222222-2222-2222-2222-222222222222", "name": "Custom Dep", "version": "1.0.0.0"},
]


def test_probe_all_present(monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")
    r = env_preflight.probe_dependency_symbols(
        _DEPS, base_url="http://10.0.0.5:7049/BC", fetcher=lambda url, h: (200, "")
    )
    assert r["ok"] is True and r["missing"] == []
    assert all(x["state"] == "ok" for x in r["results"])


def test_probe_missing_dep_names_al1024(monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")

    def fetch(url, headers):
        return (404, "") if "22222222" in url else (200, "")

    r = env_preflight.probe_dependency_symbols(_DEPS, base_url="http://x:7049/BC", fetcher=fetch)
    assert r["ok"] is False
    assert r["missing"] == ["Custom Dep 1.0.0.0"]
    assert "AL1024" in r["blocker"]


def test_probe_auth_failure_classified(monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "wrong")
    r = env_preflight.probe_dependency_symbols(
        _DEPS[:1], base_url="http://x:7049/BC", fetcher=lambda url, h: (401, "")
    )
    assert r["ok"] is False and r["results"][0]["state"] == "auth"
    assert "auth" in r["blocker"]


def test_probe_sends_basic_auth_but_never_stores_password(monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "s3cret")
    seen = {}

    def fetch(url, headers):
        seen["auth"] = headers.get("Authorization", "")
        return (200, "")

    r = env_preflight.probe_dependency_symbols(_DEPS[:1], base_url="http://x:7049/BC", fetcher=fetch)
    assert seen["auth"].startswith("Basic ")
    assert "s3cret" not in json.dumps(r)


# ---------------------------------------------------------------------------
# Manifest + freshness + gate
# ---------------------------------------------------------------------------

def _passing_manifest(container="acctest", fingerprint="abc123", **overrides):
    m = env_preflight.build_manifest(
        container,
        fingerprint=fingerprint,
        checks={"container": {"ok": True, "status": "healthy"}},
    )
    m.update(overrides)
    return m


def test_manifest_roundtrip(tmp_path):
    m = _passing_manifest()
    path = env_preflight.save_manifest(tmp_path, m)
    assert Path(path).exists()
    loaded = env_preflight.load_manifest(tmp_path, "acctest")
    assert loaded["ok"] is True and loaded["fingerprint"] == "abc123"


def test_load_manifest_unreadable_is_stale(tmp_path):
    p = env_preflight.manifest_path(tmp_path, "acctest")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert env_preflight.load_manifest(tmp_path, "acctest") is None


def test_is_fresh_ttl_and_fingerprint():
    m = _passing_manifest()
    assert env_preflight.is_fresh(m) is True
    old = datetime.now(timezone.utc) - timedelta(seconds=env_preflight.DEFAULT_TTL_SECONDS + 60)
    stale = _passing_manifest(generated_at=old.isoformat())
    assert env_preflight.is_fresh(stale) is False
    # image changed => stale; unknown recorded fingerprint => tolerated
    assert env_preflight.is_fresh(m, current_fingerprint="other") is False
    unknown = _passing_manifest(fingerprint="unknown")
    assert env_preflight.is_fresh(unknown, current_fingerprint="whatever") is True


def test_require_fresh_missing_points_to_preflight(tmp_path):
    gate = env_preflight.require_fresh(tmp_path, "acctest")
    assert gate["ok"] is False
    assert gate["next_action"]["tool"] == "bc_env_preflight"


def test_require_fresh_failed_manifest_lists_blockers(tmp_path):
    m = env_preflight.build_manifest(
        "acctest", fingerprint="f",
        checks={"license": {"ok": False, "blocker": "No license file found."}},
    )
    env_preflight.save_manifest(tmp_path, m)
    gate = env_preflight.require_fresh(tmp_path, "acctest")
    assert gate["ok"] is False and "license" in gate["reason"]


def test_require_fresh_self_heals_expired_manifest_when_container_unchanged(tmp_path):
    """TTL expiry with an unchanged healthy container re-stamps instead of blocking."""
    old = datetime.now(timezone.utc) - timedelta(seconds=env_preflight.DEFAULT_TTL_SECONDS + 120)
    env_preflight.save_manifest(tmp_path, _passing_manifest(generated_at=old.isoformat()))

    def runner(cmd):
        if "{{.Image}}" in cmd[3]:
            return _proc(stdout="sha256:abc123\n")
        return _proc(stdout="running|healthy\n")

    gate = env_preflight.require_fresh(tmp_path, "acctest", runner=runner)
    assert gate["ok"] is True, gate.get("reason")
    assert gate.get("refreshed") is True
    # the re-stamp is persisted: the next gate passes without a runner
    assert env_preflight.require_fresh(tmp_path, "acctest")["ok"] is True


def test_require_fresh_blocks_expired_manifest_when_image_changed(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(seconds=env_preflight.DEFAULT_TTL_SECONDS + 120)
    env_preflight.save_manifest(tmp_path, _passing_manifest(generated_at=old.isoformat()))

    def runner(cmd):
        if "{{.Image}}" in cmd[3]:
            return _proc(stdout="sha256:DIFFERENT0000000\n")
        return _proc(stdout="running|healthy\n")

    gate = env_preflight.require_fresh(tmp_path, "acctest", runner=runner)
    assert gate["ok"] is False
    assert gate["next_action"]["tool"] == "bc_env_preflight"


def test_require_fresh_blocks_expired_manifest_when_container_unhealthy(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(seconds=env_preflight.DEFAULT_TTL_SECONDS + 120)
    env_preflight.save_manifest(tmp_path, _passing_manifest(generated_at=old.isoformat()))
    gate = env_preflight.require_fresh(
        tmp_path, "acctest", runner=lambda cmd: _proc(stdout="exited|\n")
    )
    assert gate["ok"] is False


def test_require_fresh_passing(tmp_path):
    env_preflight.save_manifest(tmp_path, _passing_manifest())
    gate = env_preflight.require_fresh(tmp_path, "acctest")
    assert gate["ok"] is True and gate["manifest"]["fingerprint"] == "abc123"


# ---------------------------------------------------------------------------
# handle_env_preflight (tool handler, checks monkeypatched)
# ---------------------------------------------------------------------------

def test_handle_env_preflight_all_green_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(env_preflight, "check_container_health", lambda c: {"ok": True, "status": "healthy"})
    monkeypatch.setattr(env_preflight, "check_license", lambda c: {"ok": True, "path": "x"})
    monkeypatch.setattr(env_preflight, "check_shared_folder", lambda c: {"ok": True, "path": "y"})
    monkeypatch.setattr(env_preflight, "get_container_fingerprint", lambda c: "fp16")
    out = asyncio.run(handle_env_preflight(str(tmp_path), "acctest"))
    assert out["status"] == "env_ok" and out["ok"] is True
    assert out["checks"]["dependency_symbols"]["skipped"] is True
    assert env_preflight.load_manifest(tmp_path, "acctest")["fingerprint"] == "fp16"


def test_handle_env_preflight_blocked_reports_blockers(tmp_path, monkeypatch):
    monkeypatch.setattr(env_preflight, "check_container_health", lambda c: {"ok": False, "status": "exited", "blocker": "Container 'acctest' is exited."})
    monkeypatch.setattr(env_preflight, "check_license", lambda c: {"ok": True, "path": "x"})
    monkeypatch.setattr(env_preflight, "check_shared_folder", lambda c: {"ok": True, "path": "y"})
    monkeypatch.setattr(env_preflight, "get_container_fingerprint", lambda c: "fp16")
    out = asyncio.run(handle_env_preflight(str(tmp_path), "acctest"))
    assert out["status"] == "env_blocked" and out["blocked"] is True
    assert any("exited" in b for b in out["blockers"])
    # a FAILED manifest is persisted too — the gate must find and report it
    assert env_preflight.load_manifest(tmp_path, "acctest")["ok"] is False


def test_handle_env_preflight_probes_dependencies(tmp_path, monkeypatch):
    app_json = tmp_path / "app.json"
    app_json.write_text(json.dumps({"dependencies": _DEPS}), encoding="utf-8")
    monkeypatch.setattr(env_preflight, "check_container_health", lambda c: {"ok": True, "status": "healthy"})
    monkeypatch.setattr(env_preflight, "check_license", lambda c: {"ok": True, "path": "x"})
    monkeypatch.setattr(env_preflight, "check_shared_folder", lambda c: {"ok": True, "path": "y"})
    monkeypatch.setattr(env_preflight, "get_container_fingerprint", lambda c: "fp16")
    monkeypatch.setattr(env_preflight, "get_container_ip", lambda c: "10.0.0.9")
    # The user probe does real HTTP against the (fake) container IP when unmocked:
    # 40s+ of socket retries and flaky under concurrent Docker load (observed live).
    monkeypatch.setattr(env_preflight, "probe_container_user",
                        lambda **kw: {"user": "admin", "probed": ["admin"]})
    captured = {}

    def probe(deps, *, base_url, tenant, user, credential_env):
        captured["base_url"] = base_url
        captured["count"] = len(deps)
        return {"ok": True, "results": [], "missing": []}

    monkeypatch.setattr(env_preflight, "probe_dependency_symbols", probe)
    out = asyncio.run(handle_env_preflight(str(tmp_path), "acctest", app_json_path=str(app_json)))
    assert out["ok"] is True
    assert captured == {"base_url": "http://10.0.0.9:7049/BC", "count": 2}


# ---------------------------------------------------------------------------
# handle_run_tests: A1 gate + A4 full cycle
# ---------------------------------------------------------------------------

def test_run_tests_blocked_without_preflight(tmp_path):
    out = asyncio.run(handle_run_tests(str(tmp_path), "acctest", "ext-id"))
    assert out["status"] == "blocked_env_preflight" and out["blocked"] is True
    assert out["next_action"]["tool"] == "bc_env_preflight"


def test_run_tests_proceeds_with_fresh_manifest(tmp_path, monkeypatch):
    env_preflight.save_manifest(tmp_path, _passing_manifest())
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {"executed": True, "all_passed": True, "passed": 3, "total": 3, "exit_code": 0}

    monkeypatch.setattr(al_runner, "run_container_tests", lambda **kw: fake_run(**kw))
    out = asyncio.run(handle_run_tests(str(tmp_path), "acctest", "ext-id"))
    assert out["all_passed"] is True
    assert seen["container_name"] == "acctest"


def test_run_tests_full_cycle_uses_manifest_fingerprint(tmp_path, monkeypatch):
    env_preflight.save_manifest(tmp_path, _passing_manifest(fingerprint="imgfp"))
    seen = {}

    def fake_cycle(**kwargs):
        seen.update(kwargs)
        return {"executed": True, "all_passed": True, "passed": 5, "total": 5,
                "exit_code": 0, "steps": [], "cycle": "sync->compile->publish->run"}

    monkeypatch.setattr(al_runner, "run_full_cycle", lambda **kw: fake_cycle(**kw))
    out = asyncio.run(handle_run_tests(
        str(tmp_path), "acctest", "ext-id", app_project_folder="C:\\src\\testapp"
    ))
    assert out["cycle"] == "sync->compile->publish->run"
    assert seen["fingerprint"] == "imgfp"
    assert seen["app_project_folder"] == "C:\\src\\testapp"


# ---------------------------------------------------------------------------
# al_runner: full-cycle builders (A4) + symbol cache (A3)
# ---------------------------------------------------------------------------

def test_build_sync_command_mirrors_and_excludes():
    cmd = al_runner.build_sync_command(source_dir="C:\\src\\app", target_dir="C:\\build\\app")
    assert cmd[0] == "robocopy" and "/MIR" in cmd
    assert ".git" in cmd and ".alpackages" in cmd


def test_build_publish_command_uses_proven_dev_endpoint_mode():
    cmd = al_runner.build_publish_command(container_name="c", app_file="C:\\out\\a.app")
    ps = cmd[-1]
    assert "-useDevEndpoint" in ps and "-scope Tenant" in ps
    assert "PUBLISH_DONE" in ps
    assert "$env:BC_TEST_PASSWORD" in ps  # secret resolved in-child, never inline


def test_build_reinstall_dependents_command_fixpoint():
    # Encoded from the wi267598 livelock: a base publish leaves dependents
    # published+Synced+uninstalled; the recovery is a fixpoint Install loop,
    # never terminal improvisation.
    cmd = al_runner.build_reinstall_dependents_command(container_name="acctest")
    ps = cmd[-1]
    assert "Install-BcContainerApp" in ps
    assert "IsInstalled" in ps and "Synced" in ps
    assert "REINSTALL_DONE" in ps and "REINSTALL_REMAINING" in ps
    assert "$pass -le 5" in ps  # bounded fixpoint, no infinite loop
    import pytest
    with pytest.raises(ValueError):
        al_runner.build_reinstall_dependents_command(container_name="")


def test_build_compile_command_symbol_copy_flag():
    with_copy = al_runner.build_compile_command(
        container_name="c", project_folder="p", output_folder="o",
        symbols_folder="s", copy_symbols=True,
    )[-1]
    without = al_runner.build_compile_command(
        container_name="c", project_folder="p", output_folder="o",
        symbols_folder="s", copy_symbols=False,
    )[-1]
    assert "-CopySymbolsFromContainer" in with_copy
    assert "-CopySymbolsFromContainer" not in without
    # -UpdateSymbols is BANNED: it forces an authenticated dev-endpoint symbol
    # download that 401'd live while the filesystem copy had already succeeded.
    assert "-UpdateSymbols" not in with_copy
    assert "-UpdateSymbols" not in without


def test_use_symbol_cache_decision():
    assert al_runner.use_symbol_cache("d", list_dir=lambda p: []) is False
    assert al_runner.use_symbol_cache("d", list_dir=lambda p: ["Base.app", "x.txt"]) is True
    def boom(p):
        raise OSError("gone")
    assert al_runner.use_symbol_cache("d", list_dir=boom) is False


def test_parse_step_output_marker_and_errors():
    assert al_runner.parse_step_output("...\nCOMPILE_DONE: True\n", "COMPILE_DONE")["ok"] is True
    bad = al_runner.parse_step_output(
        "error AL1024: something\nCOMPILE_DONE: True", "COMPILE_DONE"
    )
    assert bad["ok"] is False and bad["errors"] == ["AL1024"]


def _cycle_ops(tmp_path):
    return {
        "mkdir": lambda p: None,
        "newest_app": lambda folder: "C:\\build\\out\\app.app",
    }


def test_run_full_cycle_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")
    calls = []

    def runner(cmd):
        calls.append(cmd[0])
        if cmd[0] == "robocopy":
            return _proc(1)  # robocopy 1 == copied files, success
        joined = cmd[-1]
        if "Compile-AppInBcContainer" in joined:
            return _proc(0, "COMPILE_DONE: True")
        if "Publish-BcContainerApp" in joined:
            return _proc(0, "PUBLISH_DONE: True")
        return _proc(0, "ALL_TESTS_PASSED: True\n")

    r = al_runner.run_full_cycle(
        container_name="acctest", app_project_folder="C:\\src\\testapp",
        test_extension_id="e", fingerprint="fp", runner=runner,
        path_ops=_cycle_ops(tmp_path),
    )
    assert r["all_passed"] is True
    # Optional infra steps (symbol-harvest, sync-assets) may appear; the ORDERED
    # core cycle is the contract.
    core = [s["step"] for s in r["steps"] if s["step"] in ("sync", "compile", "publish", "run")]
    assert core == ["sync", "compile", "publish", "run"]
    assert r["cycle"] == "sync->compile->publish->run"


# --- symbol harvest: version-aware, poisoning-proof ---------------------------

def _touch_app(path, name: str, mtime: float) -> None:
    import os
    p = path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))


def test_harvest_refuses_same_version_churn(tmp_path):
    """Same identity+version with a NEWER mtime must NOT replace the cache entry —
    a stale-bytes copy with a fresh mtime poisoned the Foundation symbol live
    (feature 239584: every later compile lost the feature objects)."""
    share = tmp_path / "my"
    cache = tmp_path / "my" / "symbolcache" / "fp"
    _touch_app(cache, "Zig_Empire Foundation_28.2610.99999.0.app", 1000)
    _touch_app(share / "build" / "out", "Zig_Empire Foundation_28.2610.99999.0.app", 2000)
    out = al_runner.harvest_local_symbols(str(share), str(cache))
    assert out == []


def test_harvest_takes_strictly_higher_versions_and_new_identities(tmp_path):
    share = tmp_path / "my"
    cache = tmp_path / "my" / "symbolcache" / "fp"
    _touch_app(cache, "Zig_Empire Foundation_28.2610.11341.0.app", 3000)
    _touch_app(share / "localbuild", "Zig_Empire Foundation_28.2610.99999.0.app", 1000)  # older mtime, higher version
    _touch_app(share / "localbuild", "Zig_NewApp_1.0.0.0.app", 500)
    out = sorted(al_runner.harvest_local_symbols(str(share), str(cache)))
    assert out == ["Zig_Empire Foundation_28.2610.99999.0.app", "Zig_NewApp_1.0.0.0.app"]


def test_harvest_never_downgrades(tmp_path):
    share = tmp_path / "my"
    cache = tmp_path / "my" / "symbolcache" / "fp"
    _touch_app(cache, "Zig_Empire Foundation_28.2610.99999.0.app", 100)
    _touch_app(share / "localbuild", "Zig_Empire Foundation_28.2610.11341.0.app", 9999)
    assert al_runner.harvest_local_symbols(str(share), str(cache)) == []


def test_harvest_skips_other_worktrees_build_output(tmp_path):
    """Parallel worktrees build the same app names at the same versions — a
    sibling's build output must NEVER become this workspace's symbol (second
    poisoning observed live on feature 239584)."""
    share = tmp_path / "my"
    cache = share / "symbolcache" / "fp-aaaa"
    own_build = share / "build" / "aaaa"
    sibling_build = share / "build" / "bbbb"
    _touch_app(own_build / "out", "Zig_OwnApp_2.0.0.0.app", 100)
    _touch_app(sibling_build / "out", "Zig_Sibling_9.9.9.9.app", 200)
    out = al_runner.harvest_local_symbols(
        str(share), str(cache), own_build_dir=str(own_build))
    assert out == ["Zig_OwnApp_2.0.0.0.app"]


def test_workspace_key_and_cache_dir_are_worktree_scoped():
    k1 = al_runner.workspace_key(r"C:\Users\x\wt-240435\extensions\TestApp")
    k2 = al_runner.workspace_key(r"C:\Users\x\wt-267598\extensions\TestApp")
    assert k1 != k2 and len(k1) == 8
    # same worktree, different app => same key (one namespace per worktree)
    assert al_runner.workspace_key(r"C:\Users\x\wt-240435\extensions\BaseApp") == k1
    d = al_runner.symbol_cache_dir("acctest", "fp123", k1)
    assert d.endswith(f"fp123-{k1}")


def test_run_full_cycle_stops_at_compile_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")

    def runner(cmd):
        if cmd[0] == "robocopy":
            return _proc(1)
        return _proc(0, "error AL0118: unknown identifier 'Foo'\n")

    r = al_runner.run_full_cycle(
        container_name="acctest", app_project_folder="C:\\src\\testapp",
        test_extension_id="e", fingerprint="fp", runner=runner,
        path_ops=_cycle_ops(tmp_path),
    )
    assert r["all_passed"] is False and r["failed_step"] == "compile"
    # app-inventory always runs first (human rule 2026-07-04: never assume the
    # container is clean - report what is installed before touching it).
    assert [s["step"] for s in r["steps"]] == ["app-inventory", "sync", "compile"]
    compile_step = r["steps"][2]
    assert compile_step["errors"] == ["AL0118"]


def test_publish_rejection_gets_deterministic_verdict(tmp_path, monkeypatch):
    """'Newer version already installed' was buried in 4000 chars of stdout —
    the step must carry a named verdict + actionable hint (observed live:
    EmpireHousing 99999.0 vs installed 99999.3)."""
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")

    def runner(cmd):
        if cmd[0] == "robocopy":
            return _proc(1)
        joined = cmd[-1]
        if "Compile-AppInBcContainer" in joined:
            return _proc(0, "COMPILE_DONE: True")
        if "Publish-BcContainerApp" in joined:
            return _proc(1, "Cannot install the extension EmpireHousing by Zig 28.2610.99999.0 "
                            "because a newer version 28.2610.99999.3 was already installed.")
        return _proc(0, "")

    r = al_runner.run_full_cycle(
        container_name="acctest", app_project_folder="C:\\src\\EmpireHousing",
        test_extension_id="e", fingerprint="fp", runner=runner,
        path_ops=_cycle_ops(tmp_path), publish_only=True,
    )
    assert r["failed_step"] == "publish"
    pub = next(s for s in r["steps"] if s["step"] == "publish")
    assert pub["verdict"] == "newer-version-installed"
    assert "99999.3" in pub["reason"] and "drop app_project_folder" in pub["reason"]
    assert r["reason"] == pub["reason"]  # surfaced at the top level too


def test_run_full_cycle_stops_at_failed_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")
    r = al_runner.run_full_cycle(
        container_name="acctest", app_project_folder="C:\\src\\testapp",
        test_extension_id="e", fingerprint="fp",
        runner=lambda cmd: _proc(16, "", "robocopy fatal"),
        path_ops=_cycle_ops(tmp_path),
    )
    assert r["failed_step"] == "sync" and r["all_passed"] is False


def test_run_full_cycle_no_app_produced(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_TEST_PASSWORD", "pw")

    def runner(cmd):
        if cmd[0] == "robocopy":
            return _proc(0)
        return _proc(0, "COMPILE_DONE: True")

    r = al_runner.run_full_cycle(
        container_name="acctest", app_project_folder="C:\\src\\a",
        test_extension_id="e", fingerprint="fp", runner=runner,
        path_ops={"mkdir": lambda p: None, "newest_app": lambda f: None},
    )
    assert r["failed_step"] == "publish"
    assert any(s["step"] == "publish" and not s["ok"] for s in r["steps"])
