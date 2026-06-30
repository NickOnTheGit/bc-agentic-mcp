"""Token bucket rate limiter. See spec Section 4.2."""
import time
from threading import Lock
from typing import Dict, Tuple


class TokenBucket:
    """Token bucket for rate limiting a single resource."""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.lock = Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self.last_refill = now

    @property
    def tokens(self) -> int:
        """Whole tokens currently available (refilled on read)."""
        with self.lock:
            self._refill()
            return int(self._tokens)

    def consume(self) -> bool:
        with self.lock:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False

    @property
    def wait_seconds(self) -> float:
        """Estimated seconds until next token available."""
        with self.lock:
            self._refill()
            if self._tokens >= 1:
                return 0.0
            if self.rate <= 0:
                return float("inf")
            return (1.0 - self._tokens) / self.rate


class RateLimiter:
    """Per-tool + per-session rate limiter."""

    def __init__(self, per_tool_rate: int = 30, per_session_rate: int = 120):
        self.per_tool_rate = per_tool_rate
        self.per_session_rate = per_session_rate
        self.session_bucket = TokenBucket(
            rate=per_session_rate / 60.0, capacity=per_session_rate
        )
        self.tool_buckets: Dict[str, TokenBucket] = {}
        self.lock = Lock()

    def _get_tool_bucket(self, tool_name: str) -> TokenBucket:
        with self.lock:
            if tool_name not in self.tool_buckets:
                rate = self.per_tool_rate / 60.0
                self.tool_buckets[tool_name] = TokenBucket(
                    rate=rate, capacity=self.per_tool_rate
                )
            return self.tool_buckets[tool_name]

    def is_allowed(self, tool_name: str) -> bool:
        """Check if a tool call is allowed. Returns False if rate limited."""
        if not self.session_bucket.consume():
            return False
        return self._get_tool_bucket(tool_name).consume()

    def check(self, tool_name: str) -> Tuple[bool, float]:
        """Check (non-consuming) if a call would be blocked.

        Returns (blocked, retry_after_seconds). Does not consume tokens.
        """
        session_wait = self.session_bucket.wait_seconds
        tool_wait = self._get_tool_bucket(tool_name).wait_seconds
        blocked = session_wait > 0 or tool_wait > 0
        return blocked, max(session_wait, tool_wait)
