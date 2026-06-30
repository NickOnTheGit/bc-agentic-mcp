"""Tests for rate limiter."""
import time
from bc_agentic_mcp.rate_limiter import TokenBucket, RateLimiter


class TestTokenBucket:
    def test_initial_tokens(self):
        tb = TokenBucket(rate=10, capacity=10)
        assert tb.tokens == 10

    def test_consume_allowed(self):
        tb = TokenBucket(rate=10, capacity=10)
        assert tb.consume() is True
        assert tb.tokens == 9

    def test_consume_until_exhausted(self):
        tb = TokenBucket(rate=5, capacity=5)
        results = [tb.consume() for _ in range(6)]
        assert results == [True, True, True, True, True, False]

    def test_refill_over_time(self):
        tb = TokenBucket(rate=10, capacity=10)
        for _ in range(10):
            tb.consume()
        assert tb.tokens == 0
        time.sleep(0.3)
        assert tb.tokens > 0


class TestRateLimiter:
    def test_per_tool_limit(self):
        rl = RateLimiter(per_tool_rate=2, per_session_rate=100)
        assert rl.is_allowed("bc_implement") is True
        assert rl.is_allowed("bc_implement") is True
        assert rl.is_allowed("bc_implement") is False

    def test_per_session_limit(self):
        rl = RateLimiter(per_tool_rate=100, per_session_rate=2)
        assert rl.is_allowed("bc_init") is True
        assert rl.is_allowed("bc_status") is True
        assert rl.is_allowed("bc_analyze_module") is False

    def test_retry_after(self):
        rl = RateLimiter(per_tool_rate=1, per_session_rate=100)
        rl.is_allowed("bc_implement")  # consume
        blocked, retry_after = rl.check("bc_implement")
        assert blocked is True
        assert retry_after > 0

    def test_different_tools_independent(self):
        rl = RateLimiter(per_tool_rate=1, per_session_rate=100)
        rl.is_allowed("bc_implement")  # exhaust bc_implement
        assert rl.is_allowed("bc_init") is True  # different tool, own tokens
        assert rl.is_allowed("bc_implement") is False
