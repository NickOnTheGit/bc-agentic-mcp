"""Tests for input validation utilities."""
import pytest
from bc_agentic_mcp.validation import (
    validate_spec_name,
    sanitize_path,
    validate_phase,
    validate_decision,
    validate_idempotency_key,
)


class TestValidateSpecName:
    def test_valid(self):
        validate_spec_name("rental-mutation-v2")

    def test_empty(self):
        with pytest.raises(ValueError, match="spec_name"):
            validate_spec_name("")

    def test_path_traversal(self):
        with pytest.raises(ValueError, match="path traversal"):
            validate_spec_name("../escape")

    def test_null_bytes(self):
        with pytest.raises(ValueError, match="null byte"):
            validate_spec_name("fine\0corrupt")

    def test_too_long(self):
        with pytest.raises(ValueError, match="max 200"):
            validate_spec_name("x" * 201)


class TestSanitizePath:
    def test_normal_path(self):
        assert sanitize_path("src/Tables/Test.Table.al") == "src/Tables/Test.Table.al"

    def test_traversal_blocked(self):
        with pytest.raises(ValueError, match="traversal"):
            sanitize_path("../../../etc/passwd")

    def test_null_bytes_blocked(self):
        with pytest.raises(ValueError, match="null byte"):
            sanitize_path("file\0.al")


class TestValidatePhase:
    VALID_PHASES = {"spec", "design", "tasks", "implement", "complete"}

    def test_valid_phases(self):
        for phase in self.VALID_PHASES:
            validate_phase(phase, self.VALID_PHASES)

    def test_invalid_phase(self):
        with pytest.raises(ValueError, match="must be one of"):
            validate_phase("unknown", self.VALID_PHASES)


class TestValidateDecision:
    VALID = {"approve", "reject", "request_changes"}

    def test_valid_decisions(self):
        for d in self.VALID:
            validate_decision(d, self.VALID)

    def test_invalid_decision(self):
        with pytest.raises(ValueError, match="must be one of"):
            validate_decision("maybe", self.VALID)


class TestValidateIdempotencyKey:
    def test_valid(self):
        validate_idempotency_key("key-abc-123")

    def test_empty(self):
        with pytest.raises(ValueError, match="required"):
            validate_idempotency_key("")

    def test_too_long(self):
        with pytest.raises(ValueError, match="max 256"):
            validate_idempotency_key("x" * 257)
