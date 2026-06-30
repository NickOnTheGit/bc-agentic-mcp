"""Input validation utilities. See spec Section 4.4."""
import os
from typing import Set


MAX_SPEC_NAME_LEN = 200
MAX_IDEMPOTENCY_KEY_LEN = 256


def validate_spec_name(name: str) -> None:
    """Validate a spec name: no path traversal, no null bytes, reasonable length."""
    if not name or not name.strip():
        raise ValueError("spec_name is required and must not be empty")
    if ".." in name:
        raise ValueError("spec_name must not contain path traversal (..)")
    if "\0" in name:
        raise ValueError("spec_name must not contain null bytes")
    if len(name) > MAX_SPEC_NAME_LEN:
        raise ValueError(f"spec_name max {MAX_SPEC_NAME_LEN} chars, got {len(name)}")


def sanitize_path(path: str) -> str:
    """Validate a file path: block absolute paths, traversal, and null bytes."""
    if os.path.isabs(path):
        raise ValueError("path must be relative, not absolute")
    if ".." in path:
        raise ValueError("path must not contain traversal (..)")
    if "\0" in path:
        raise ValueError("path must not contain null bytes")
    return path


def validate_phase(phase: str, valid_phases: Set[str]) -> None:
    """Validate that phase is one of the allowed values."""
    if phase not in valid_phases:
        raise ValueError(f"phase '{phase}' must be one of: {', '.join(sorted(valid_phases))}")


def validate_decision(decision: str, valid_decisions: Set[str]) -> None:
    """Validate that decision is one of the allowed values."""
    if decision not in valid_decisions:
        raise ValueError(f"decision '{decision}' must be one of: {', '.join(sorted(valid_decisions))}")


def validate_idempotency_key(key: str) -> None:
    """Validate an idempotency key."""
    if not key or not key.strip():
        raise ValueError("idempotency_key is required")
    if len(key) > MAX_IDEMPOTENCY_KEY_LEN:
        raise ValueError(f"idempotency_key max {MAX_IDEMPOTENCY_KEY_LEN} chars, got {len(key)}")
