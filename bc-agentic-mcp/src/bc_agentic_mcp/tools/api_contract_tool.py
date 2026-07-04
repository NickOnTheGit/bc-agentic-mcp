"""bc_api_contract — run OData/API contract checks against a live endpoint and capture evidence."""
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Optional, Union

from bc_agentic_mcp import api_contract, verification
from bc_agentic_mcp.spec_loader import load_spec


def _fields_from_spec(project_root: str, spec_name: str) -> List[Dict[str, Any]]:
    try:
        spec = load_spec(specs_root(project_root) / spec_name)
        return list(spec.get("data_model", []) or [])
    except Exception:
        return []


async def handle_api_contract(
    project_root: str,
    base_url: str,
    entity: str,
    fields: Optional[List[Dict[str, Any]]] = None,
    operations: Optional[Any] = None,
    user: str = "",
    password_env: str = "BC_API_PASSWORD",
    spec_name: Optional[str] = None,
    covers: Optional[Union[str, List[int]]] = None,
) -> Dict[str, Any]:
    """Execute the derived contract plan (GET + negatives + boundaries) against ``base_url``.

    ``fields``/``operations`` may be provided directly or loaded from the spec's data_model.
    Credentials come from ``password_env``. On a supplied ``spec_name`` + ``covers`` the
    captured result is recorded as runtime evidence.
    """
    if fields is None and spec_name:
        fields = _fields_from_spec(project_root, spec_name)
    result = api_contract.run_contract(
        base_url=base_url,
        entity=entity,
        fields=fields or [],
        operations=operations or {},
        user=user,
        password_env=password_env,
    )
    if spec_name and covers is not None and result.get("executed"):
        evidence = (
            f"{base_url}/{entity} contract {result.get('passed')}/{result.get('total')} passed"
        )
        # Persist the EXPLICIT per-check list — aggregate-only records left the PR
        # template unable to name what each API check validated (observed live:
        # 'API contract 4/4' while the reviewer had to trust a bare count).
        executed_checks = [
            {"codeunit": entity,
             "test": str(r.get("id", "?")),
             "shape": "negative" if r.get("expect") == "reject" else "happy",
             "result": "success" if r.get("passed") else "failure",
             "validates": (f"{r.get('method', '?')} {r.get('reason', '')} "
                            f"(expected {'a 4xx rejection' if r.get('expect') == 'reject' else 'a 2xx acceptance'}, "
                            f"got HTTP {r.get('status')})")}
            for r in result.get("results", [])
        ]
        verification.record_test(
            Path(project_root).resolve(),
            spec_name,
            name=f"API contract {result.get('passed')}/{result.get('total')} ({entity})",
            result="pass" if result.get("all_passed") else "fail",
            covers=covers,
            layer="api",
            evidence=evidence,
            executed_tests=executed_checks,
        )
        result["evidence_recorded"] = True
    return result
