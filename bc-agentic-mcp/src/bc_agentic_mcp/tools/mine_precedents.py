"""bc_mine_precedents — how did we deliver items LIKE this one before? (enforced context)

Mines ADO history for the spec's work item: similar closed items -> their PRs ->
changed paths -> distilled delivery shape. The result (or an explicit, reasoned skip)
is REQUIRED evidence: bc_plan_design fail-closes with `blocked_precedents_due` for
ADO-backed specs until `context/precedents.json` exists.
"""
import os
from pathlib import Path
from typing import Any, Dict, Optional

from bc_agentic_mcp import item_context, precedents


async def handle_mine_precedents(
    project_root: str,
    spec_name: str,
    work_item_id: Optional[str] = None,
    item_type: Optional[str] = None,
    title: Optional[str] = None,
    top_k: int = 5,
    skip: bool = False,
    reason: Optional[str] = None,
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
) -> Dict[str, Any]:
    """Mine precedents for the spec's captured item (identity defaults from the
    capture manifest — never guessed). ``skip=true`` records an explicit waiver
    instead; ``reason`` is then mandatory and lands in the audit trail."""
    root = Path(project_root).resolve()

    if skip:
        if not (reason or "").strip():
            return {"status": "blocked_skip_needs_reason", "blocked": True,
                    "reason": "an explicit skip must say WHY (audited waiver, not a shrug)"}
        from datetime import datetime, timezone
        payload = {"skipped": True, "reason": reason.strip(),
                   "as_of": datetime.now(timezone.utc).isoformat()}
        path = precedents.save(root, spec_name, payload)
        return {"status": "skipped", "recorded": True, "path": str(path)}

    # Identity from the captured manifest — the same no-guessing rule as everywhere.
    ctx = item_context.load_context(str(root), spec_name) or {}
    identity = ctx.get("identity") or {}
    work_item_id = work_item_id or str(ctx.get("item_id") or "")
    item_type = item_type or str(identity.get("type") or "")
    title = title or str(identity.get("title") or "")
    if not (work_item_id and item_type and title):
        return {"status": "blocked_no_identity", "blocked": True,
                "reason": ("work item identity incomplete (need id+type+title); capture "
                           "the item first (bc_capture_item_context) or pass them explicitly"),
                "next_action": {"tool": "bc_capture_item_context",
                                "params_hint": {"spec_name": spec_name}}}

    org_url = org_url or os.environ.get("AZURE_DEVOPS_ORG")
    project = project or os.environ.get("AZURE_DEVOPS_PROJECT")
    pat = os.environ.get(pat_env, "")
    if not (org_url and project and pat):
        return {"status": "blocked_no_ado_access", "blocked": True,
                "reason": (f"AZURE_DEVOPS_ORG/PROJECT/{pat_env} not configured; either fix "
                           "the env or record an explicit waiver via skip=true + reason")}

    payload = precedents.mine(
        org_url=org_url, project=project, item_id=work_item_id,
        item_type=item_type, title=title, pat=pat, top_k=top_k)
    path = precedents.save(root, spec_name, payload)
    shape = payload.get("delivery_shape", {})
    return {
        "status": "mined",
        "path": str(path),
        "candidate_pool": payload.get("candidate_pool", 0),
        "precedents": [
            {"id": p["id"], "title": p["title"], "score": p["score"],
             "pr_ids": p["pr_ids"], "files_changed": p["files_changed"]}
            for p in payload.get("precedents", [])
        ],
        "delivery_shape": shape,
        "hint": ("delivery_shape is the historical footprint of items like this one — "
                 "hold the spec's objects_to_modify/create against it before planning"),
    }
