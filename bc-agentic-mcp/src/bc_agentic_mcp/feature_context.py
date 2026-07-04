"""feature_context — capture an ENTIRE feature tree fresh (Workstream H, tier above items).

Observed proof (feature 239584): the question that blocked item-level planning ("what is
the 'Facilities per Space' toggle?") was ANSWERED BY A SIBLING PBI (#240435). Cross-item
decisions are only visible at feature altitude — so capture the whole tree, deterministically.

Same rules as item capture: REST+PAT fresh (never memory), fetcher seam for tests,
fail-closed without a PAT, everything persisted with hashes to .specs/<feature-spec>/context/.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import error as _urlerror
from urllib import request as _urlrequest

from bc_agentic_mcp.item_context import _html_to_text
from bc_agentic_mcp.workspace import specs_root

Fetcher = Callable[[str, Dict[str, str]], "tuple[int, str]"]

_PARENT_REL = "System.LinkTypes.Hierarchy-Reverse"
_CHILD_REL = "System.LinkTypes.Hierarchy-Forward"


def _default_fetcher(url: str, headers: Dict[str, str]):
    req = _urlrequest.Request(url, headers=headers, method="GET")
    try:
        with _urlrequest.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except _urlerror.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace") if exc.fp else ""
    except Exception as exc:
        return 0, str(exc)


def _auth_header(pat: str) -> Dict[str, str]:
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def workitem_url(org_url: str, project: str, item_id: str, expand: str = "relations") -> str:
    from urllib.parse import quote
    return (f"{org_url.rstrip('/')}/{quote(str(project), safe='')}/_apis/wit/workitems/{item_id}"
            f"?$expand={expand}&api-version=7.0")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _fields_of(payload: Dict[str, Any]) -> Dict[str, Any]:
    f = payload.get("fields", {})
    return {
        "id": payload.get("id"),
        "type": f.get("System.WorkItemType", ""),
        "title": f.get("System.Title", ""),
        "state": f.get("System.State", ""),
        "assigned": (f.get("System.AssignedTo") or {}).get("displayName", ""),
        "description": _html_to_text(f.get("System.Description", "") or ""),
    }


def fetch_item(org_url: str, project: str, item_id: str, *,
               headers: Dict[str, str], fetcher: Fetcher) -> Optional[Dict[str, Any]]:
    status, body = fetcher(workitem_url(org_url, project, item_id), headers)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def resolve_feature(payload: Dict[str, Any]) -> "tuple[Optional[str], List[str]]":
    """(parent_id, child_ids) from a work item's relations. Deterministic order."""
    parent = None
    children: List[str] = []
    for rel in payload.get("relations", []) or []:
        target = str(rel.get("url", "")).rsplit("/", 1)[-1]
        if rel.get("rel") == _PARENT_REL:
            parent = target
        elif rel.get("rel") == _CHILD_REL:
            children.append(target)
    return parent, sorted(children, key=lambda x: int(x) if x.isdigit() else 0)


def capture_feature(
    root: str,
    spec_name: str,
    *,
    work_item_id: str,
    org_url: str,
    project: str,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
    fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Capture the feature containing ``work_item_id`` (the id may BE the feature or any child)."""
    pat = os.environ.get(pat_env)
    if not pat and fetcher is None:
        return {"captured": False,
                "reason": f"{pat_env} not set — cannot fetch the feature tree fresh. Set the PAT and retry."}
    headers = _auth_header(pat or "")
    fetch = fetcher or _default_fetcher

    seed = fetch_item(org_url, project, str(work_item_id), headers=headers, fetcher=fetch)
    if seed is None:
        return {"captured": False, "reason": f"work item {work_item_id} not fetchable"}
    seed_type = str(seed.get("fields", {}).get("System.WorkItemType", "")).lower()
    parent_id, _ = resolve_feature(seed)

    if seed_type == "feature":
        feature_payload = seed
    elif parent_id:
        feature_payload = fetch_item(org_url, project, parent_id, headers=headers, fetcher=fetch)
        if feature_payload is None:
            return {"captured": False, "reason": f"parent feature {parent_id} not fetchable"}
    else:
        return {"captured": False,
                "reason": f"work item {work_item_id} has no parent feature (and is not a Feature itself)"}

    _, child_ids = resolve_feature(feature_payload)
    feature = _fields_of(feature_payload)

    cdir = specs_root(Path(root).resolve()) / spec_name / "context"
    (cdir / "children").mkdir(parents=True, exist_ok=True)

    children: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    for cid in child_ids:
        payload = fetch_item(org_url, project, cid, headers=headers, fetcher=fetch)
        if payload is None:
            unresolved.append(cid)
            continue
        child = _fields_of(payload)
        children.append(child)
        body = (f"# {child['title']} (#{child['id']})\n\n"
                f"- type: {child['type']}\n- state: {child['state']}\n"
                f"- assigned: {child['assigned'] or '(none)'}\n\n{child['description']}\n")
        (cdir / "children" / f"{cid}.md").write_text(body, encoding="utf-8")

    tree = {"feature": feature, "children": children}
    (cdir / "feature.json").write_text(json.dumps(tree, indent=1, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "spec_name": spec_name,
        "feature_id": str(feature["id"]),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "child_count": len(children),
        "states": sorted({c["state"] for c in children}),
        "children": [{"id": c["id"], "state": c["state"], "title": c["title"][:100],
                      "sha": _sha(c["description"])} for c in children],
        "unresolved": unresolved,
        "complete": not unresolved,
    }
    (cdir / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"captured": True, **manifest}


def load_tree(root: str, spec_name: str) -> Optional[Dict[str, Any]]:
    path = specs_root(Path(root).resolve()) / spec_name / "context" / "feature.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
