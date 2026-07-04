"""advance — C3 composite driver decision core (pure, fully testable).

One instruction — "advance item <id>" — chains every DETERMINISTIC lifecycle step
server-side and stops only at:
  - a human gate        (plan approval, PR review, merge)
  - a judgment step     (writing spec/code, answering clarifications, rework)
  - a blocked result    (a gate inside a tool refused — its next_action is surfaced)
  - completion          (archived)

Nothing here talks to the network or filesystem: this module only DECIDES; the tool
handler executes decisions through the server's own ``_run_tool`` pipeline so every
chained step still gets policy, doom-loop, timeline and audit treatment.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Tools the driver may invoke autonomously — deterministic, content-free calls only.
# A tool needing model-authored content (spec text, AL code, answers) is NEVER auto-run.
AUTO_SAFE_TOOLS = {
    "bc_generate_tests",
    "bc_run_tests",
    "bc_verify",
    "bc_prepare_pr",
    "bc_create_pr",
    "bc_get_review_comments",
    "bc_merge_status",
    "bc_archive",
}

# Params each auto-safe tool needs beyond project_root/spec_name, and where they
# come from in the advance call's own inputs. Missing input => structured stop.
_TOOL_INPUT_NEEDS: Dict[str, List[str]] = {
    "bc_run_tests": ["container_name", "test_extension_id"],
    "bc_create_pr": ["org_url", "project", "repository"],
}

# Phases where the item is still being SHAPED by model judgment — never auto-advanced.
_PLANNING_PHASES = {
    None, "item_received", "item_refined", "root_cause_identified", "spec_written",
    "design_planned", "code_context_built", "tasks_broken_down", "review_prepared",
    "feature_captured", "feature_refined", "feature_planned",
}

# Result statuses that are deterministic terminal stops for this advance run.
_STATUS_STOPS = {
    "review_comments_open": ("waiting_rework",
                             "Open PR review comments need code changes (model judgment)."),
    "approved": ("waiting_human_merge",
                 "PR approved — merging is human gate 3 (the ADO merge button)."),
    "pending_review": ("waiting_pr_review",
                       "PR awaits reviewer votes — human gate 2 happens in ADO."),
}


def seed_action(phase: Optional[str], available: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic phase -> first action (or stop). ``available`` holds the advance
    call's optional inputs (container/org coordinates)."""
    if phase == "archived":
        return {"stop": "done", "reason": "Item is archived — lifecycle complete."}
    if phase in _PLANNING_PHASES:
        return {
            "stop": "waiting_judgment",
            "reason": (
                "Planning artifacts are model-authored. Produce/complete them, then "
                "bc_request_approval for the `plan` gate."
            ),
        }
    if phase == "approval_requested":
        return {
            "stop": "waiting_human_approval",
            "reason": "Human gate 1: a pending approval awaits bc_submit_decision.",
        }
    if phase == "decision_recorded":
        return {
            "stop": "waiting_judgment",
            "reason": "Plan approved — write the implementation via bc_implement_write.",
        }
    if phase == "review_comments_open":
        return {"stop": "waiting_rework",
                "reason": "Open PR review comments need code changes (model judgment)."}
    table = {
        "implemented": "bc_generate_tests",
        "tests_generated": "bc_run_tests",
        "tests_run": "bc_verify",
        "verified": "bc_prepare_pr",
        "reviewed": "bc_prepare_pr",
        "pr_prepared": "bc_create_pr",
        "pr_created": "bc_get_review_comments",
        "merged": "bc_archive",
    }
    tool = table.get(phase or "")
    if tool is None:
        return {"stop": "waiting_judgment", "reason": f"No deterministic step for phase '{phase}'."}
    return _action_or_missing_input(tool, available)


def _action_or_missing_input(tool: str, available: Dict[str, Any]) -> Dict[str, Any]:
    missing = [k for k in _TOOL_INPUT_NEEDS.get(tool, []) if not available.get(k)]
    if missing:
        return {
            "stop": "waiting_input",
            "reason": f"{tool} needs {', '.join(missing)} — pass them to bc_advance to continue.",
            "needed": missing,
            "tool": tool,
        }
    return {"action": tool}


def next_after_result(
    executed_tool: str, result: Any, available: Dict[str, Any]
) -> Dict[str, Any]:
    """Decide what follows a completed step, from the step's OWN result (ground truth)."""
    if not isinstance(result, dict):
        return {"stop": "error", "reason": f"{executed_tool} returned a non-dict result."}
    if result.get("isError") is True:
        return {"stop": "error", "reason": f"{executed_tool} failed.",
                "detail": str(result.get("content", ""))[:300]}
    if result.get("blocked") is True or str(result.get("status", "")).startswith("blocked"):
        return {
            "stop": "blocked",
            "reason": f"{executed_tool} is gated: {result.get('reason') or result.get('message', '')}"[:400],
            "next_action": result.get("next_action"),
        }
    status = str(result.get("status", ""))
    if status in _STATUS_STOPS:
        stop, reason = _STATUS_STOPS[status]
        return {"stop": stop, "reason": reason}
    if executed_tool == "bc_archive":
        return {"stop": "done", "reason": "Item archived — lifecycle complete."}
    # Follow the tool's own next_action when it is auto-safe; otherwise it needs judgment.
    na = result.get("next_action")
    tool = (na or {}).get("tool") if isinstance(na, dict) else None
    if tool in AUTO_SAFE_TOOLS:
        step = _action_or_missing_input(tool, available)
        if "action" in step and tool == executed_tool:
            return {"stop": "no_progress",
                    "reason": f"{tool} would repeat immediately — re-run bc_advance later."}
        return step
    if tool:
        return {"stop": "waiting_judgment",
                "reason": f"Next step '{tool}' needs model/human input.", "next_action": na}
    return {"stop": "step_complete",
            "reason": f"{executed_tool} completed with no onward action — re-derive from phase."}


def build_step_params(tool: str, base: Dict[str, Any], available: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the exact kwargs for an auto-safe step (nothing extra, nothing implicit)."""
    params = dict(base)  # project_root + spec_name
    spec_name = base.get("spec_name")
    if tool == "bc_run_tests":
        params.update({
            "container_name": available["container_name"],
            "test_extension_id": available["test_extension_id"],
            "credential_env": available.get("credential_env", "BC_TEST_PASSWORD"),
            "spec_name": spec_name,
            "covers": "all",
            "validation_mode": "item",
        })
        if available.get("app_project_folder"):
            params["app_project_folder"] = available["app_project_folder"]
    elif tool == "bc_create_pr":
        params.update({
            "org_url": available["org_url"],
            "project": available["project"],
            "repository": available["repository"],
        })
        if available.get("work_item_id"):
            params["work_item_id"] = available["work_item_id"]
    elif tool == "bc_prepare_pr" and available.get("target_branch"):
        params["target_branch"] = available["target_branch"]
    return params
