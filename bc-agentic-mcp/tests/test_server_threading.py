"""Tests for server execution plumbing."""
import asyncio
import tempfile
import time
from pathlib import Path

import bc_agentic_mcp.server as server_module
from bc_agentic_mcp.audit import AuditLogger
from bc_agentic_mcp.config import ServerConfig
from bc_agentic_mcp.rate_limiter import RateLimiter
from bc_agentic_mcp.server import ToolContext, _run_tool


def test_sync_handlers_do_not_block_event_loop():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        original_ctx = server_module._ctx
        server_module._ctx = ToolContext(
            config=ServerConfig(project_root=root),
            rate_limiter=RateLimiter(per_tool_rate=1000, per_session_rate=1000),
            audit=AuditLogger(root / ".specs"),
        )

        async def exercise() -> dict:
            def slow_handler() -> dict:
                time.sleep(0.15)
                return {"ok": True}

            started = time.monotonic()
            task = asyncio.create_task(_run_tool("slow_tool", slow_handler))
            await asyncio.sleep(0.02)
            elapsed = time.monotonic() - started
            assert elapsed < 0.1
            assert not task.done()
            result = await task
            # F4 envelope adds stage/artifacts; the handler's own payload is preserved.
            assert result["ok"] is True
            assert result["stage"] == "plan" and result["artifacts"] == []
            return result

        try:
            asyncio.run(exercise())
        finally:
            server_module._ctx = original_ctx


def test_run_tool_accepts_handler_kwarg_named_name():
    """bc_record_test passes name=<test name> — the seam's own first parameter
    must not collide with handler kwargs (observed live: EVERY bc_record_test
    call failed with \"got multiple values for argument 'name'\")."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        original_ctx = server_module._ctx
        server_module._ctx = ToolContext(
            config=ServerConfig(project_root=root),
            rate_limiter=RateLimiter(per_tool_rate=1000, per_session_rate=1000),
            audit=AuditLogger(root / ".specs"),
        )

        async def exercise() -> None:
            def handler(name: str, result: str) -> dict:
                return {"ok": True, "echo": name, "result": result}

            out = await _run_tool("record_test_probe", handler,
                                  name="MyTest", result="pass")
            assert out["ok"] is True and out["echo"] == "MyTest"

        try:
            asyncio.run(exercise())
        finally:
            server_module._ctx = original_ctx


def test_sync_handler_timeout_returns_structured_error():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        original_ctx = server_module._ctx
        server_module._ctx = ToolContext(
            config=ServerConfig(project_root=root, tool_timeout_seconds=1),
            rate_limiter=RateLimiter(per_tool_rate=1000, per_session_rate=1000),
            audit=AuditLogger(root / ".specs"),
        )

        async def exercise() -> dict:
            def slow_handler() -> dict:
                time.sleep(1.25)
                return {"ok": True}

            result = await _run_tool("slow_tool", slow_handler)
            assert result["isError"] is True
            assert result["_meta"]["error"] == "SERVER_ERROR"
            assert result["_meta"]["retry_after"] == 1
            return result

        try:
            asyncio.run(exercise())
        finally:
            server_module._ctx = original_ctx


def test_run_tool_normalizes_relative_path_fields_to_absolute():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        original_ctx = server_module._ctx
        server_module._ctx = ToolContext(
            config=ServerConfig(project_root=root),
            rate_limiter=RateLimiter(per_tool_rate=1000, per_session_rate=1000),
            audit=AuditLogger(root / ".specs"),
        )

        async def exercise() -> dict:
            def handler(project_root: str) -> dict:
                return {
                    "artifact": "docs/diagrams/bc-mcp-lifecycle.png",
                    "artifacts": ["AGENTS.md", "./.github/copilot-instructions.md"],
                    "nested": {"report_path": "reports/out.json"},
                }

            result = await _run_tool("path_tool", handler, project_root=str(root))
            assert result["artifact"] == (root / "docs/diagrams/bc-mcp-lifecycle.png").resolve().as_posix()
            assert result["artifacts"][0] == (root / "AGENTS.md").resolve().as_posix()
            assert result["artifacts"][1] == (root / ".github/copilot-instructions.md").resolve().as_posix()
            assert result["nested"]["report_path"] == (root / "reports/out.json").resolve().as_posix()
            return result

        try:
            asyncio.run(exercise())
        finally:
            server_module._ctx = original_ctx


def test_run_tool_does_not_rewrite_external_references():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        original_ctx = server_module._ctx
        server_module._ctx = ToolContext(
            config=ServerConfig(project_root=root),
            rate_limiter=RateLimiter(per_tool_rate=1000, per_session_rate=1000),
            audit=AuditLogger(root / ".specs"),
        )

        async def exercise() -> dict:
            def handler(project_root: str) -> dict:
                return {
                    "path": "https://example.test/file.md",
                    "artifact": "vstfs:///Build/Build/123",
                }

            result = await _run_tool("path_tool", handler, project_root=str(root))
            assert result["path"] == "https://example.test/file.md"
            assert result["artifact"] == "vstfs:///Build/Build/123"
            return result

        try:
            asyncio.run(exercise())
        finally:
            server_module._ctx = original_ctx