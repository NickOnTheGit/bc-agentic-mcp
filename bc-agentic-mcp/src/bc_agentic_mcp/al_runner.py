"""al_runner — deterministic bridge to run AL tests in a Business Central container.

The value here is the *pure* command construction and output parsing (fully testable and
reproducible). The actual process launch is a thin, injectable seam so the logic never
depends on a live container. Nothing is hardcoded: container name, app path, test id and
credentials are all inputs; credentials are read from the environment, never stored.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Any, Callable, Dict, List, Optional

# Parses lines like:
#   "Codeunit 50000 SubprocOnHoldRunTest Success (0.176 seconds)"
#   "  Testfunction AllFieldsPersist Success (0.06 seconds)"
_CODEUNIT_RE = re.compile(
    r"Codeunit\s+(?P<id>\d+)\s+(?P<name>.+?)\s+(?P<result>Success|Failure|Skipped)",
    re.IGNORECASE,
)
_TESTFUNC_RE = re.compile(
    r"Testfunction\s+(?P<name>.+?)\s+(?P<result>Success|Failure|Skipped)",
    re.IGNORECASE,
)

_RUNNER = Callable[[List[str]], "subprocess.CompletedProcess"]


def build_run_tests_command(
    *,
    container_name: str,
    test_extension_id: str,
    credential_env: str = "BC_TEST_PASSWORD",
    user: str = "admin",
    tenant: str = "default",
    test_codeunit: Optional[str] = None,
) -> List[str]:
    """Build a deterministic pwsh command to run a published test extension.

    The password is referenced via an environment variable *inside* the child process, so
    the secret never appears in the argument vector or in logs.
    """
    if not container_name or not test_extension_id:
        raise ValueError("container_name and test_extension_id are required")

    # Deterministic license recovery: prefer explicit override, then known helper/cache paths.
    license_candidates = [
        f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}\\my\\license.bclicense",
        f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}\\my\\license.flf",
        f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}\\my\\license.lic",
        "C:\\run\\my\\license.bclicense",
        "C:\\run\\my\\license.flf",
        "C:\\run\\my\\license.lic",
    ]
    license_candidates_ps = ",".join(f"'{p}'" for p in license_candidates)

    ps = (
        "$ErrorActionPreference='Stop';"
        "Import-Module BcContainerHelper -DisableNameChecking | Out-Null;"
        "$licenseCandidates=@();"
        "if($env:BC_LICENSE_FILE){$licenseCandidates += $env:BC_LICENSE_FILE};"
        f"$licenseCandidates += @({license_candidates_ps});"
        "$licenseFile=$licenseCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1;"
        "if($licenseFile){"
        "  try {"
        f"    Import-BcContainerLicense -containerName {shlex.quote(container_name)} -licenseFile $licenseFile | Out-Null;"
        "    Write-Output ('LICENSE_RECOVERY: imported ' + $licenseFile)"
        "  } catch {"
        "    Write-Output ('LICENSE_RECOVERY: import-failed ' + $_.Exception.Message)"
        "  }"
        "} else {"
        "  Write-Output 'LICENSE_RECOVERY: no-license-file-found'"
        "};"
        f"$p=ConvertTo-SecureString $env:{credential_env} -AsPlainText -Force;"
        f"$c=New-Object System.Management.Automation.PSCredential('{user}',$p);"
        "$ok=Run-TestsInBcContainer -containerName "
        f"{shlex.quote(container_name)} -credential $c -tenant {shlex.quote(tenant)} "
        f"-extensionId {shlex.quote(test_extension_id)} "
        + (f"-testCodeunit {shlex.quote(str(test_codeunit))} " if test_codeunit else "")
        + "-detailed -returnTrueIfAllPassed;"
        "Write-Output ('ALL_TESTS_PASSED: ' + $ok)"
    )
    return ["pwsh", "-NoProfile", "-Command", ps]


def parse_test_results(stdout: str) -> Dict[str, Any]:
    """Parse Run-TestsInBcContainer output into a structured, deterministic result."""
    codeunits: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        cu = _CODEUNIT_RE.search(line)
        if cu:
            current = {
                "id": int(cu.group("id")),
                "name": cu.group("name").strip(),
                "result": cu.group("result").lower(),
                "tests": [],
            }
            codeunits.append(current)
            continue
        tf = _TESTFUNC_RE.search(line)
        if tf and current is not None:
            current["tests"].append(
                {"name": tf.group("name").strip(), "result": tf.group("result").lower()}
            )
            continue
        # -detailed prints the error message + call stack as indented lines right
        # after a failing Testfunction. Attach them so a failure is diagnosable
        # from the tool result alone — no blind re-runs.
        if (
            line
            and current is not None
            and current["tests"]
            and current["tests"][-1]["result"] == "failure"
            and not line.startswith(("ALL_TESTS_PASSED", "LICENSE_RECOVERY"))
        ):
            detail = current["tests"][-1].setdefault("error_lines", [])
            if len(detail) < 12:
                detail.append(line)
    all_tests = [t for cu in codeunits for t in cu["tests"]]
    passed = sum(1 for t in all_tests if t["result"] == "success")
    failed = sum(1 for t in all_tests if t["result"] == "failure")
    marker = None
    m = re.search(r"ALL_TESTS_PASSED:\s*(True|False)", stdout or "", re.IGNORECASE)
    if m:
        marker = m.group(1).lower() == "true"
    license_recovery = None
    lm = re.findall(r"LICENSE_RECOVERY:\s*(.+)", stdout or "", re.IGNORECASE)
    if lm:
        license_recovery = lm[-1].strip()
    all_passed = (failed == 0 and len(all_tests) > 0) if marker is None else marker
    failures = [
        {"codeunit": cu["name"], "test": t["name"],
         "error": " | ".join(t.get("error_lines", []))}
        for cu in codeunits for t in cu["tests"] if t["result"] == "failure"
    ]
    return {
        "codeunits": codeunits,
        "total": len(all_tests),
        "passed": passed,
        "failed": failed,
        "all_passed": bool(all_passed),
        "failures": failures,
        "license_recovery": license_recovery,
    }


def _default_runner(cmd: List[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                          stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# A4: the sanctioned tool owns the FULL cycle (sync -> compile -> publish -> run)
# so the terminal bypass observed on WI 239597 is never the easier path again.
# A3: symbol cache — stop re-copying ~140 symbol apps on every compile.
# All builders are pure (fully testable); execution goes through the runner seam.
# ---------------------------------------------------------------------------

def shared_build_root(container_name: str) -> str:
    """The only sanctioned host<->container exchange folder (BcContainerHelper 'my')."""
    return f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}\\my\\build"


def workspace_key(app_project_folder: str) -> str:
    """Stable identity of the CALLING worktree (…/<wt>/extensions/<App> -> <wt>).

    Parallel sessions build the SAME app names (BaseApp/TestApp) at the SAME
    versions from different worktrees — sharing one build dir let a sibling's
    Foundation .app (same version, no feature objects) overwrite ours and poison
    every later compile (observed live twice on feature 239584). Each worktree
    gets its own build+symbol namespace.
    """
    import hashlib as _hashlib
    from pathlib import Path as _Path
    p = _Path(app_project_folder).resolve()
    root = p.parent.parent if p.parent.name.lower() == "extensions" else p.parent
    return _hashlib.sha1(str(root).lower().encode("utf-8")).hexdigest()[:8]


def symbol_cache_dir(container_name: str, fingerprint: str, workspace: str = "") -> str:
    """Per-image, per-WORKTREE symbol cache: new image => fresh copy (A3); a
    sibling worktree can never shadow this workspace's own symbols."""
    suffix = f"-{workspace}" if workspace else ""
    return (
        f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}"
        f"\\my\\symbolcache\\{fingerprint or 'unknown'}{suffix}"
    )


