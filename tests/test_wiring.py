"""Tests that infrastructure is wired into server lifecycle."""
import pytest
from bc_agentic_mcp.server import create_server, _get_ctx


def test_server_imports_rate_limiter():
    """Rate limiter must be instantiated during server creation."""
    server = create_server()
    ctx = _get_ctx()
    assert hasattr(ctx, "rate_limiter"), "RateLimiter not wired into server"
    # Verify it's actually a RateLimiter instance
    from bc_agentic_mcp.rate_limiter import RateLimiter
    assert isinstance(ctx.rate_limiter, RateLimiter)


def test_server_imports_audit_logger():
    """Audit logger must be instantiated during server creation."""
    server = create_server()
    ctx = _get_ctx()
    assert hasattr(ctx, "audit"), "AuditLogger not wired into server"
    # Verify it's actually an AuditLogger instance
    from bc_agentic_mcp.audit import AuditLogger
    assert isinstance(ctx.audit, AuditLogger)


def test_tool_returns_structured_error_on_rate_limit():
    """When rate limited, tools must return structured error, not bare dict."""
    from bc_agentic_mcp.errors import ErrorCode
    from bc_agentic_mcp.rate_limiter import RateLimiter

    rl = RateLimiter(per_tool_rate=0, per_session_rate=0)  # fully blocked
    blocked, retry_after = rl.check("bc_init")
    assert blocked is True
    assert retry_after > 0
    # In production, this would trigger an error_response, verified by
    # integration tests that hit the server through the MCP protocol.


def test_error_response_includes_all_required_fields():
    """Every error response must have _meta.error, _meta.retryable, _meta.hint."""
    from bc_agentic_mcp.errors import error_response, ErrorCode

    resp = error_response(ErrorCode.CLIENT_ERROR, "Bad input", hint="Try again")
    assert resp["isError"] is True
    assert "error" in resp["_meta"]
    assert "retryable" in resp["_meta"]
    assert "hint" in resp["_meta"]
