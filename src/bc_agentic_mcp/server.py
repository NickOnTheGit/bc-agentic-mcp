"""BC Agentic MCP Server — main entry point. See spec Section 2.1.

Uses FastMCP (mcp.server.fastmcp). Tools are thin wrappers that delegate to the
handlers in bc_agentic_mcp.tools.*; all scope/ID/schema enforcement lives in the
handlers and helper modules so it is independent of model quality.
"""
import argparse
import os
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from bc_agentic_mcp.tools.init import handle_init
from bc_agentic_mcp.tools.analyze import analyze_module
from bc_agentic_mcp.tools.clarify import handle_clarify
from bc_agentic_mcp.tools.write_spec import handle_write_spec
from bc_agentic_mcp.tools.plan_design import handle_plan_design
from bc_agentic_mcp.tools.breakdown_tasks import handle_breakdown_tasks
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision
from bc_agentic_mcp.tools.status import handle_status
from bc_agentic_mcp.tools.implement import handle_implement
from bc_agentic_mcp.tools.generate_tests import handle_generate_tests
from bc_agentic_mcp.tools.upgrade_codeunit import handle_upgrade_codeunit
from bc_agentic_mcp.tools.converge import handle_converge
from bc_agentic_mcp.tools.quality_check import handle_quality_check
from bc_agentic_mcp.tools.feedback import handle_feedback
from bc_agentic_mcp.tools.archive import handle_archive


def _cwd() -> str:
    return os.getcwd()


def create_server() -> FastMCP:
    """Create and configure the MCP server with all 16 tools."""
    mcp = FastMCP("bc-agentic-mcp")

    @mcp.tool(name="bc_init")
    async def bc_init(
        project_root: Optional[str] = None,
        module_name: Optional[str] = None,
        constitution: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initialize .specs/ directory structure for BC agentic development."""
        return await handle_init(project_root or _cwd(), module_name, constitution)

    @mcp.tool(name="bc_analyze_module")
    async def bc_analyze_module(
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
        depth: str = "basic",
    ) -> Dict[str, Any]:
        """Read an AL module's structure and extract naming/patterns/dependencies."""
        return analyze_module(project_root or _cwd(), spec_name, depth)

    @mcp.tool(name="bc_clarify")
    async def bc_clarify(
        spec_name: str,
        context: str,
        project_root: Optional[str] = None,
        analysis: Optional[str] = None,
        specific_concern: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate structured clarification questions from human requirement bullets."""
        return await handle_clarify(
            project_root or _cwd(), spec_name, context, analysis, specific_concern
        )

    @mcp.tool(name="bc_write_spec")
    async def bc_write_spec(
        spec_name: str,
        human_bullets: str,
        idempotency_key: str,
        project_root: Optional[str] = None,
        analysis: Optional[str] = None,
        clarifications: Optional[str] = None,
        template: str = "tdd",
    ) -> Dict[str, Any]:
        """Generate a TDD and machine-consumable spec from human bullets."""
        return await handle_write_spec(
            project_root or _cwd(),
            spec_name,
            human_bullets,
            analysis,
            clarifications,
            idempotency_key,
            template,
        )

    @mcp.tool(name="bc_plan_design")
    async def bc_plan_design(
        spec_name: str,
        project_root: Optional[str] = None,
        machine_spec_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate the technical design (DESIGN.md + ADRs) from the machine spec."""
        return await handle_plan_design(project_root or _cwd(), spec_name, machine_spec_path)

    @mcp.tool(name="bc_breakdown_tasks")
    async def bc_breakdown_tasks(
        spec_name: str,
        project_root: Optional[str] = None,
        design_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Decompose the design into dependency-ordered implementation tasks."""
        return await handle_breakdown_tasks(project_root or _cwd(), spec_name, design_path)

    @mcp.tool(name="bc_request_approval")
    async def bc_request_approval(
        spec_name: str,
        phase: str,
        artifact_path: str,
        summary: str,
        idempotency_key: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a phase artifact for human review."""
        return await handle_request_approval(
            project_root or _cwd(), spec_name, phase, artifact_path, summary, idempotency_key
        )

    @mcp.tool(name="bc_submit_decision")
    async def bc_submit_decision(
        spec_name: str,
        phase: str,
        decision: str,
        project_root: Optional[str] = None,
        feedback: str = "",
    ) -> Dict[str, Any]:
        """Record the human's decision on a pending approval."""
        return await handle_submit_decision(
            project_root or _cwd(), spec_name, phase, decision, feedback
        )

    @mcp.tool(name="bc_status")
    async def bc_status(
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Show the current state of all specs (or one spec)."""
        return await handle_status(project_root or _cwd(), spec_name)

    @mcp.tool(name="bc_implement")
    async def bc_implement(
        spec_name: str,
        project_root: Optional[str] = None,
        task_ids: Optional[List[str]] = None,
        mode: str = "auto",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Execute implementation tasks from TASKS.md within scope boundaries."""
        return await handle_implement(project_root or _cwd(), spec_name, task_ids, mode, dry_run)

    @mcp.tool(name="bc_generate_tests")
    async def bc_generate_tests(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate an AL test codeunit scaffold for the spec."""
        return await handle_generate_tests(project_root or _cwd(), spec_name)

    @mcp.tool(name="bc_upgrade_codeunit")
    async def bc_upgrade_codeunit(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate an AL upgrade codeunit scaffold for the spec."""
        return await handle_upgrade_codeunit(project_root or _cwd(), spec_name)

    @mcp.tool(name="bc_converge")
    async def bc_converge(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare the implementation on disk against the declared spec."""
        return await handle_converge(project_root or _cwd(), spec_name)

    @mcp.tool(name="bc_quality_check")
    async def bc_quality_check(
        project_root: Optional[str] = None,
        spec_name: str = "",
    ) -> Dict[str, Any]:
        """Run AL analyzers (CodeCop/AppSourceCop/UICop) via the AL MCP Server."""
        return await handle_quality_check(project_root or _cwd(), spec_name)

    @mcp.tool(name="bc_feedback")
    async def bc_feedback(
        spec_name: str,
        feedback: str,
        project_root: Optional[str] = None,
        rating: int = 0,
    ) -> Dict[str, Any]:
        """Record human feedback for a spec."""
        return await handle_feedback(project_root or _cwd(), spec_name, feedback, rating)

    @mcp.tool(name="bc_archive")
    async def bc_archive(
        spec_name: str,
        project_root: Optional[str] = None,
        outcome: str = "merged",
    ) -> Dict[str, Any]:
        """Close out a spec with an outcome."""
        return await handle_archive(project_root or _cwd(), spec_name, outcome)

    return mcp


def main():
    """Console-script entry point referenced by [project.scripts]."""
    parser = argparse.ArgumentParser(description="BC Agentic MCP Server")
    parser.add_argument(
        "--project-root", default=os.getcwd(), help="AL project root directory"
    )
    args = parser.parse_args()
    os.chdir(args.project_root)

    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