def use_symbol_cache(symbols_dir: str, *, list_dir=None) -> bool:
    """Deterministic A3 decision: reuse the cache only when it already holds symbols."""
    from pathlib import Path as _Path
    try:
        if list_dir is not None:
            entries = list_dir(symbols_dir)
        else:
            p = _Path(symbols_dir)
            entries = [child.name for child in p.iterdir()] if p.is_dir() else []
    except OSError:
        return False
    return any(str(e).lower().endswith(".app") for e in entries)


def build_sync_command(*, source_dir: str, target_dir: str) -> List[str]:
    """Mirror the test-app sources into the container-shared build folder.

    /MIR guarantees the container compiles EXACTLY what is in the workspace —
    the stale-copy compile failures observed live cannot happen again.
    """
    if not source_dir or not target_dir:
        raise ValueError("source_dir and target_dir are required")
    return [
        "robocopy", source_dir, target_dir, "/MIR",
        "/XD", ".git", ".vscode", ".alpackages", "/NFL", "/NDL", "/NJH", "/NJS",
    ]


def build_compile_command(
    *,
    container_name: str,
    project_folder: str,
    output_folder: str,
    symbols_folder: str,
    copy_symbols: bool,
    credential_env: str = "BC_TEST_PASSWORD",
    user: str = "admin",
    tenant: str = "default",
) -> List[str]:
    """Compile-AppInBcContainer with the A3 symbol-cache decision applied.

    NEVER pass ``-UpdateSymbols``: it forces an HTTP symbol download from the dev
    endpoint, which failed 401 on this stack while the filesystem copy of the very
    same symbols had ALREADY SUCCEEDED (observed live, install of PBI 240435).
    Cold cache => ``-CopySymbolsFromContainer`` (filesystem, no auth); warm cache
    => use the cached .app symbols as-is.
    """
    if not container_name or not project_folder:
        raise ValueError("container_name and project_folder are required")
    copy_flag = " -CopySymbolsFromContainer" if copy_symbols else ""
    ps = (
        "$ErrorActionPreference='Stop';"
        "Import-Module BcContainerHelper -DisableNameChecking | Out-Null;"
        f"$p=ConvertTo-SecureString $env:{credential_env} -AsPlainText -Force;"
        f"$c=New-Object System.Management.Automation.PSCredential('{user}',$p);"
        f"Compile-AppInBcContainer -containerName {shlex.quote(container_name)}"
        f" -tenant {shlex.quote(tenant)} -credential $c"
        f" -appProjectFolder '{project_folder}'"
        f" -appOutputFolder '{output_folder}'"
        f" -appSymbolsFolder '{symbols_folder}'"
        f"{copy_flag};"
        "Write-Output 'COMPILE_DONE: True'"
    )
    return ["pwsh", "-NoProfile", "-Command", ps]


