"""Tests for structured error handling."""
from bc_agentic_mcp.errors import ErrorCode, MCPError, error_response


class TestErrorCode:
    def test_all_codes_are_strings(self):
        for code in ErrorCode:
            assert isinstance(code.value, str)

    def test_codes_include_all_categories(self):
        values = {c.value for c in ErrorCode}
        assert "CLIENT_ERROR" in values
        assert "SERVER_ERROR" in values
        assert "EXTERNAL_ERROR" in values
        assert "SCOPE_ERROR" in values


class TestMCPError:
    def test_basic_error(self):
        err = MCPError(
            code=ErrorCode.CLIENT_ERROR,
            message="Spec not found",
            hint="Run bc_status to see available specs",
        )
        assert err.code == ErrorCode.CLIENT_ERROR
        assert err.retryable is False

    def test_default_retryable(self):
        assert MCPError(code=ErrorCode.SERVER_ERROR, message="").retryable is True
        assert MCPError(code=ErrorCode.EXTERNAL_ERROR, message="").retryable is True
        assert MCPError(code=ErrorCode.CLIENT_ERROR, message="").retryable is False
        assert MCPError(code=ErrorCode.SCOPE_ERROR, message="").retryable is False


class TestErrorResponse:
    def test_minimal_response(self):
        result = error_response(
            ErrorCode.CLIENT_ERROR,
            "Spec 'xyz' not found",
            hint="Run bc_status",
        )
        assert result["isError"] is True
        assert "xyz" in result["content"][0]["text"]
        meta = result["_meta"]
        assert meta["error"] == "CLIENT_ERROR"
        assert meta["retryable"] is False
        assert meta["hint"] == "Run bc_status"

    def test_with_details(self):
        result = error_response(
            ErrorCode.SCOPE_ERROR,
            "File outside scope",
            hint="Expand scope or use alternative",
            details={"file": "src/Other.Table.al", "scope": ["EmpireRental"]},
        )
        assert result["_meta"]["details"]["file"] == "src/Other.Table.al"

    def test_retryable_with_delay(self):
        result = error_response(
            ErrorCode.EXTERNAL_ERROR,
            "AL MCP Server timeout",
            hint="Retry in 30s",
            retry_after=30,
        )
        assert result["_meta"]["retryable"] is True
        assert result["_meta"]["retry_after"] == 30
