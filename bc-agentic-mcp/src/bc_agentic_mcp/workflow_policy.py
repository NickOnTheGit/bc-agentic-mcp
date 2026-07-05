"""workflow_policy — MCP-only orchestration contract enforcement.

Enforces two dimensions at server runtime:
1) Agent-role allowlist (planner / implementer / gatekeeper / orchestrator)
2) Deterministic stage routing (plan -> implement -> verify -> archive)

This applies only to MCP tool calls (server-side). It is intentionally fail-closed
for unknown roles and unknown tools.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bc_agentic_mcp import timeline


COMMON_TOOLS = {
    "bc_status",
    "bc_recall",
    "bc_checkpoint",
    "bc_timeline",
    "bc_guard_pr_thread_resolution",
    # Reflection is the CLOSING half of the checkpoint loop: a correction/mistake
    # checkpoint (recordable in every stage) raises reflection_due, and the contract
    # demands bc_reflect before anything else — so it must be callable wherever
    # bc_checkpoint is, or the loop deadlocks (observed live in stage 'plan').
    "bc_reflect",
    # PRESCRIBED-TOOL LAW (12 contradictions in one session taught this): any tool a
    # GATE can name in its blocker/next_action must be callable in EVERY stage — the
    # enforcement engines (clarifications, refinement judgment, reflection) fire in any
    # stage, so their resolving tools can never be stage-fenced without deadlock.
    "bc_answer_clarification",
    # Commit gate demands refinement judgment (critique) whenever mismatches exist —
    # observed live in stage 'implement' on wi265204 (7 mismatches, tool blocked).
    "bc_refine_item",
    # Infrastructure preflight: environment truth is needed in EVERY stage/role and
    # advances no lifecycle phase — it must never be blocked by stage routing.
    "bc_env_preflight",
    # C3 composite driver: chains steps through _run_tool, so each INNER call is
    # policy-checked against the caller's role — the driver grants no extra power.
    "bc_advance",
    # E2 read-only metrics.
    "bc_metrics",
    # H: read-only feature roll-up — needed in every stage to route between items.
    "bc_feature_status",
    # The feature-review packet is a read-only REPORT over child specs: the mega
    # review is born in plan, but the packet must be REGENERABLE after implement/
    # verify to reflect reality (13th stage contradiction, observed live: blocked
    # in stage 'implement' right after the full container sweep went green).
    "bc_prepare_feature_review",
    # Repo table of contents — read-only, useful in every stage.
    "bc_repo_map",
    # Per-item isolated checkouts — infrastructure, needed in every stage.
    "bc_worktree",
    # Read-only reliability report over the audit log.
    "bc_tool_health",
    "_health",
    "bc_health",
}

PLANNER_TOOLS = {
    "bc_init",
    "bc_analyze_module",
    "bc_capture_item_context",
    "bc_mine_precedents",
    "bc_clarify",
    "bc_answer_clarification",
    "bc_auto_clarify",
    "bc_write_spec",
    # Feature tier (Workstream H): planning-side tools.
    "bc_capture_feature",
    "bc_refine_feature",
    "bc_plan_feature",
    "bc_refine_item",
    "bc_prepare_feature_review",
    # Bugfix lane: diagnosis is a planning-side step (before the fix spec).
    "bc_root_cause",
    "bc_plan_design",
    "bc_breakdown_tasks",
    "bc_prepare_review",
    "bc_extract_references",
    "bc_fetch_wiki",
    # Refinement lab: free-form material -> dossier -> lane graduation.
    "bc_intake_start",
    "bc_intake_add",
    "bc_intake_analyze",
    "bc_intake_graduate",
    # Cross-artifact consistency + ADO write-back (planning-side).
    "bc_analyze_consistency",
    "bc_push_items",
    "bc_find_consumers",
    "bc_reconcile_target",
    "bc_upgrade_preflight",
    "bc_request_approval",
}

IMPLEMENTER_TOOLS = {
    "bc_implement",
    "bc_implement_context",
    "bc_implement_write",
    "bc_implement_delete",
    "bc_generate_tests",
    "bc_upgrade_codeunit",
    "bc_quality_check",
    "bc_read_code_context",
    # The write gate PRESCRIBES bc_prepare_review when the packet is stale
    # (blocked_needs_fresh_review) — a prescribed tool must never be policy-blocked
    # (10th stage contradiction, observed live on wi240032: plan approved -> stage
    # implement -> stale packet -> prepare_review CLIENT_ERROR deadlock).
    "bc_prepare_review",
}

# B1: the PR family lives in the gatekeeper role (verification-side of the lifecycle).
PR_TOOLS = {
    "bc_prepare_pr",
    "bc_create_pr",
    "bc_get_review_comments",
    "bc_resolve_review_comment",
    "bc_merge_status",
    "bc_sync_item_state",
}

GATEKEEPER_TOOLS = {
    "bc_submit_decision",
    "bc_approve_data_model",
    "bc_run_tests",
    "bc_api_contract",
    "bc_check_permission_coverage",
    "bc_detect",
    "bc_review",
    "bc_record_test",
    "bc_verify",
    "bc_converge",
    "bc_reflect",
    "bc_lessons",
    "bc_promote_lesson",
    "bc_feedback",
    "bc_archive",
} | PR_TOOLS

ROLE_ALLOWLIST = {
    "planner": COMMON_TOOLS | PLANNER_TOOLS,
    "implementer": COMMON_TOOLS | IMPLEMENTER_TOOLS,
    "gatekeeper": COMMON_TOOLS | GATEKEEPER_TOOLS,
    "orchestrator": COMMON_TOOLS | PLANNER_TOOLS | IMPLEMENTER_TOOLS | GATEKEEPER_TOOLS,
}

STAGE_ALLOWLIST = {
    # bc_read_code_context and bc_quality_check are PLANNING-evidence tools: the
    # code_context and quality enforcement engines PRESCRIBE them before any spec
    # can be approved — a stage policy that blocks what enforcement demands is a
    # deterministic-layer contradiction (read-only analyzers, safe in plan).
    "plan": COMMON_TOOLS | PLANNER_TOOLS | {
        "bc_submit_decision", "bc_read_code_context", "bc_quality_check",
        "bc_approve_data_model",
    },
    # B2 rework loop: implement stage includes the PR read/resolve tools so open review
    # comments can be fixed (within Charter scope) and resolved without a stage fight.
    # bc_detect + bc_review are the MANDATORY post-write steps (lifecycle 11-12): the
    # mistake detector and independent review run on the fresh diff BEFORE verify.
    "implement": COMMON_TOOLS | IMPLEMENTER_TOOLS | {
        "bc_request_approval", "bc_submit_decision", "bc_run_tests",
        "bc_detect", "bc_review", "bc_quality_check", "bc_approve_data_model",
        # Evidence completion happens WHILE the item sits in implement (phase
        # tests_run maps here): verify/record/api-contract must be callable or the
        # lifecycle orders a step the stage forbids (contradiction class, 9th case).
        "bc_verify", "bc_record_test", "bc_api_contract",
        "bc_get_review_comments", "bc_resolve_review_comment", "bc_merge_status",
        # ENFORCEMENT REMEDIATION INVARIANT: every tool an engine prescribes in its
        # next_action must be callable here — the commit gate blocks implement-stage
        # commits on red engines, so a stage that forbids the named fix deadlocks the
        # loop (observed live: clarifications engine red, bc_answer_clarification
        # stage-blocked; scope changes mid-implement need spec/refine reruns too).
        "bc_capture_item_context", "bc_refine_item", "bc_root_cause",
        "bc_write_spec", "bc_answer_clarification",
    },
    # Verify stage runs the EVIDENCE loop (lifecycle 13-14): generate tests, write the
    # test codeunit (bc_implement_write is scope-enforced either way), execute in the
    # container, record results. The stage map must allow what the lifecycle orders —
    # 'reviewed' maps to verify, and verify without test tools was a dead-end.
    "verify": COMMON_TOOLS | GATEKEEPER_TOOLS | {
        "bc_quality_check", "bc_generate_tests", "bc_implement_write",
        "bc_implement_context", "bc_env_preflight",
    },
    "archive": COMMON_TOOLS | {"bc_status", "bc_recall", "bc_checkpoint", "bc_timeline", "_health"},
}


def _phase_to_stage(phase: Optional[str]) -> str:
    if phase in {"plan_approved", "implemented", "tests_generated", "tests_run", "review_comments_open"}:
        # plan_approved = human said GO: implement tools open up.
        # review_comments_open re-admits implement-stage tools (B2 rework loop).
        return "implement"
    if phase in {"verified", "reviewed", "decision_recorded", "pr_prepared", "pr_created", "merged"}:
        # `merged` stays in verify so bc_archive (gatekeeper) remains callable.
        return "verify"
    if phase in {"archived"}:
        return "archive"
    # feature_captured / feature_planned and all authoring phases live in `plan`.
    return "plan"


# Only these phases carry stage semantics. Everything else (item_refined,
# root-caused, checkpoints, ...) is EVIDENCE and must never move the stage —
# especially not backward to 'plan' (observed live on wi265204: reviewed ->
# item_refined regressed the stage and policy-blocked the prescribed test run).
STAGE_BEARING_PHASES = {
    "plan_approved", "implemented", "tests_generated", "tests_run",
    "review_comments_open", "verified", "reviewed", "decision_recorded",
    "pr_prepared", "pr_created", "merged", "archived",
}

# Once one of these is reached, later EVIDENCE phases (tests_run after a
# supplementary regression slice, implemented after a doc touch-up) must not
# pull the spec back to implement — only the explicit rework trigger
# (review_comments_open) does (observed live: post-verify evidence runs
# regressed a fully verified feature and re-fenced the PR tools).
_VERIFY_TIER_PHASES = {
    "verified", "reviewed", "decision_recorded", "pr_prepared", "pr_created", "merged",
}


def _spec_stage(root: Path, spec_name: str) -> str:
    try:
        history = timeline.phases_in_order(root, spec_name, STAGE_BEARING_PHASES)
    except Exception:
        history = []
    if not history:
        return "plan"
    stage = _phase_to_stage(history[-1])
    if stage == "implement":
        # regress from verify only via the explicit rework trigger
        tail_since_rework = history
        if "review_comments_open" in history:
            last_rework = len(history) - 1 - history[::-1].index("review_comments_open")
            tail_since_rework = history[last_rework:]
        if any(p in _VERIFY_TIER_PHASES for p in tail_since_rework):
            return "verify"
    return stage


def infer_stage(project_root: Optional[str], spec_name: Optional[str]) -> str:
    if not project_root or not spec_name:
        return "plan"
    root = Path(project_root).resolve()
    stage = _spec_stage(root, spec_name)
    if stage in ("plan", "implement"):
        feature_stage = _feature_stage_from_children(root, spec_name)
        if feature_stage is not None:
            return feature_stage
    return stage


def _feature_stage_from_children(root: Path, spec_name: str) -> Optional[str]:
    """A FEATURE's stage is the floor of its children's stages.

    The feature spec itself records only planning phases (captured/planned/
    plan_approved) — the delivery lifecycle lives on the CHILD items. Without
    this, a fully verified feature stays in 'implement' forever and the
    one-PR-per-feature tools (bc_prepare_pr/bc_create_pr) are permanently
    fenced (14th stage contradiction, observed live on feature-239584).
    """
    from bc_agentic_mcp.workspace import specs_root as _specs_root
    try:
        if not (_specs_root(root) / spec_name / "feature_plan.json").exists():
            return None
        from bc_agentic_mcp.tools.feature import feature_children_specs
        children = feature_children_specs(root, spec_name)
    except Exception:
        return None
    order = ["plan", "implement", "verify", "archive"]
    stages = []
    for child in children:
        item_spec = child.get("item_spec")
        if not item_spec:
            continue
        stages.append(_spec_stage(root, item_spec))
    if not stages:
        return None
    return min(stages, key=order.index)


def check_tool_call(
    *,
    tool_name: str,
    agent_role: str,
    project_root: Optional[str],
    spec_name: Optional[str],
) -> Tuple[bool, Dict[str, Any]]:
    """Return (allowed, metadata) for a requested tool invocation."""
    role = (agent_role or "orchestrator").strip().lower()
    if role not in ROLE_ALLOWLIST:
        return False, {
            "reason": f"Unknown agent role '{role}'.",
            "hint": f"Use one of: {', '.join(sorted(ROLE_ALLOWLIST))}",
            "policy": "role",
        }

    if tool_name not in ROLE_ALLOWLIST[role]:
        return False, {
            "reason": f"Tool '{tool_name}' is not allowed for role '{role}'.",
            "hint": "Switch to orchestrator role or call from the appropriate specialized agent.",
            "policy": "role",
            "role": role,
        }

    stage = infer_stage(project_root, spec_name)
    if spec_name and tool_name not in STAGE_ALLOWLIST.get(stage, set()):
        return False, {
            "reason": f"Tool '{tool_name}' is not allowed in stage '{stage}'.",
            "hint": "Follow deterministic flow plan -> implement -> verify -> archive.",
            "policy": "stage",
            "stage": stage,
        }

    return True, {"stage": stage, "role": role}
