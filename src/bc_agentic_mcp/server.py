"""BC Agentic MCP Server — main entry point with infrastructure wiring.

Target protocol: MCP 2026-07-28. Current FastMCP version may lag.

Migration checklist for full 2026-07-28 compliance:
  1. server/discover RPC handler (list all tools in discover format)
  2. Mcp-Method / Mcp-Name headers on every response
  3. Error code migration from -32002 to -32602
  4. Streamable HTTP transport support (SSE-based)
  5. ttlMs and cacheScope metadata in tools/list response

Infrastructure lifecycle:
  1. Config loaded (ServerConfig)
  2. Rate limiter instantiated per-session
  3. Audit logger instantiated (writes to .specs/.audit/)
  4. AL tool discovered (graceful degradation)
  5. All 16 tools registered with rate-limit + audit wrappers
  6. Tool integrity verification (hash-pinning, GAP 7)
"""
import argparse
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from bc_agentic_mcp import __version__
from bc_agentic_mcp.config import ServerConfig, discover_al_tool, discover_app_json
from bc_agentic_mcp.rate_limiter import RateLimiter
from bc_agentic_mcp.audit import AuditLogger
from bc_agentic_mcp.errors import MCPError, ErrorCode, error_response

# --- Tool handlers ---
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


# ---------------------------------------------------------------------------
# Tool wrapper: injects rate limiting + audit logging around every call
# ---------------------------------------------------------------------------


class ToolContext:
    """Infrastructure injected into every tool call."""

    __slots__ = ("config", "rate_limiter", "audit")

    def __init__(self, config: ServerConfig, rate_limiter: RateLimiter, audit: AuditLogger):
        self.config = config
        self.rate_limiter = rate_limiter
        self.audit = audit


_ctx: Optional[ToolContext] = None  # module-level singleton, set at boot


def _get_ctx() -> ToolContext:
    """Get the module-level ToolContext singleton."""
    assert _ctx is not None, "ToolContext not initialized — server must call create_server()"
    return _ctx


async def _run_tool(name: str, handler, session_id: str = "default", **kwargs) -> Dict[str, Any]:
    """Rate-limit, execute, audit-log, error-wrap a tool handler.

    Supports both async and sync handlers transparently.
    """
    ctx = _get_ctx()

    # 1. Rate limit check
    blocked, retry_after = ctx.rate_limiter.check(name)
    if blocked:
        return error_response(
            ErrorCode.CLIENT_ERROR,
            f"Rate limit exceeded for {name}",
            hint=f"Wait {retry_after:.1f}s before retrying",
            retry_after=int(retry_after),
        )

    # 2. Execute handler (supports both sync and async handlers)
    started = time.monotonic()
    try:
        if inspect.iscoroutinefunction(handler):
            result = await handler(**kwargs)
        else:
            result = handler(**kwargs)
        ctx.audit.log(name, session_id, success=True, duration_ms=int((time.monotonic() - started) * 1000))
        return result
    except MCPError as e:
        ctx.audit.log(name, session_id, success=False, duration_ms=int((time.monotonic() - started) * 1000))
        return error_response(e.code, e.message, hint=e.hint, details=e.details)
    except Exception as e:
        ctx.audit.log(name, session_id, success=False, duration_ms=int((time.monotonic() - started) * 1000))
        return error_response(
            ErrorCode.SERVER_ERROR,
            f"Internal error in {name}: {e}",
            hint="Retry the operation. If the error persists, report it.",
            retry_after=10,
        )


# ---------------------------------------------------------------------------
# Tool integrity verification (GAP 7)
# ---------------------------------------------------------------------------


