"""Phase 4 — C3 composite driver: decision core + chained execution + E3 acceptance.

The driver only DECIDES; execution goes through an injected run_tool, so these tests
script realistic tool results and assert the chain + stop points. No network, no model.
"""
import asyncio
from typing import Any, Dict, List

import pytest

from bc_agentic_mcp import advance as decision
from bc_agentic_mcp import timeline
from bc_agentic_mcp.tools.advance import handle_advance


# ---------------------------------------------------------------------------
# Decision core (pure)
# ---------------------------------------------------------------------------

def test_seed_action_stops_at_human_and_judgment_gates():
    assert decision.seed_action("archived", {})["stop"] == "done"
    assert decision.seed_action(None, {})["stop"] == "waiting_judgment"
    assert decision.seed_action("spec_written", {})["stop"] == "waiting_judgment"
    assert decision.seed_action("approval_requested", {})["stop"] == "waiting_human_approval"
    assert decision.seed_action("decision_recorded", {})["stop"] == "waiting_judgment"
    assert decision.seed_action("review_comments_open", {})["stop"] == "waiting_rework"


def test_seed_action_deterministic_table():
    assert decision.seed_action("implemented", {})["action"] == "bc_generate_tests"
    assert decision.seed_action("tests_run", {})["action"] == "bc_verify"
    assert decision.seed_action("verified", {})["action"] == "bc_prepare_pr"
    assert decision.seed_action("pr_created", {})["action"] == "bc_get_review_comments"
    assert decision.seed_action("merged", {})["action"] == "bc_archive"


def test_seed_action_missing_inputs_stop_structured():
    step = decision.seed_action("tests_generated", {})
    assert step["stop"] == "waiting_input" and "container_name" in step["needed"]
    step = decision.seed_action("tests_generated",
                                {"container_name": "acctest", "test_extension_id": "e"})
    assert step["action"] == "bc_run_tests"
    step = decision.seed_action("pr_prepared", {})
    assert step["stop"] == "waiting_input" and "org_url" in step["needed"]


def test_next_after_result_stops():
    # blocked gate result -> structured stop, surfaces the gate's own next_action
    step = decision.next_after_result("bc_run_tests", {
        "status": "blocked_env_preflight", "blocked": True,
        "next_action": {"tool": "bc_env_preflight"}}, {})
    assert step["stop"] == "blocked" and step["next_action"]["tool"] == "bc_env_preflight"
    # review comments open -> rework
    assert decision.next_after_result(
        "bc_get_review_comments", {"status": "review_comments_open"}, {}
    )["stop"] == "waiting_rework"
    # PR approved -> merging is the human's button
    assert decision.next_after_result(
        "bc_merge_status", {"status": "approved"}, {}
    )["stop"] == "waiting_human_merge"
    # archive done
    assert decision.next_after_result("bc_archive", {"status": "closed"}, {})["stop"] == "done"


def test_next_after_result_follows_auto_safe_next_action():
    org = {"org_url": "https://x", "project": "p", "repository": "r"}
    step = decision.next_after_result(
        "bc_prepare_pr", {"status": "pr_prepared",
                          "next_action": {"tool": "bc_create_pr"}}, org)
    assert step["action"] == "bc_create_pr"
    # judgment next_action -> stop
    step = decision.next_after_result(
        "bc_generate_tests", {"status": "scaffold_generated",
                              "next_action": {"tool": "bc_implement_write"}}, {})
    assert step["stop"] == "waiting_judgment"
    # same-tool repeat -> no_progress guard
    step = decision.next_after_result(
        "bc_get_review_comments", {"status": "no_open_comments",
                                   "next_action": {"tool": "bc_get_review_comments"}}, {})
    assert step["stop"] == "no_progress"


def test_build_step_params_run_tests_and_create_pr():
    base = {"project_root": "C:/repo", "spec_name": "item-1"}
    available = {"container_name": "acctest", "test_extension_id": "ext",
                 "credential_env": "BC_TEST_PASSWORD", "app_project_folder": "C:/src/testapp",
                 "org_url": "https://x", "project": "p", "repository": "r",
                 "work_item_id": 239597}
    p = decision.build_step_params("bc_run_tests", base, available)
    assert p["container_name"] == "acctest" and p["covers"] == "all"
    assert p["app_project_folder"] == "C:/src/testapp"
    p = decision.build_step_params("bc_create_pr", base, available)
    assert p["repository"] == "r" and p["work_item_id"] == 239597
    p = decision.build_step_params("bc_verify", base, available)
    assert p == base


