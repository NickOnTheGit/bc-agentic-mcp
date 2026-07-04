"""Smoke test for MCP server."""
import pytest


def test_server_imports():
    """Server module must import without errors."""
    from bc_agentic_mcp.server import create_server
    assert callable(create_server)


def test_init_tool_exists():
    """bc_init tool must be registered."""
    from bc_agentic_mcp.server import create_server
    server = create_server()
    tool_names = [t.name for t in server._tool_manager._tools.values()]
    assert "bc_init" in tool_names


def test_all_sixteen_tools_registered():
    """All core tools must be registered."""
    from bc_agentic_mcp.server import create_server
    server = create_server()
    tool_names = {t.name for t in server._tool_manager._tools.values()}
    expected = {
        "bc_init",
        "bc_analyze_module",
        "bc_clarify",
        "bc_answer_clarification",
        "bc_write_spec",
        "bc_plan_design",
        "bc_breakdown_tasks",
        "bc_prepare_review",
        "bc_request_approval",
        "bc_submit_decision",
        "bc_status",
        "bc_recall",
        "bc_checkpoint",
        "bc_record_test",
        "bc_verify",
        "bc_implement",
        "bc_generate_tests",
        "bc_upgrade_codeunit",
        "bc_converge",
        "bc_quality_check",
        "bc_feedback",
        "bc_lessons",
        "bc_archive",
        "bc_run_tests",
        "bc_api_contract",
        "bc_reconcile_target",
        "bc_upgrade_preflight",
        "bc_find_consumers",
        "bc_promote_lesson",
        "bc_extract_references",
        "bc_fetch_wiki",
        "bc_capture_item_context",
        "bc_check_permission_coverage",
        "bc_reflect",
        "bc_guard_pr_thread_resolution",
        "bc_detect",
        "bc_review",
        "bc_read_code_context",
        "bc_timeline",
        "bc_worktree",
        "bc_intake_start",
        "bc_intake_add",
        "bc_intake_analyze",
        "bc_intake_graduate",
        "bc_analyze_consistency",
        "bc_tool_health",
        "bc_push_items",
        "_health",
    }
    assert expected <= tool_names
    assert len(tool_names) == 71

@pytest.mark.asyncio
async def test_bc_init_creates_specs_dir(tmp_path):
    """bc_init should create .specs/ structure."""
    from bc_agentic_mcp.tools.init import handle_init

    result = await handle_init(project_root=str(tmp_path))
    assert result["success"] is True
    assert "created_paths" in result
    assert (tmp_path / ".specs" / "state.json").exists()
    assert (tmp_path / ".specs" / "CONSTITUTION.md").exists()