def _verify_tool_integrity(mcp: FastMCP, specs_dir: Path) -> None:
    """Verify tool definitions haven't changed and save updated manifest."""
    try:
        from bc_agentic_mcp.tool_defense import verify_manifest, save_manifest

        all_tools = [t for t in mcp._tool_manager._tools.values()]
        tool_defs = [
            {"name": t.name, "description": t.description, "inputSchema": t.parameters}
            for t in all_tools
        ]
        integrity_dir = specs_dir / ".integrity"
        results = verify_manifest(integrity_dir, tool_defs)
        changed = {k: v for k, v in results.items() if v == "changed"}
        if changed:
            print(
                f"WARNING: {len(changed)} tool definitions changed since last approval: "
                f"{list(changed.keys())}",
                file=sys.stderr,
            )
        save_manifest(integrity_dir, tool_defs)
    except Exception as e:
        print(
            f"WARNING: Tool integrity verification failed (non-blocking): {e}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Server creation
# ---------------------------------------------------------------------------


def create_server(project_root: Optional[str] = None) -> FastMCP:
    """Create and configure the MCP server with all 16 tools + infrastructure."""
    global _ctx
    root = Path(project_root or os.getcwd()).resolve()

    # Bootstrap config
    config = ServerConfig(
        project_root=root,
        al_tool=discover_al_tool(),
        app_json_path=discover_app_json(root),
    )

    # Bootstrap infrastructure
    specs_dir = root / ".specs"
    rate_limiter = RateLimiter(
        per_tool_rate=config.per_tool_rate,
        per_session_rate=config.per_session_rate,
    )
    audit = AuditLogger(specs_dir)
    _ctx = ToolContext(config=config, rate_limiter=rate_limiter, audit=audit)

    mcp = FastMCP("bc-agentic-mcp")

    # -----------------------------------------------------------------------
    # Register all 16 tools with rate-limiting + audit wrapping
    # -----------------------------------------------------------------------

    @mcp.tool(name="bc_init")
    async def bc_init(
        project_root: Optional[str] = None,
        module_name: Optional[str] = None,
        constitution: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initialize .specs/ directory structure for BC agentic development."""
        return await _run_tool(
            "bc_init", handle_init,
            project_root=project_root or str(_get_ctx().config.project_root),
            module_name=module_name,
            constitution=constitution,
        )

    @mcp.tool(name="bc_analyze_module")
    async def bc_analyze_module(
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
        depth: str = "basic",
    ) -> Dict[str, Any]:
        """Read an AL module's structure and extract naming/patterns/dependencies."""
        return await _run_tool(
            "bc_analyze_module", analyze_module,
            module_path=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            depth=depth,
        )

    @mcp.tool(name="bc_clarify")
    async def bc_clarify(
        spec_name: str,
        context: str,
        project_root: Optional[str] = None,
        analysis: Optional[str] = None,
        specific_concern: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate structured clarification questions from human requirement bullets."""
        return await _run_tool(
            "bc_clarify", handle_clarify,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            context=context,
            analysis=analysis,
            specific_concern=specific_concern,
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
        return await _run_tool(
            "bc_write_spec", handle_write_spec,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            human_bullets=human_bullets,
            analysis=analysis,
            clarifications=clarifications,
            idempotency_key=idempotency_key,
            template=template,
        )

    @mcp.tool(name="bc_plan_design")
    async def bc_plan_design(
        spec_name: str,
        project_root: Optional[str] = None,
        machine_spec_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate the technical design (DESIGN.md + ADRs) from the machine spec."""
        return await _run_tool(
            "bc_plan_design", handle_plan_design,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            machine_spec_path=machine_spec_path,
        )

    @mcp.tool(name="bc_breakdown_tasks")
    async def bc_breakdown_tasks(
        spec_name: str,
        project_root: Optional[str] = None,
        design_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Decompose the design into dependency-ordered implementation tasks."""
        return await _run_tool(
            "bc_breakdown_tasks", handle_breakdown_tasks,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            design_path=design_path,
        )

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
        return await _run_tool(
            "bc_request_approval", handle_request_approval,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            phase=phase,
            artifact_path=artifact_path,
            summary=summary,
            idempotency_key=idempotency_key,
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
        return await _run_tool(
            "bc_submit_decision", handle_submit_decision,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            phase=phase,
            decision=decision,
            feedback=feedback,
        )

    @mcp.tool(name="bc_status")
    async def bc_status(
        project_root: Optional[str] = None,
        spec_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Show the current state of all specs (or one spec)."""
        return await _run_tool(
            "bc_status", handle_status,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_implement")
    async def bc_implement(
        spec_name: str,
        project_root: Optional[str] = None,
        task_ids: Optional[List[str]] = None,
        mode: str = "auto",
        dry_run: bool = False,
        # Phase 2: code execution
        code: Optional[str] = None,
        file_path: Optional[str] = None,
        attempt: int = 1,
        previous_diagnostics: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute implementation tasks. Phase 1 (no code): prepare context.
        Phase 2 (with code): write file, compile, return diagnostics."""
        return await _run_tool(
            "bc_implement", handle_implement,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            task_ids=task_ids,
            mode=mode,
            dry_run=dry_run,
            code=code,
            file_path=file_path,
            attempt=attempt,
            previous_diagnostics=previous_diagnostics,
        )

    @mcp.tool(name="bc_generate_tests")
    async def bc_generate_tests(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate an AL test codeunit scaffold for the spec."""
        return await _run_tool(
            "bc_generate_tests", handle_generate_tests,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_upgrade_codeunit")
    async def bc_upgrade_codeunit(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate an AL upgrade codeunit scaffold for the spec."""
        return await _run_tool(
            "bc_upgrade_codeunit", handle_upgrade_codeunit,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_converge")
    async def bc_converge(
        spec_name: str,
        project_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compare the implementation on disk against the declared spec."""
        return await _run_tool(
            "bc_converge", handle_converge,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_quality_check")
    async def bc_quality_check(
        project_root: Optional[str] = None,
        spec_name: str = "",
    ) -> Dict[str, Any]:
        """Run AL analyzers (CodeCop/AppSourceCop/UICop) via the AL MCP Server."""
        return await _run_tool(
            "bc_quality_check", handle_quality_check,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
        )

    @mcp.tool(name="bc_feedback")
    async def bc_feedback(
        spec_name: str,
        feedback: str,
        project_root: Optional[str] = None,
        rating: int = 0,
    ) -> Dict[str, Any]:
        """Record human feedback for a spec."""
        return await _run_tool(
            "bc_feedback", handle_feedback,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            feedback=feedback,
            rating=rating,
        )

    @mcp.tool(name="bc_archive")
    async def bc_archive(
        spec_name: str,
        project_root: Optional[str] = None,
        outcome: str = "merged",
    ) -> Dict[str, Any]:
        """Close out a spec with an outcome."""
        return await _run_tool(
            "bc_archive", handle_archive,
            project_root=project_root or str(_get_ctx().config.project_root),
            spec_name=spec_name,
            outcome=outcome,
        )

    # -------------------------------------------------------------------
    # GAP 8: Health check (for Docker/K8s liveness probes)
    # -------------------------------------------------------------------
    @mcp.tool(name="_health")
    async def _health() -> Dict[str, Any]:
        return {"status": "ok", "version": __version__}

    # -------------------------------------------------------------------
    # GAP 7: Tool integrity verification (hash-pinning)
    # -------------------------------------------------------------------
    _verify_tool_integrity(mcp, specs_dir)

    return mcp


def main():
    """Console-script entry point referenced by [project.scripts]."""
    parser = argparse.ArgumentParser(description="BC Agentic MCP Server")
    parser.add_argument(
        "--project-root", default=os.getcwd(), help="AL project root directory"
    )
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    if not root.is_dir():
        print(f"FATAL: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    os.chdir(str(root))
    server = create_server(str(root))
    server.run()


if __name__ == "__main__":
    main()
