"""Structured error responses for bc-agentic-mcp.
See spec Section 4.1.
"""
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(Enum):
    CLIENT_ERROR = "CLIENT_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    EXTERNAL_ERROR = "EXTERNAL_ERROR"
    SCOPE_ERROR = "SCOPE_ERROR"


RETRYABLE_CODES = {ErrorCode.SERVER_ERROR, ErrorCode.EXTERNAL_ERROR}


class MCPError(Exception):
    """Structured MCP error with metadata for agent guidance."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str = "",
        retry_after: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.retryable = code in RETRYABLE_CODES
        self.hint = hint
        self.retry_after = retry_after
        self.details = details or {}
        super().__init__(message)


def error_response(
    code: ErrorCode,
    message: str,
    *,
    hint: str = "",
    retry_after: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a structured MCP error response.

    Returns a dict that the MCP framework serializes as a tool error.
    The _meta field provides agent-actionable guidance.
    """
    is_retryable = code in RETRYABLE_CODES
    meta: Dict[str, Any] = {
        "error": code.value,
        "retryable": is_retryable,
        "hint": hint,
    }
    if retry_after is not None:
        meta["retry_after"] = retry_after
    if details:
        meta["details"] = details

    return {
        "content": [{"type": "text", "text": f"{code.value}: {message}"}],
        "isError": True,
        "_meta": meta,
    }
