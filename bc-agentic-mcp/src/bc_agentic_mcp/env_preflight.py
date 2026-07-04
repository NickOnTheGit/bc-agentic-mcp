"""env_preflight — deterministic environment truth BEFORE container work (A1).

Observed failure classes this kills (WI 239597 session): license lost after a DB
restore discovered via ~10 exploratory calls; AL1024 dependency-symbol cascades
across ~8 publish attempts; publish mode (dev-endpoint vs default) discovered by
trial-and-error; stale shared-folder copies compiling old code.

One 30-second preflight checks all of it and caches the result as a
container-scoped manifest at ``.specs/.env/<container>.json`` (container truth is
per-container, not per-item — one preflight serves every item on that container).
``bc_run_tests`` refuses to run without a fresh, passing manifest.

Design rules: pure decision logic; every I/O (docker, filesystem, HTTP) behind an
injectable seam; secrets read from env inside probes and never stored in the
manifest; fail-closed (an unreadable manifest is a stale manifest).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import error as _urlerror
from urllib import request as _urlrequest

from bc_agentic_mcp.workspace import specs_root

DEFAULT_TTL_SECONDS = 1800  # 30 minutes: containers are stable within a session
DEFAULT_DEV_PORT = 7049
PUBLISH_MODE = "dev-endpoint-tenant"  # the mode proven stable on this stack
# Container-user candidates, most likely first. The USER is environment truth:
# acctest was created with 'devadmin' while every default said 'admin' — the
# resulting 401s cost an install attempt (observed live, PBI 240435).
USER_CANDIDATES = ("devadmin", "admin")

_RUNNER = Callable[[List[str]], "subprocess.CompletedProcess"]
_FETCHER = Callable[[str, Dict[str, str]], "tuple[int, str]"]


def _default_runner(cmd: List[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)


def _default_fetcher(url: str, headers: Dict[str, str]):
    req = _urlrequest.Request(url, headers=headers, method="GET")
    try:
        with _urlrequest.urlopen(req, timeout=30) as resp:  # noqa: S310 (built URL)
            return resp.status, ""
    except _urlerror.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # URLError, timeout, ...
        return 0, str(exc)


# ---------------------------------------------------------------------------
# Individual checks (each returns {ok, ...} and never raises)
# ---------------------------------------------------------------------------

def license_candidates(container_name: str) -> List[str]:
    """Deterministic license search order (same list the runner recovery uses)."""
    base = f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}\\my"
    return [
        f"{base}\\license.bclicense",
        f"{base}\\license.flf",
        f"{base}\\license.lic",
        "C:\\run\\my\\license.bclicense",
        "C:\\run\\my\\license.flf",
        "C:\\run\\my\\license.lic",
    ]


def check_license(
    container_name: str,
    *,
    path_exists: Callable[[str], bool] = lambda p: Path(p).exists(),
) -> Dict[str, Any]:
    """Find the first present license file; explicit override env wins."""
    override = os.environ.get("BC_LICENSE_FILE")
    candidates = ([override] if override else []) + license_candidates(container_name)
    for candidate in candidates:
        if candidate and path_exists(candidate):
            return {"ok": True, "path": candidate}
    return {
        "ok": False,
        "path": None,
        "blocker": (
            "No license file found. Checked (in order): " + "; ".join(candidates)
            + ". Place the license at the first path or set BC_LICENSE_FILE."
        ),
        "checked": candidates,
    }


def check_container_health(
    container_name: str, *, runner: Optional[_RUNNER] = None
) -> Dict[str, Any]:
    """Container must be running (and healthy when a healthcheck exists)."""
    run = runner or _default_runner
    try:
        proc = run([
            "docker", "inspect", "-f",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container_name,
        ])
    except Exception as exc:
        return {"ok": False, "status": "docker-unavailable", "blocker": str(exc)}
    if getattr(proc, "returncode", 1) != 0:
        return {
            "ok": False,
            "status": "not-found",
            "blocker": f"Container '{container_name}' not found: {(getattr(proc, 'stderr', '') or '').strip()[:200]}",
        }
    raw = (getattr(proc, "stdout", "") or "").strip()
    state, _, health = raw.partition("|")
    ok = state == "running" and health in ("", "healthy")
    result: Dict[str, Any] = {"ok": ok, "status": health or state}
    if not ok:
        result["blocker"] = (
            f"Container '{container_name}' is {state}"
            + (f" (health: {health})" if health else "")
            + ". Start/heal the container before running tests."
        )
    return result


def check_shared_folder(
    container_name: str,
    *,
    path_exists: Callable[[str], bool] = lambda p: Path(p).exists(),
) -> Dict[str, Any]:
    """The BcContainerHelper 'my' folder is the only sanctioned host<->container path."""
    shared = f"C:\\ProgramData\\BcContainerHelper\\Extensions\\{container_name}\\my"
    if path_exists(shared):
        return {"ok": True, "path": shared}
    return {
        "ok": False,
        "path": shared,
        "blocker": f"Shared folder not found: {shared} — was the container created with BcContainerHelper?",
    }


def get_container_fingerprint(
    container_name: str, *, runner: Optional[_RUNNER] = None
) -> str:
    """Image identity of the container; a changed image invalidates caches."""
    run = runner or _default_runner
    try:
        proc = run(["docker", "inspect", "-f", "{{.Image}}", container_name])
        if getattr(proc, "returncode", 1) == 0:
            return (getattr(proc, "stdout", "") or "").strip().replace("sha256:", "")[:16]
    except Exception:
        pass
    return "unknown"


def get_container_ip(
    container_name: str, *, runner: Optional[_RUNNER] = None
) -> Optional[str]:
    run = runner or _default_runner
    try:
        proc = run([
            "docker", "inspect", "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container_name,
        ])
        if getattr(proc, "returncode", 1) == 0:
            ip = (getattr(proc, "stdout", "") or "").strip()
            return ip or None
    except Exception:
        pass
    return None


def probe_dependency_symbols(
    dependencies: List[Dict[str, Any]],
    *,
    base_url: str,
    tenant: str = "default",
    user: str = "admin",
    credential_env: str = "BC_TEST_PASSWORD",
    fetcher: Optional[_FETCHER] = None,
) -> Dict[str, Any]:
    """Probe /dev/packages for every app.json dependency (kills AL1024 by-surprise).

    Classification: 200 -> ok, 404 -> missing, 401/403 -> auth, else unreachable.
    Auth uses Basic(user:password-from-env); the password never enters the result.
    """
    fetch = fetcher or _default_fetcher
    password = os.environ.get(credential_env, "")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}"}
    results: List[Dict[str, Any]] = []
    missing: List[str] = []
    problems: List[str] = []
    for dep in dependencies or []:
        app_id = str(dep.get("id", "")).strip()
        name = str(dep.get("name", app_id))
        version = str(dep.get("version", "0.0.0.0"))
        if not app_id:
            continue
        url = f"{base_url.rstrip('/')}/dev/packages?appId={app_id}&versionText={version}&tenant={tenant}"
        status, err = fetch(url, headers)
        if status == 200:
            state = "ok"
        elif status == 404:
            state = "missing"
            missing.append(f"{name} {version}")
        elif status in (401, 403):
            state = "auth"
            problems.append(f"{name}: auth failed ({status}) — check {credential_env}/user")
        else:
            state = "unreachable"
            problems.append(f"{name}: unreachable ({status or err})")
        results.append({"name": name, "version": version, "state": state})
    ok = not missing and not problems
    out: Dict[str, Any] = {"ok": ok, "results": results, "missing": missing}
    if missing:
        out["blocker"] = (
            "Dependency symbols NOT available in the tenant (would fail publish with AL1024): "
            + ", ".join(missing)
            + ". Publish the missing dependency chain first."
        )
    elif problems:
        out["blocker"] = "; ".join(problems)
    return out


def probe_container_user(
    *,
    base_url: str,
    tenant: str = "default",
    credential_env: str = "BC_TEST_PASSWORD",
    candidates: "tuple[str, ...]" = USER_CANDIDATES,
    fetcher: Optional[_FETCHER] = None,
) -> Dict[str, Any]:
    """Determine WHICH user authenticates against the dev endpoint (fail-closed).

    Basic-auth GET per candidate; 401/403 = wrong user, anything else (200/404/…)
    means auth was ACCEPTED (404 just means the probed package id is unknown).
    """
    import base64 as _b64

    password = os.environ.get(credential_env, "")
    if not password:
        return {"ok": False, "blocker": f"{credential_env} not set — cannot probe the container user"}
    fetch = fetcher or _default_fetcher
    url = f"{base_url.rstrip('/')}/dev/packages?appId=probe&versionText=1.0.0.0&tenant={tenant}"
    tried = []
    for user in candidates:
        token = _b64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        status, err = fetch(url, {"Authorization": f"Basic {token}"})
        tried.append(f"{user}:{status or err}")
        if status and status not in (401, 403):
            return {"ok": True, "user": user, "probed": tried}
    return {
        "ok": False,
        "user": None,
        "probed": tried,
        "blocker": ("No candidate user authenticated against the dev endpoint ("
                    + ", ".join(tried) + f") — check {credential_env} and the container's auth setup"),
    }


# ---------------------------------------------------------------------------
# Manifest: build / save / load / freshness
# ---------------------------------------------------------------------------

def manifest_path(project_root: Path, container_name: str) -> Path:
    return specs_root(Path(project_root).resolve()) / ".env" / f"{container_name}.json"


def build_manifest(
    container_name: str,
    *,
    fingerprint: str,
    checks: Dict[str, Dict[str, Any]],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    container_user: Optional[str] = None,
) -> Dict[str, Any]:
    blockers = [
        f"{name}: {check['blocker']}"
        for name, check in checks.items()
        if not check.get("ok") and check.get("blocker")
    ]
    return {
        "container_name": container_name,
        "fingerprint": fingerprint,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": ttl_seconds,
        "publish_mode": PUBLISH_MODE,
        "container_user": container_user,
        "checks": checks,
        "blockers": blockers,
        "ok": not blockers,
    }


def save_manifest(project_root: Path, manifest: Dict[str, Any]) -> str:
    path = manifest_path(project_root, manifest["container_name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(path)


def load_manifest(project_root: Path, container_name: str) -> Optional[Dict[str, Any]]:
    path = manifest_path(project_root, container_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # unreadable == stale (fail-closed)


def is_fresh(
    manifest: Optional[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    current_fingerprint: Optional[str] = None,
) -> bool:
    """Fresh = exists, within TTL, and (when known) same container image."""
    if not manifest:
        return False
    try:
        generated = datetime.fromisoformat(str(manifest.get("generated_at")))
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    age = (now - generated).total_seconds()
    if age < 0 or age > int(manifest.get("ttl_seconds", DEFAULT_TTL_SECONDS)):
        return False
    if current_fingerprint and manifest.get("fingerprint") not in ("unknown", current_fingerprint):
        return False
    return True


def try_refresh(
    project_root: Path,
    manifest: Dict[str, Any],
    *,
    runner: Optional[_RUNNER] = None,
) -> Optional[Dict[str, Any]]:
    """Self-heal an EXPIRED but previously PASSING manifest.

    The TTL guards against container drift — and drift is detectable in seconds:
    same image fingerprint + healthy state means the expensive probes (license,
    symbols, user) still hold. Only that exact case earns a re-stamp; a changed
    or unhealthy container still demands the full bc_env_preflight.

    Observed live: the 30-minute TTL expired three times INSIDE one feature's
    test sweep, blocking runs whose container had not changed at all.
    """
    if not manifest.get("ok"):
        return None
    container_name = str(manifest.get("container_name") or "")
    if not container_name:
        return None
    health = check_container_health(container_name, runner=runner)
    if not health.get("ok"):
        return None
    fingerprint = get_container_fingerprint(container_name, runner=runner)
    if manifest.get("fingerprint") not in ("unknown", fingerprint):
        return None
    refreshed = dict(manifest)
    refreshed["generated_at"] = datetime.now(timezone.utc).isoformat()
    refreshed["refreshed_by"] = "ttl-self-heal"
    save_manifest(project_root, refreshed)
    return refreshed


def require_fresh(
    project_root: Path,
    container_name: str,
    *,
    runner: Optional[_RUNNER] = None,
) -> Dict[str, Any]:
    """The gate consumed by container-touching tools.

    Returns {ok} when a fresh passing manifest exists; otherwise a structured
    blocker with the exact next action (never an exploratory hunt).
    """
    manifest = load_manifest(project_root, container_name)
    if manifest is None:
        return {
            "ok": False,
            "reason": (
                f"No environment preflight recorded for container '{container_name}'. "
                "Run bc_env_preflight first — it verifies health, license, shared folder "
                "and dependency symbols in one pass."
            ),
            "next_action": {
                "tool": "bc_env_preflight",
                "params_hint": {"container_name": container_name},
            },
        }
    if not is_fresh(manifest):
        healed = try_refresh(project_root, manifest, runner=runner)
        if healed is not None:
            return {"ok": True, "manifest": healed, "refreshed": True}
        return {
            "ok": False,
            "reason": (
                f"Environment preflight for '{container_name}' is stale "
                f"(generated {manifest.get('generated_at')}, ttl {manifest.get('ttl_seconds')}s) "
                "and the container could not be re-verified (unhealthy or image changed). "
                "Re-run bc_env_preflight."
            ),
            "next_action": {
                "tool": "bc_env_preflight",
                "params_hint": {"container_name": container_name},
            },
        }
    if not manifest.get("ok"):
        return {
            "ok": False,
            "reason": "Environment preflight FAILED — fix the blockers before container work: "
                      + "; ".join(manifest.get("blockers", [])),
            "blockers": manifest.get("blockers", []),
            "next_action": {
                "tool": "bc_env_preflight",
                "params_hint": {"container_name": container_name},
            },
        }
    return {"ok": True, "manifest": manifest}