def build_publish_command(
    *,
    container_name: str,
    app_file: str,
    credential_env: str = "BC_TEST_PASSWORD",
    user: str = "admin",
    tenant: str = "default",
) -> List[str]:
    """Publish via the PROVEN stable local mode: dev endpoint + tenant scope.

    (Default server publish produced AL1024 symbol cascades on this stack; the
    dev-endpoint tenant publish succeeded first try — encoded here as code, not prose.)
    """
    if not container_name or not app_file:
        raise ValueError("container_name and app_file are required")
    ps = (
        "$ErrorActionPreference='Stop';"
        "Import-Module BcContainerHelper -DisableNameChecking | Out-Null;"
        f"$p=ConvertTo-SecureString $env:{credential_env} -AsPlainText -Force;"
        f"$c=New-Object System.Management.Automation.PSCredential('{user}',$p);"
        f"Publish-BcContainerApp -containerName {shlex.quote(container_name)}"
        f" -appFile '{app_file}' -skipVerification -sync -install"
        f" -tenant {shlex.quote(tenant)} -scope Tenant -useDevEndpoint -credential $c;"
        "Write-Output 'PUBLISH_DONE: True'"
    )
    return ["pwsh", "-NoProfile", "-Command", ps]


_COMPILE_ERROR_RE = re.compile(r"error (AL\d+|ALC\d+)", re.IGNORECASE)


