"""bc_reconcile_target / bc_upgrade_preflight — schema safety gates (deterministic)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import schema
from bc_agentic_mcp.workspace import specs_root

RECONCILE_REPORT_FILENAME = "reconcile_report.json"


def _persist_report(project_root: str, spec_name: str, report: Dict[str, Any]) -> str:
    """Persist the reconcile result so downstream gates (bc_generate_tests) can
    verify that spec fields were checked against deployed reality."""
    sdir = specs_root(Path(project_root).resolve()) / spec_name
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / RECONCILE_REPORT_FILENAME
    payload = dict(report)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return str(path)


async def handle_reconcile_target(
    project_root: str,
    requested: List[str],
    deployed: Optional[List[str]] = None,
    metadata_url: Optional[str] = None,
    entity: str = "",
    spec_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconcile requested fields against what is ALREADY deployed.

    Supply ``deployed`` directly, or a ``metadata_url`` (OData $metadata) to fetch it.
    Surfaces fields that already exist so an "extend the API" item never recreates them.
    When ``spec_name`` is given the report is persisted so test generation can gate on it.
    """
    if deployed is not None:
        result = schema.reconcile_fields(requested, deployed)
    elif metadata_url:
        result = schema.reconcile_against_endpoint(
            requested=requested, metadata_url=metadata_url, entity=entity
        )
    else:
        return {"error": "provide either 'deployed' or 'metadata_url'"}
    if spec_name:
        result["report_path"] = _persist_report(project_root, spec_name, result)
    return result


async def handle_upgrade_preflight(
    project_root: str,
    current_fields: List[str],
    baseline_fields: List[str],
    current_tables: Optional[List[str]] = None,
    baseline_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Detect whether an upgrade would REMOVE fields/tables the deployed baseline has."""
    return schema.diff_schema(
        current_fields,
        baseline_fields,
        current_tables=current_tables,
        baseline_tables=baseline_tables,
    )
