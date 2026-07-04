"""ado_items — WRITE-back to Azure DevOps: turn refined children into real work items.

The Refinement Lab (and feature planning) can propose child PBIs; until now a
human had to create them in ADO by hand. This module closes that loop with the
same fail-closed REST+PAT seam the PR module uses.

Deliberate-write discipline (shared-system mutation):
  * ``confirm=True`` is MANDATORY — an agent cannot push items as a side effect;
  * every created id is recorded to ``pushed_items.json`` in the spec folder
    (idempotency: re-push skips titles already recorded);
  * the PAT comes from the environment and is never logged or returned.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.pr import DEFAULT_PAT_ENV, Requester, _auth, _default_requester
from bc_agentic_mcp.workspace import specs_root

API_VERSION = "7.1"


def _build_patch(title: str, description: str, parent_url: Optional[str]) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = [
        {"op": "add", "path": "/fields/System.Title", "value": title[:255]},
    ]
    if description:
        ops.append({"op": "add", "path": "/fields/System.Description", "value": description})
    if parent_url:
        ops.append({"op": "add", "path": "/relations/-", "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse", "url": parent_url}})
    return ops


def create_work_item(
    *, org_url: str, project: str, item_type: str, title: str, description: str = "",
    parent_id: Optional[str] = None, pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    """Create ONE work item (optionally parented). Fail-closed without a PAT."""
    pat, refusal = _auth(pat_env, requester)
    if refusal:
        return refusal
    send = requester or _default_requester
    from urllib.parse import quote
    proj = quote(str(project), safe="")
    encoded_type = quote(str(item_type), safe="")
    url = f"{org_url.rstrip('/')}/{proj}/_apis/wit/workitems/${encoded_type}?api-version={API_VERSION}"
    parent_url = (f"{org_url.rstrip('/')}/{proj}/_apis/wit/workItems/{parent_id}"
                  if parent_id else None)
    headers = {"Content-Type": "application/json-patch+json"}
    if pat:
        from bc_agentic_mcp.pr import basic_auth_header
        headers["Authorization"] = basic_auth_header(pat)
    body = json.dumps(_build_patch(title, description, parent_url)).encode("utf-8")
    status, text = send("POST", url, headers, body)
    if status < 200 or status >= 300:
        return {"created": False, "status_code": status,
                "reason": f"ADO refused the create (HTTP {status}): {text[:300]}"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"created": False, "reason": "ADO returned non-JSON on create."}
    return {"created": True, "id": str(data.get("id", "")),
            "url": str((data.get("_links") or {}).get("html", {}).get("href", ""))}


def handle_push_items(
    project_root: str,
    spec_name: str,
    org_url: str,
    project: str,
    items: List[Dict[str, Any]],
    parent_work_item_id: Optional[str] = None,
    item_type: str = "Product Backlog Item",
    confirm: bool = False,
    pat_env: str = DEFAULT_PAT_ENV,
    requester: Optional[Requester] = None,
) -> Dict[str, Any]:
    """Create the proposed child items IN Azure DevOps (deliberate write)."""
    if not confirm:
        return {
            "status": "blocked_confirmation_required", "blocked": True,
            "reason": "Creating work items mutates the shared ADO project. Re-call with "
                      "confirm=true AFTER the human explicitly approved the child list.",
        }
    wanted = [i for i in (items or [])
              if isinstance(i, dict) and str(i.get("title") or "").strip()]
    if not wanted:
        return {"status": "error", "reason": "items must contain at least one {title, description?}."}

    sdir = specs_root(Path(project_root).resolve()) / spec_name
    sdir.mkdir(parents=True, exist_ok=True)
    record_path = sdir / "pushed_items.json"
    record: Dict[str, Any] = {"items": []}
    if record_path.exists():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {"items": []}
    already = {str(i.get("title", "")).strip().lower() for i in record["items"]}

    created: List[Dict[str, Any]] = []
    skipped: List[str] = []
    failures: List[Dict[str, Any]] = []
    for item in wanted:
        title = str(item["title"]).strip()
        if title.lower() in already:
            skipped.append(title)
            continue
        result = create_work_item(
            org_url=org_url, project=project, item_type=item_type, title=title,
            description=str(item.get("description") or ""),
            parent_id=parent_work_item_id, pat_env=pat_env, requester=requester)
        if result.get("created"):
            entry = {"id": result["id"], "title": title, "url": result.get("url", ""),
                     "pushed_at": datetime.now(timezone.utc).isoformat()}
            record["items"].append(entry)
            created.append(entry)
        else:
            failures.append({"title": title, "reason": result.get("reason", "unknown")})
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    out: Dict[str, Any] = {
        "status": "pushed" if created and not failures else
                  ("partial" if created else "failed"),
        "created": created, "skipped_existing": skipped, "failures": failures,
        "record": str(record_path),
    }
    if created and parent_work_item_id:
        out["next_action"] = {
            "tool": "bc_capture_feature",
            "reason": "Children now exist in ADO — capture the feature tree fresh so the "
                      "standard feature lifecycle can run.",
            "params_hint": {"spec_name": spec_name, "work_item_id": parent_work_item_id,
                            "org_url": org_url, "project": project},
        }
    return out