def build_reinstall_dependents_command(
    *,
    container_name: str,
    publisher: str = "Zig",
    tenant: str = "default",
    max_passes: int = 5,
) -> List[str]:
    """One idempotent step: reinstall published+Synced+NOT-installed apps.

    A dev-endpoint base publish (synchronize) UNINSTALLS the dependent chain and
    leaves it published+synced. Observed live three times in one day (wi267598,
    2026-07-04) — each time hand-recovered with a multi-pass Install loop in the
    terminal. That improvisation is now code: fixpoint passes resolve dependency
    order ('Object reference not set' from Install-BcContainerApp = dependency
    not installed YET, next pass fixes it). Apps whose dependencies are not in
    the container at all (e.g. a test app awaiting ITS base) legitimately remain
    and are reported, not failed on.
    """
    if not container_name:
        raise ValueError("container_name is required")
    ps = (
        "$ErrorActionPreference='Continue';"
        "Import-Module BcContainerHelper -DisableNameChecking | Out-Null;"
        f"$cn='{container_name}';$pub='{publisher}';$ten='{tenant}';"
        f"for($pass=1;$pass -le {int(max_passes)};$pass++){{"
        "  $todo=Get-BcContainerAppInfo -containerName $cn -tenantSpecificProperties 3>$null |"
        "    Where-Object { $_.Publisher -eq $pub -and $_.IsPublished -and (-not $_.IsInstalled) -and \"$($_.SyncState)\" -eq 'Synced' };"
        "  if(-not $todo){ break };"
        "  $before=($todo | Measure-Object).Count;"
        "  foreach($a in $todo){"
        "    try { Install-BcContainerApp -containerName $cn -appName $a.Name -appPublisher $a.Publisher -appVersion $a.Version -tenant $ten 3>$null *> $null } catch {}"
        "  };"
        "  $after=(Get-BcContainerAppInfo -containerName $cn -tenantSpecificProperties 3>$null |"
        "    Where-Object { $_.Publisher -eq $pub -and $_.IsPublished -and (-not $_.IsInstalled) -and \"$($_.SyncState)\" -eq 'Synced' } | Measure-Object).Count;"
        "  if($after -eq $before){ break }"
        "};"
        "$left=Get-BcContainerAppInfo -containerName $cn -tenantSpecificProperties 3>$null |"
        "  Where-Object { $_.Publisher -eq $pub -and $_.IsPublished -and (-not $_.IsInstalled) -and \"$($_.SyncState)\" -eq 'Synced' };"
        "Write-Output ('REINSTALL_REMAINING: ' + (($left | ForEach-Object { $_.Name }) -join ', '));"
        "Write-Output 'REINSTALL_DONE: True'"
    )
    return ["pwsh", "-NoProfile", "-Command", ps]


def parse_step_output(stdout: str, marker: str) -> Dict[str, Any]:
    """Deterministic step verdict: marker present and no AL error diagnostics."""
    text = stdout or ""
    errors = sorted({m.group(1).upper() for m in _COMPILE_ERROR_RE.finditer(text)})
    ok = (f"{marker}: True" in text) and not errors
    first_error_lines = [
        line.strip() for line in text.splitlines() if "error AL" in line
    ][:5]
    return {"ok": ok, "errors": errors, "first_error_lines": first_error_lines}


def container_mutex(container_name: str, timeout_s: int = 900):
    """CROSS-PROCESS container mutex (lesson #6, PBI 240435 install).

    The in-process asyncio lock cannot stop two SERVER PROCESSES from publishing
    concurrently — observed live as 'another service is currently modifying the
    state of extensions' when attempt N+1 collided with attempt N still running.
    O_EXCL lock file under the container share; stale locks (dead pid / older than
    timeout) are broken deliberately and loudly.
    """
    import contextlib
    import time as _time
    from pathlib import Path as _Path

    @contextlib.contextmanager
    def _ctx():
        lock_dir = _Path(shared_build_root(container_name))
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = lock_dir / "container.lock"
        deadline = _time.monotonic() + timeout_s
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}@{_time.time()}".encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    age = _time.time() - lock.stat().st_mtime
                except OSError:
                    continue  # lock vanished between open and stat — retry
                if age > timeout_s:
                    lock.unlink(missing_ok=True)  # stale (crashed holder) — break it
                    continue
                if _time.monotonic() > deadline:
                    raise TimeoutError(
                        f"container '{container_name}' is locked by another operation "
                        f"(lock age {age:.0f}s) — refusing to publish concurrently")
                _time.sleep(5)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    return _ctx()


