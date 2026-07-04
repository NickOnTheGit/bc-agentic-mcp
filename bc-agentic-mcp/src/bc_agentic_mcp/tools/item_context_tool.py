"""bc_capture_item_context — save ALL of a work item's referenced data FRESH to disk, once.

First action on a new item: materialize the description + every linked wiki page + related
items into `.specs/<spec>/context/`. Everything downstream references this bundle, so the
work is never driven by stale caches or code-convention guesses.
"""
import os
from typing import Any, Dict, Optional

from bc_agentic_mcp import item_context, wiki


async def handle_capture_item_context(
    project_root: str,
    spec_name: str,
    work_item_id: str,
    description: Optional[str] = None,
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    pat_env: str = "AZURE_DEVOPS_EXT_PAT",
) -> Dict[str, Any]:
    """Capture the item's full fresh context. If ``description`` is omitted, the item is
    fetched live via REST+PAT. ``org_url``/``project`` default to the AZURE_DEVOPS_* env vars.
    """
    org_url = org_url or os.environ.get("AZURE_DEVOPS_ORG")
    project = project or os.environ.get("AZURE_DEVOPS_PROJECT")

    identity: Dict[str, Any] = {}
    wi: Dict[str, Any] = {}
    if org_url and project:
        wi = item_context.fetch_work_item(
            org_url=org_url, project=project, item_id=work_item_id, pat_env=pat_env
        )
        if wi.get("fetched"):
            fields = wi.get("fields", {}) or {}
            identity = {
                "id": str(work_item_id),
                "type": fields.get("System.WorkItemType", ""),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "parent_id": str(fields.get("System.Parent") or "") or None,
            }
            # Bugfix lane: a Bug routes through diagnosis-before-planning (bc_root_cause).
            if str(identity["type"]).strip().lower() == "bug":
                identity["lane"] = "bugfix"
                identity["severity"] = fields.get("Microsoft.VSTS.Common.Severity", "")
                identity["priority"] = fields.get("Microsoft.VSTS.Common.Priority", "")

    if description is None:
        if not wi.get("fetched"):
            return {"captured": False,
                    "reason": "no description and the item could not be fetched fresh "
                              f"({wi.get('reason', 'AZURE_DEVOPS_ORG/PROJECT not set')})"}
        description = wi["description"]

    comments = None
    ancestry = None
    if org_url and project:
        # Parent identity: resolve the parent's type/title so "child of Feature 239584
        # 'Facilities per Space'" can be stated everywhere without guessing.
        if identity.get("parent_id"):
            pw = item_context.fetch_work_item(
                org_url=org_url, project=project, item_id=identity["parent_id"], pat_env=pat_env
            )
            if pw.get("fetched"):
                pf = pw.get("fields", {}) or {}
                identity["parent_type"] = pf.get("System.WorkItemType", "")
                identity["parent_title"] = pf.get("System.Title", "")
        cm = item_context.fetch_comments(
            org_url=org_url, project=project, item_id=work_item_id, pat_env=pat_env
        )
        if cm.get("fetched"):
            comments = cm["comments"]
        # Walk parent -> feature -> epic so the plan has the full "why" context.
        ancestry = item_context.fetch_ancestry(
            org_url=org_url, project=project, item_id=work_item_id, pat_env=pat_env
        )

    manifest = item_context.capture(
        project_root, spec_name, item_id=work_item_id, description=description,
        org_url=org_url, project=project, pat_env=pat_env, comments=comments,
        extra_related_ids=ancestry, identity=identity,
    )
    result: Dict[str, Any] = {"captured": True, "complete": manifest["complete"], "manifest": manifest}
    q = manifest.get("quarantine") or {}
    if q.get("risk") in ("high", "low"):
        result["quarantine_risk"] = q["risk"]
        if q.get("warning"):
            result["warning"] = q["warning"]
    if str(identity.get("lane", "")).lower() == "bugfix":
        result["lane"] = "bugfix"
        result["next_action"] = {
            "tool": "bc_root_cause",
            "reason": "Bug lane: diagnose BEFORE planning — record symptom, root cause and "
                      "code evidence (verified against the repo), then write the fix spec.",
            "params_hint": {"spec_name": spec_name,
                            "symptom": "<observed wrong behavior>",
                            "root_cause": "<diagnosis grounded in code>",
                            "evidence": ["<path/to/File.al or 'table 11024121'>"],
                            "fix_approach": "<planned fix>"},
        }
    return result
