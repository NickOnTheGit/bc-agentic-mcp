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


# --- Poka-yoke enums (F1): loose strings are refused at the tool boundary so a typo
# can never silently record the wrong evidence class. ---

VALID_VALIDATION_MODES = {"item", "regression"}

# Canonical evidence-tier layer names accepted by the verification ladder.
VALID_EVIDENCE_LAYERS = {
    "", "heuristic", "static", "claim", "claimed",
    "empiric-compile", "compile",
    "al-unit", "al-regression", "unit", "integration",
    "empiric-runtime", "runtime", "api", "e2e",
}


def validate_validation_mode(mode: str) -> str:
    """Validate and normalize a validation mode. Returns the normalized value."""
    normalized = str(mode or "item").strip().lower()
    if normalized not in VALID_VALIDATION_MODES:
        raise ValueError(
            f"validation_mode '{mode}' must be one of: {', '.join(sorted(VALID_VALIDATION_MODES))}"
        )
    return normalized


def validate_evidence_layer(layer: str) -> str:
    """Validate and normalize an evidence layer name. Returns the normalized value."""
    normalized = str(layer or "").strip().lower()
    if normalized not in VALID_EVIDENCE_LAYERS:
        raise ValueError(
            f"layer '{layer}' must be one of: {', '.join(sorted(v for v in VALID_EVIDENCE_LAYERS if v))} (or empty)"
        )
    return normalized


def validate_covers(covers) -> None:
    """Validate a coverage declaration: 'all' or a non-empty list of positive ints."""
    if covers == "all":
        return
    if isinstance(covers, list) and covers and all(
        isinstance(i, int) and i >= 1 for i in covers
    ):
        return
    raise ValueError(
        "covers must be the string 'all' or a non-empty list of 1-based acceptance-criterion indexes"
    )