# ---------------------------------------------------------------------------
# Driver loop (scripted run_tool)
# ---------------------------------------------------------------------------

def _scripted_run_tool(script: Dict[str, Dict[str, Any]], calls: List[str]):
    async def run_tool(tool_name: str, **params):
        calls.append(tool_name)
        return script[tool_name]
    return run_tool


def test_advance_no_steps_in_planning_phase(tmp_path):
    timeline.record_phase(tmp_path, "item-1", "spec_written")
    out = asyncio.run(handle_advance(str(tmp_path), "item-1",
                                     run_tool=_scripted_run_tool({}, [])))
    assert out["status"] == "no_step_taken"
    assert out["stopped"] == "waiting_judgment"


def test_advance_stops_on_blocked_gate(tmp_path):
    timeline.record_phase(tmp_path, "item-1", "tests_run")
    calls: List[str] = []
    script = {"bc_verify": {"status": "blocked_x", "blocked": True,
                            "reason": "no evidence",
                            "next_action": {"tool": "bc_record_test"}}}
    out = asyncio.run(handle_advance(str(tmp_path), "item-1",
                                     run_tool=_scripted_run_tool(script, calls)))
    assert calls == ["bc_verify"]
    assert out["stopped"] == "blocked"
    assert out["next_action"]["tool"] == "bc_record_test"


def test_advance_e3_acceptance_verified_to_archived(tmp_path):
    """E3: from `verified`, the driver reaches `archived` with ZERO human actions in
    between — the only human touches in the whole lifecycle stay: plan approval (gate 1,
    before this chain), PR review votes (gate 2, in ADO) and the merge button (gate 3,
    in ADO). Everything else is one bc_advance call."""
    timeline.record_phase(tmp_path, "item-1", "verified")
    calls: List[str] = []
    script = {
        "bc_prepare_pr": {"status": "pr_prepared",
                          "next_action": {"tool": "bc_create_pr"}},
        "bc_create_pr": {"status": "pr_created",
                         "next_action": {"tool": "bc_get_review_comments"}},
        "bc_get_review_comments": {"status": "no_open_comments",
                                   "next_action": {"tool": "bc_merge_status"}},
        "bc_merge_status": {"status": "merged", "completed": True,
                            "next_action": {"tool": "bc_archive"}},
        "bc_archive": {"status": "closed"},
    }
    out = asyncio.run(handle_advance(
        str(tmp_path), "item-1", run_tool=_scripted_run_tool(script, calls),
        org_url="https://dev.azure.com/org", project="p", repository="r",
    ))
    assert calls == ["bc_prepare_pr", "bc_create_pr", "bc_get_review_comments",
                     "bc_merge_status", "bc_archive"]
    assert out["stopped"] == "done"
    # zero human/judgment tools in the chain
    assert not {"bc_submit_decision", "bc_request_approval",
                "bc_implement_write"} & set(calls)


def test_advance_rework_loop_stop(tmp_path):
    timeline.record_phase(tmp_path, "item-1", "pr_created")
    calls: List[str] = []
    script = {"bc_get_review_comments": {"status": "review_comments_open",
                                         "open_count": 2}}
    out = asyncio.run(handle_advance(str(tmp_path), "item-1",
                                     run_tool=_scripted_run_tool(script, calls)))
    assert out["stopped"] == "waiting_rework"
    assert calls == ["bc_get_review_comments"]


def test_advance_max_steps_cap(tmp_path):
    timeline.record_phase(tmp_path, "item-1", "verified")
    calls: List[str] = []

    async def run_tool(tool_name: str, **params):
        calls.append(tool_name)
        # every step claims another auto-safe follow-up: loop must respect max_steps
        return {"status": "x", "next_action": {"tool": "bc_verify" if tool_name != "bc_verify" else "bc_prepare_pr"}}

    out = asyncio.run(handle_advance(str(tmp_path), "item-1",
                                     run_tool=run_tool, max_steps=3))
    assert len(calls) == 3
    assert out["stopped"] == "max_steps"