def harvest_local_symbols(
    share_root: str, symbols_dir: str, path_ops=None, *, own_build_dir: Optional[str] = None
) -> List[str]:
    """Copy locally-published dependency .app files into the compile symbol cache.

    Container lesson (PBI 240435 install): symbols must NEVER be acquired via the
    authenticated dev endpoint (401s while the same bytes sit on disk). Local
    publishes leave their .app files under the container share — harvest per app
    identity (publisher_name, version stripped) into the cache.

    POISONING lesson (feature 239584, live): mtime is NOT truth. A stale-bytes
    Foundation symbol carrying the SAME version got a fresh mtime (copy) and
    SHADOWED the good cache entry — every later compile lost the feature objects.
    Rule: harvest only ADDS new identities or strictly HIGHER versions. Equal or
    lower versions never touch the cache — same-version freshness is owned by the
    compile itself (symbol-seed step copies its own output into the cache).

    WORKTREE lesson (same feature, second poisoning): parallel sessions build the
    same apps at the same versions — build outputs of OTHER worktrees are never
    harvested (``own_build_dir`` marks ours; everything else under build/ is skipped).
    """
    from pathlib import Path as _Path
    import re as _re
    import shutil as _shutil

    share, cache = _Path(share_root), _Path(symbols_dir)
    if not share.exists():
        return []
    cache.mkdir(parents=True, exist_ok=True)
    build_base = share / "build"
    own_build = _Path(own_build_dir).resolve() if own_build_dir else None

    def identity(name: str) -> str:
        # Publisher_App Name_1.2.3.4.app -> publisher_app name
        return _re.sub(r"_[\d.]+\.app$", "", name, flags=_re.IGNORECASE).lower()

    def version_of(name: str) -> tuple:
        m = _re.search(r"_([\d.]+)\.app$", name, flags=_re.IGNORECASE)
        if not m:
            return (0,)
        try:
            return tuple(int(x) for x in m.group(1).split(".") if x != "")
        except ValueError:
            return (0,)

    cache_by_identity: Dict[str, "_Path"] = {}
    for p in cache.glob("*.app"):
        key = identity(p.name)
        cur = cache_by_identity.get(key)
        if cur is None or version_of(p.name) > version_of(cur.name):
            cache_by_identity[key] = p
    newest: Dict[str, "_Path"] = {}
    for p in share.rglob("*.app"):
        if cache in p.parents:
            continue  # never re-harvest the cache itself
        if str(p.parent).lower().find("symbolcache") >= 0:
            continue  # other worktrees' caches are not sources either
        if build_base in p.parents:
            if own_build is None or own_build not in p.resolve().parents:
                continue  # NEVER harvest another worktree's build output
        key = identity(p.name)
        cached = cache_by_identity.get(key)
        if cached is not None and version_of(p.name) <= version_of(cached.name):
            continue  # never downgrade, never same-version churn
        cur = newest.get(key)
        if cur is None or version_of(p.name) > version_of(cur.name) or (
            version_of(p.name) == version_of(cur.name) and p.stat().st_mtime > cur.stat().st_mtime
        ):
            newest[key] = p
    out: List[str] = []
    for key, p in newest.items():
        try:
            stale = cache_by_identity.get(key)
            if stale is not None and stale.name != p.name:
                stale.unlink(missing_ok=True)  # never leave two versions in the cache
            _shutil.copyfile(p, cache / p.name)
            out.append(p.name)
        except OSError:
            continue
    return sorted(out)


