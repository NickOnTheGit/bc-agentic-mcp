"""Wiring invariants — bad wiring must fail the suite, not surface in production.

Born 2026-07-05 after three real wiring bugs shipped silently in one week:
- bc_record_test broken for ALL callers (_run_tool positional name collision)
- `confirm` not forwarded on three PR tools (dry-run gate unreachable)
- tool-count drift between the live registry and policy surfaces

These tests pin every wiring surface against the registered-tool ground truth:
policy allowlists, stage maps, timeline phase map, recovery exclusions, and the
@mcp.tool <-> _run_tool name agreement in server.py source.
"""
import re
from pathlib import Path

import pytest

SERVER_SRC = Path(__file__).resolve().parents[1] / "src" / "bc_agentic_mcp" / "server.py"


@pytest.fixture(scope="module")
def registered():
    from bc_agentic_mcp.server import create_server
    server = create_server()
    return {t.name for t in server._tool_manager._tools.values()}


def test_every_registered_tool_is_callable_by_some_role(registered):
    """A registered tool absent from every role allowlist is DEAD: the policy layer
    fail-closes on unknown tools, so it would be permanently blocked (the exact
    fate bc_mine_precedents escaped only because we added it to PLANNER_TOOLS)."""
    from bc_agentic_mcp import workflow_policy as wp
    allowed_somewhere = set().union(*wp.ROLE_ALLOWLIST.values())
    dead = {t for t in registered if t.startswith("bc_") or t == "_health"} - allowed_somewhere
    assert not dead, f"registered but blocked for every role: {sorted(dead)}"


def test_policy_names_ghost_no_tools(registered):
    """A policy entry for a tool that does not exist is a stale rename trap —
    it looks like protection but protects nothing."""
    from bc_agentic_mcp import workflow_policy as wp
    named = set().union(*wp.ROLE_ALLOWLIST.values())
    for stage_set in wp.STAGE_ALLOWLIST.values():
        named |= stage_set
    ghosts = {t for t in named if t.startswith("bc_") or t == "_health"} - registered
    assert not ghosts, f"policy references unregistered tools: {sorted(ghosts)}"


def test_timeline_phase_map_is_wired_to_real_tools(registered):
    from bc_agentic_mcp import timeline
    ghosts = set(timeline.TOOL_PHASE) - registered
    assert not ghosts, f"TOOL_PHASE maps unregistered tools: {sorted(ghosts)}"


def test_every_phase_has_a_human_label():
    """TIMELINE.md and the narrator must never show a raw phase id to a human."""
    from bc_agentic_mcp import timeline
    phases = set(timeline.TOOL_PHASE.values())
    # Ground-truth phases arriving via result markers rather than the static map.
    phases |= {"plan_approved", "review_comments_open", "merged"}
    unlabeled = phases - set(timeline._PHASE_LABEL)
    assert not unlabeled, f"phases without a human label: {sorted(unlabeled)}"


def test_recovery_exclusions_are_real_tools(registered):
    from bc_agentic_mcp import context_recovery
    ghosts = context_recovery.EXCLUDED_TOOLS - registered
    assert not ghosts, f"EXCLUDED_TOOLS references unregistered tools: {sorted(ghosts)}"


def test_mcp_tool_and_run_tool_names_agree():
    """The name given to _run_tool drives rate-limit, policy, audit, doom-guard and
    timeline. A mismatch with the registered @mcp.tool name silently splits those
    ledgers (bug class observed live on bc_record_test)."""
    src = SERVER_SRC.read_text(encoding="utf-8")
    pattern = re.compile(
        r'@mcp\.tool\(name="([^"]+)"\)(.*?)(?=@mcp\.tool\(name="|\Z)', re.DOTALL)
    mismatches = []
    missing_run_tool = []
    # Health probes deliberately bypass _run_tool: they return a static literal and
    # must answer even when rate-limiting/policy/state is broken — a health check
    # that can be blocked is useless. Everything else MUST go through the wall.
    deliberate_bypass = {"_health", "bc_health"}
    for name, body in pattern.findall(src):
        m = re.search(r'_run_tool\(\s*\n?\s*"([^"]+)"', body)
        if not m:
            if name not in deliberate_bypass:
                missing_run_tool.append(name)
            continue
        if m.group(1) != name:
            mismatches.append((name, m.group(1)))
    assert not mismatches, f"@mcp.tool vs _run_tool name mismatches: {mismatches}"
    # Every tool must route through _run_tool — it is the wall that applies policy,
    # audit, doom-guard, timeline, recovery. A tool bypassing it is unguarded.
    assert not missing_run_tool, f"tools not routed through _run_tool: {missing_run_tool}"


def test_stage_bearing_phases_resolve_explicitly():
    """Every phase the timeline can record must resolve to a stage deliberately —
    a phase the stage machine does not know silently downgrades the item to
    'plan' and locks out its tools (observed class: item_refined regression)."""
    from bc_agentic_mcp import timeline, workflow_policy as wp
    phases = set(timeline.TOOL_PHASE.values()) | {
        "plan_approved", "review_comments_open", "merged", "archived"}
    post_gate = (
        {"plan_approved", "implemented", "tests_generated", "tests_run", "review_comments_open"}
        | {"verified", "reviewed", "decision_recorded", "pr_prepared", "pr_created", "merged"}
        | {"archived"}
    )
    for phase in post_gate:
        stage = wp._phase_to_stage(phase)
        assert stage in {"implement", "verify", "archive"}, f"{phase} -> {stage}"
    for phase in phases - post_gate:
        assert wp._phase_to_stage(phase) == "plan", f"authoring phase {phase} escaped 'plan'"
