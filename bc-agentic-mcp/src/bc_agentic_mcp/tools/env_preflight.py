"""bc_env_preflight — one deterministic pass over container-environment truth (A1).

Checks: container health, license presence at the deterministic candidate paths,
shared-folder mapping, and (optionally, when an app.json is supplied) a
/dev/packages probe for every dependency so AL1024 can never strike mid-publish.
The result is cached as a container-scoped manifest that gates bc_run_tests.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from bc_agentic_mcp import env_preflight


async def handle_env_preflight(
    project_root: str,
    container_name: str,
    tenant: str = "default",
    user: str = "admin",
    credential_env: str = "BC_TEST_PASSWORD",
    app_json_path: Optional[str] = None,
    dev_port: int = env_preflight.DEFAULT_DEV_PORT,
    server_instance: str = "BC",
    ttl_seconds: int = env_preflight.DEFAULT_TTL_SECONDS,
) -> Dict[str, Any]:
    """Run the environment preflight and cache the manifest.

    ``app_json_path`` (host path to the app/test-app manifest) enables the
    dependency-symbol probe; without it that check is skipped, not failed.
    """
    root = Path(project_root).resolve()

    checks: Dict[str, Dict[str, Any]] = {}
    checks["container"] = env_preflight.check_container_health(container_name)
    checks["license"] = env_preflight.check_license(container_name)
    checks["shared_folder"] = env_preflight.check_shared_folder(container_name)

    fingerprint = env_preflight.get_container_fingerprint(container_name)

    # Container-USER probe: which account actually authenticates (devadmin vs admin).
    # ADVISORY: a wrong default surfaces as a clear 401 at publish; an unprobeable
    # user must not block an otherwise healthy environment.
    container_user: Optional[str] = None
    if checks["container"]["ok"]:
        ip_for_auth = env_preflight.get_container_ip(container_name)
        if ip_for_auth:
            user_probe = env_preflight.probe_container_user(
                base_url=f"http://{ip_for_auth}:{dev_port}/{server_instance}",
                tenant=tenant, credential_env=credential_env,
            )
            container_user = user_probe.get("user")
            checks["container_user"] = {
                "ok": True,  # advisory — never a preflight blocker
                "resolved": bool(container_user),
                "user": container_user,
                "probed": user_probe.get("probed", []),
                "note": None if container_user else user_probe.get("blocker"),
            }

    # Dependency-symbol probe: only meaningful when the container is reachable and an
    # app.json was provided; a skipped probe is recorded as such (not a silent pass).
    if app_json_path and checks["container"]["ok"]:
        ip = env_preflight.get_container_ip(container_name)
        if not ip:
            checks["dependency_symbols"] = {
                "ok": False,
                "blocker": "Could not resolve the container IP for the /dev/packages probe.",
            }
        else:
            try:
                app_json = json.loads(Path(app_json_path).read_text(encoding="utf-8-sig"))
                deps = list(app_json.get("dependencies", []) or [])
            except (OSError, json.JSONDecodeError) as exc:
                deps = None
                checks["dependency_symbols"] = {
                    "ok": False,
                    "blocker": f"app.json unreadable at {app_json_path}: {exc}",
                }
            if deps is not None:
                checks["dependency_symbols"] = env_preflight.probe_dependency_symbols(
                    deps,
                    base_url=f"http://{ip}:{dev_port}/{server_instance}",
                    tenant=tenant,
                    user=user,
                    credential_env=credential_env,
                )
    elif app_json_path:
        checks["dependency_symbols"] = {
            "ok": False,
            "blocker": "Container is not healthy — symbol probe not attempted.",
        }
    else:
        checks["dependency_symbols"] = {"ok": True, "skipped": True,
                                        "note": "no app_json_path supplied — probe skipped"}

    manifest = env_preflight.build_manifest(
        container_name, fingerprint=fingerprint, checks=checks, ttl_seconds=ttl_seconds,
        container_user=container_user,
    )
    manifest_file = env_preflight.save_manifest(root, manifest)

    return {
        "status": "env_ok" if manifest["ok"] else "env_blocked",
        "ok": manifest["ok"],
        "blocked": not manifest["ok"],
        "blockers": manifest["blockers"],
        "publish_mode": manifest["publish_mode"],
        "container_user": container_user,
        "fingerprint": fingerprint,
        "manifest_path": manifest_file,
        "checks": {
            name: {k: v for k, v in check.items() if k != "checked"}
            for name, check in checks.items()
        },
    }
