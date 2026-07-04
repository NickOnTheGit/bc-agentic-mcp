"""api_contract — deterministic OData/API contract test planning and execution.

Builds a request plan from the spec's own field metadata + the testing playbook (no
hardcoded entity or field names), executes it against a live endpoint through an
injectable fetcher seam, and classifies each response. Credentials come from the
environment; only the plan and the pass/fail classification are pure logic.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable, Dict, List, Optional
from urllib import request as _urlrequest
from urllib import error as _urlerror

from bc_agentic_mcp import testing_playbook as playbook

# A fetcher returns (status_code, body_text). Injectable for tests.
Fetcher = Callable[[str, str, Dict[str, str], Optional[bytes]], "tuple[int, str]"]


def build_contract_plan(
    fields: List[Dict[str, Any]],
    operations: Any,
    *,
    entity: str,
) -> List[Dict[str, Any]]:
    """Deterministic list of contract checks derived from field types + declared operations.

    Each check = {id, method, expect ('accept'|'reject'), field?, value?, reason}.
    """
    is_update = False
    if isinstance(operations, dict):
        is_update = any(bool(operations.get(k)) for k in ("update", "create", "delete", "insert"))
    elif isinstance(operations, list):
        is_update = any(str(o).lower() in ("update", "create", "delete", "insert") for o in operations)

    checks: List[Dict[str, Any]] = [
        {"id": "read", "method": "GET", "expect": "accept", "reason": "entity is readable"},
    ]
    has_date = False
    for f in fields or []:
        name = f.get("name", "")
        al_type = f.get("al_type", f.get("type", ""))
        # A NON-EDITABLE field's contract is the OPPOSITE of an editable one:
        # every write must be REFUSED — expecting the boundary write to succeed
        # contradicts the spec itself (observed live on FacilityCodeFilter,
        # Editable=false: the 'exactly 250 chars accepted' check could never pass).
        editable = f.get("editable", True) is not False
        if str(al_type).lower().startswith("date"):
            has_date = True
        for neg in playbook.negative_cases_for_field(name, al_type):
            checks.append({
                "id": f"neg:{name}:{len(checks)}",
                "method": "PATCH", "expect": "reject",
                "field": name, "reason": neg["scenario"],
            })
        for bnd in playbook.boundary_cases_for_field(name, al_type):
            checks.append({
                "id": f"bnd:{name}:{len(checks)}",
                "method": "PATCH",
                "expect": "accept" if editable else "reject",
                "field": name,
                "reason": bnd["scenario"] + ("" if editable else " — field is non-editable: the write must be refused"),
            })
    if has_date:
        for edge in playbook.date_edge_cases():
            checks.append({
                "id": f"date:{edge['value']}",
                "method": "PATCH", "expect": edge["expect"],
                "value": edge["value"], "reason": edge["reason"],
            })
    for c in playbook.api_contract_negatives(is_update):
        method = "POST" if "insert" in c["scenario"].lower() else (
            "DELETE" if "delete" in c["scenario"].lower() else "PATCH")
        checks.append({
            "id": f"contract:{len(checks)}",
            "method": method, "expect": "reject", "reason": c["scenario"],
        })
    return checks


def classify_response(expect: str, status_code: int) -> Dict[str, Any]:
    """Deterministic pass/fail: 'accept' wants 2xx; 'reject' wants a client-side
    refusal — 4xx INCLUDING 405 (read-only API pages refuse writes with
    MethodNotAllowed, which IS the contract for non-editable surfaces)."""
    ok_2xx = 200 <= status_code < 300
    client_4xx = 400 <= status_code < 500
    passed = ok_2xx if expect == "accept" else client_4xx
    return {"status": status_code, "expect": expect, "passed": bool(passed)}


def _basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("ascii")).decode("ascii")
    return f"Basic {token}"


def _default_fetcher(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]):
    req = _urlrequest.Request(url, method=method, headers=headers, data=body)
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310 (caller-provided URL)
            return resp.status, resp.read().decode("utf-8", "replace")
    except _urlerror.HTTPError as exc:  # 4xx/5xx come here
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""


def run_contract(
    *,
    base_url: str,
    entity: str,
    fields: List[Dict[str, Any]],
    operations: Any,
    user: str = "",
    password_env: str = "BC_API_PASSWORD",
    fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Execute the contract plan. ``fetcher`` is injectable; credentials from the env."""
    plan = build_contract_plan(fields, operations, entity=entity)
    password = os.environ.get(password_env)
    if fetcher is None and (not password or not user):
        return {"executed": False, "reason": "missing credentials/env", "plan_size": len(plan)}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if user and password:
        headers["Authorization"] = _basic_auth_header(user, password)
    fetch = fetcher or _default_fetcher
    results: List[Dict[str, Any]] = []
    passed = 0
    for check in plan:
        url = f"{base_url.rstrip('/')}/{entity}"
        status, _body = fetch(check["method"], url, headers, None)
        verdict = classify_response(check["expect"], status)
        verdict.update({"id": check["id"], "reason": check["reason"], "method": check["method"]})
        results.append(verdict)
        if verdict["passed"]:
            passed += 1
    return {
        "executed": True,
        "entity": entity,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "all_passed": passed == len(results) and len(results) > 0,
        "results": results,
    }