def run_full_cycle(
    *,
    container_name: str,
    app_project_folder: str,
    test_extension_id: str,
    fingerprint: str = "unknown",
    credential_env: str = "BC_TEST_PASSWORD",
    user: str = "admin",
    tenant: str = "default",
    runner: Optional[_RUNNER] = None,
    path_ops=None,
    test_codeunit: Optional[str] = None,
    publish_only: bool = False,
) -> Dict[str, Any]:
    """A4: sync -> compile (A3 cached symbols) -> publish (dev endpoint) -> run.

    ``publish_only`` stops after publish — used for DEPENDENCY apps in a multi-app
    install (publish BaseApp first, then the test app; tests run once at the end).
    Stops at the first failed step and names it — no cascading noise. ``runner``
    and ``path_ops`` are injectable seams for tests; nothing is hardcoded.
    """
    if os.environ.get(credential_env) is None and runner is None:
        return {
            "executed": False,
            "reason": f"credential env var {credential_env} is not set",
            "all_passed": False,
        }
    run = runner or _default_runner
    from pathlib import Path as _Path
    ops = path_ops or {
        "mkdir": lambda p: _Path(p).mkdir(parents=True, exist_ok=True),
        "newest_app": lambda folder: max(
            (str(p) for p in _Path(folder).glob("*.app")),
            key=lambda p: (_Path(p).stat().st_mtime, p),  # name tiebreak: mtime ties must not pick by fs order
            default=None,
        ),
    }

    project_name = app_project_folder.replace("\\", "/").rstrip("/").split("/")[-1]
    ws_key = workspace_key(app_project_folder)
    build_root = f"{shared_build_root(container_name)}\\{ws_key}"
    target_dir = f"{build_root}\\{project_name}"
    output_dir = f"{build_root}\\out"
    symbols_dir = symbol_cache_dir(container_name, fingerprint, ws_key)
    for d in (target_dir, output_dir, symbols_dir):
        ops["mkdir"](d)

    steps: List[Dict[str, Any]] = []

    # LIVE PROGRESS: each step announces itself in a small json under the container
    # share, so drivers/UIs can show WHAT is running instead of apparent silence
    # (a 10-minute compile looked frozen to the human — observed live).
    import time as _time
    _cycle_t0 = _time.monotonic()

    def _progress(step_name: str) -> None:
        try:
            import json as _json
            p = _Path(build_root) / "progress.json"
            p.write_text(_json.dumps({
                "container": container_name,
                "app": project_name,
                "step": step_name,
                "elapsed_s": round(_time.monotonic() - _cycle_t0, 1),
            }), encoding="utf-8")
        except OSError:
            pass

    def _persist_failure_log(step_name: str, proc) -> Optional[str]:
        """Container lesson: a failed step must leave its FULL stdout+stderr on disk —
        truncated result payloads hid the real compile error (observed live)."""
        try:
            from datetime import datetime, timezone
            log_dir = _Path(build_root) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log = log_dir / f"{step_name}-{stamp}.log"
            log.write_text(
                (getattr(proc, "stdout", "") or "")
                + "\n===== STDERR =====\n"
                + (getattr(proc, "stderr", "") or ""),
                encoding="utf-8", errors="replace")
            return str(log)
        except OSError:
            return None

    # Step 0: container app inventory (human rule 2026-07-04: NEVER assume the
    # container is clean — another implementation may already be installed there,
    # e.g. a parallel lane published the same app name/version from a different
    # SHA). Best-effort and never blocking: the report lands in the step payload
    # so the caller SEES what is on the container before we touch it.
    _progress("app-inventory")
    inventory: List[Dict[str, Any]] = []
    try:
        inv_proc = run([
            "powershell", "-NoProfile", "-Command",
            "Import-Module BcContainerHelper -DisableNameChecking 3>$null; "
            f"Get-BcContainerAppInfo -containerName {container_name} -tenantSpecificProperties "
            "| Select-Object Name, Version, IsInstalled | ConvertTo-Json -Compress",
        ])
        import json as _inv_json
        raw = (getattr(inv_proc, "stdout", "") or "").strip()
        if raw:
            parsed = _inv_json.loads(raw)
            entries = parsed if isinstance(parsed, list) else [parsed]
            inventory = [{"name": str(e.get("Name", "")),
                          "version": str(e.get("Version", "")),
                          "installed": bool(e.get("IsInstalled"))}
                         for e in entries]
    except Exception:
        inventory = []  # inventory is a truth report, never a blocker
    steps.append({"step": "app-inventory", "ok": True, "apps": len(inventory),
                  "installed": [e for e in inventory if e.get("installed")][:20]})

    # Step 1: sync (robocopy exit codes 0-7 mean success).
    _progress("sync")
    proc = run(build_sync_command(source_dir=app_project_folder, target_dir=target_dir))
    sync_ok = (getattr(proc, "returncode", 8) or 0) <= 7
    steps.append({"step": "sync", "ok": sync_ok, "exit_code": getattr(proc, "returncode", None)})
    if not sync_ok:
        return {"executed": True, "all_passed": False, "failed_step": "sync", "steps": steps,
                "log_file": _persist_failure_log("sync", proc),
                "stdout": (getattr(proc, "stdout", "") or "")[-2000:]}

    # Step 1b: app.json may reference assets OUTSIDE the app folder (observed live:
    # BaseApp logo '../Zig365 Operations/deonline-logo.jpg' -> AL1001 in the synced
    # copy). Resolve such relative references against the source tree and copy them
    # to the same relative location under the build root. Filesystem-only.
    try:
        import json as _json
        import shutil as _shutil
        app_json = _Path(app_project_folder) / "app.json"
        if app_json.exists():
            meta = _json.loads(app_json.read_text(encoding="utf-8-sig"))
            logo = str(meta.get("logo", "") or "")
            if logo.startswith(".."):
                src_asset = (_Path(app_project_folder) / logo).resolve()
                dst_asset = (_Path(target_dir) / logo).resolve()
                if src_asset.exists():
                    dst_asset.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copyfile(src_asset, dst_asset)
                    steps.append({"step": "sync-assets", "ok": True,
                                  "copied": logo})
    except (OSError, ValueError):
        steps.append({"step": "sync-assets", "ok": False})

    # Step 2: compile with the symbol-cache decision (A3).
    _progress("compile")
    copy_symbols = not use_symbol_cache(symbols_dir)
    # Harvest locally-published dependency symbols FIRST (filesystem-only; the
    # compile must never fall back to the authenticated dev-endpoint download).
    # Scan the whole container share ('my') but never another worktree's build.
    _share_parent = str(_Path(shared_build_root(container_name)).parent)
    harvested = harvest_local_symbols(_share_parent, symbols_dir, own_build_dir=build_root)
    if harvested:
        steps.append({"step": "symbol-harvest", "ok": True, "harvested": harvested[:12]})
    proc = run(build_compile_command(
        container_name=container_name, project_folder=target_dir,
        output_folder=output_dir, symbols_folder=symbols_dir, copy_symbols=copy_symbols,
        credential_env=credential_env, user=user, tenant=tenant,
    ))
    verdict = parse_step_output(getattr(proc, "stdout", "") or "", "COMPILE_DONE")
    steps.append({"step": "compile", "ok": verdict["ok"], "symbol_cache_used": not copy_symbols,
                  "errors": verdict["errors"], "first_error_lines": verdict["first_error_lines"]})
    if not verdict["ok"]:
        return {"executed": True, "all_passed": False, "failed_step": "compile", "steps": steps,
                "log_file": _persist_failure_log("compile", proc),
                "stderr_tail": (getattr(proc, "stderr", "") or "")[-1500:],
                "stdout": (getattr(proc, "stdout", "") or "")[-4000:]}

    # Step 2b: SEED the symbol cache with the app we just compiled — the freshest
    # compile is the only authority for its own identity+version. This repairs any
    # same-version poisoning deterministically on every green compile (the harvest
    # above refuses same-version churn by design).
    try:
        import shutil as _shutil2
        fresh_app = ops["newest_app"](output_dir)
        if fresh_app:
            _shutil2.copy2(fresh_app, str(_Path(symbols_dir) / _Path(fresh_app).name))
            steps.append({"step": "symbol-seed", "ok": True, "seeded": _Path(fresh_app).name})
    except OSError:
        steps.append({"step": "symbol-seed", "ok": False})

    # Step 3: publish the newest compiled app via the proven dev-endpoint mode.
    # 3a: restore the dependent chain FIRST — a sibling base publish may have left
    # this app's dependencies published-but-uninstalled; publishing would then fail
    # the dependency check (encoded from the wi267598 livelock recovery).
    _progress("reinstall-dependents")
    proc = run(build_reinstall_dependents_command(container_name=container_name, tenant=tenant))
    pre_verdict = parse_step_output(getattr(proc, "stdout", "") or "", "REINSTALL_DONE")
    remaining = re.search(r"REINSTALL_REMAINING: (.*)", getattr(proc, "stdout", "") or "")
    steps.append({"step": "reinstall-dependents", "ok": pre_verdict["ok"],
                  "remaining": (remaining.group(1).strip() if remaining else "")})
    _progress("publish")
    app_file = ops["newest_app"](output_dir)
    if not app_file:
        steps.append({"step": "publish", "ok": False, "reason": "no .app produced by compile"})
        return {"executed": True, "all_passed": False, "failed_step": "publish", "steps": steps}
    proc = run(build_publish_command(
        container_name=container_name, app_file=app_file,
        credential_env=credential_env, user=user, tenant=tenant,
    ))
    verdict = parse_step_output(getattr(proc, "stdout", "") or "", "PUBLISH_DONE")
    publish_step: Dict[str, Any] = {"step": "publish", "ok": verdict["ok"], "app_file": app_file,
                                    "errors": verdict["errors"]}
    if not verdict["ok"]:
        # Deterministic verdicts for the KNOWN publish rejections — the raw
        # message was buried in 4000 chars of stdout (observed live: 'Cannot
        # install ... because a newer version 28.2610.99999.3 was already
        # installed' cost a manual stdout dig).
        combined = ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or ""))
        newer = re.search(
            r"Cannot install the extension (?P<app>.+?) by (?P<pub>\S+) (?P<ver>[\d.]+) "
            r"because a newer version (?P<installed>[\d.]+) was already installed",
            combined)
        if newer:
            publish_step["verdict"] = "newer-version-installed"
            publish_step["reason"] = (
                f"{newer.group('app')} {newer.group('ver')} rejected: the container already runs "
                f"the NEWER {newer.group('installed')}. Either run tests against the installed "
                "build (drop app_project_folder) or bump the app version above it."
            )
        elif re.search(r"401|unauthorized|credential", combined, re.IGNORECASE):
            publish_step["verdict"] = "auth"
            publish_step["reason"] = "dev-endpoint rejected the credentials — re-run bc_env_preflight."
    steps.append(publish_step)
    if not verdict["ok"]:
        return {"executed": True, "all_passed": False, "failed_step": "publish", "steps": steps,
                **({"reason": publish_step["reason"]} if publish_step.get("reason") else {}),
                "stdout": (getattr(proc, "stdout", "") or "")[-4000:]}

    if publish_only:
        # 3b: the publish just knocked out ITS dependents (synchronize uninstalls
        # the chain) — heal them in the same window instead of leaving the manual
        # multi-pass Install dance to the caller.
        _progress("reinstall-dependents (post-publish)")
        proc = run(build_reinstall_dependents_command(container_name=container_name, tenant=tenant))
        post_verdict = parse_step_output(getattr(proc, "stdout", "") or "", "REINSTALL_DONE")
        remaining = re.search(r"REINSTALL_REMAINING: (.*)", getattr(proc, "stdout", "") or "")
        steps.append({"step": "reinstall-dependents-post", "ok": post_verdict["ok"],
                      "remaining": (remaining.group(1).strip() if remaining else "")})
        _progress("done (publish_only)")
        return {"executed": True, "all_passed": None, "publish_only": True,
                "steps": steps, "cycle": "sync->compile->publish->reinstall"}

    # Step 4: run the tests (existing path; slice-aware).
    _progress("run-tests")
    result = run_container_tests(
        container_name=container_name, test_extension_id=test_extension_id,
        credential_env=credential_env, user=user, tenant=tenant, runner=runner,
        test_codeunit=test_codeunit,
    )
    steps.append({"step": "run", "ok": bool(result.get("all_passed")),
                  "passed": result.get("passed"), "total": result.get("total")})
    _progress("done")
    result["steps"] = steps
    result["cycle"] = "sync->compile->publish->run"
    return result


def run_container_tests(
    *,
    container_name: str,
    test_extension_id: str,
    credential_env: str = "BC_TEST_PASSWORD",
    user: str = "admin",
    tenant: str = "default",
    runner: Optional[_RUNNER] = None,
    test_codeunit: Optional[str] = None,
) -> Dict[str, Any]:
    """Build + execute the run command and parse results. ``runner`` is injectable.

    ``test_codeunit`` runs ONE test codeunit (a feature slice) instead of the whole
    extension — the feature model is: install once, then test slice by slice.
    """
    if os.environ.get(credential_env) is None and runner is None:
        return {
            "executed": False,
            "reason": f"credential env var {credential_env} is not set",
            "all_passed": False,
        }
    cmd = build_run_tests_command(
        container_name=container_name,
        test_extension_id=test_extension_id,
        credential_env=credential_env,
        user=user,
        tenant=tenant,
        test_codeunit=test_codeunit,
    )
    run = runner or _default_runner
    proc = run(cmd)
    stdout = getattr(proc, "stdout", "") or ""
    parsed = parse_test_results(stdout)
    parsed["executed"] = True
    parsed["exit_code"] = getattr(proc, "returncode", None)
    return parsed
