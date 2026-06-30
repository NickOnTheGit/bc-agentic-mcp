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
    """All 16 tools must be registered."""
    from bc_agentic_mcp.server import create_server
    server = create_server()
    tool_names = {t.name for t in server._tool_manager._tools.values()}
    expected = {
        "bc_init",
        "bc_analyze_module",
        "bc_clarify",
        "bc_write_spec",
        "bc_plan_design",
        "bc_breakdown_tasks",
        "bc_request_approval",
        "bc_submit_decision",
        "bc_status",
        "bc_implement",
        "bc_generate_tests",
        "bc_upgrade_codeunit",
        "bc_converge",
        "bc_quality_check",
        "bc_feedback",
        "bc_archive",
    }
    assert expected <= tool_names
    assert len(tool_names) == 16


@pytest.mark.asyncio
async def test_bc_init_creates_specs_dir(tmp_path):
    """bc_init should create .specs/ structure."""
    from bc_agentic_mcp.tools.init import handle_init

    result = await handle_init(project_root=str(tmp_path))
    assert result["success"] is True
    assert "created_paths" in result
    assert (tmp_path / ".specs" / "state.json").exists()
    assert (tmp_path / ".specs" / "CONSTITUTION.md").exists()
