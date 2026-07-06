"""attempts — the deterministic research→try→fail→retry→succeed→learn loop.

Every risky tool call follows one cycle, enforced by the server (not by prose):

* TRY      — the attempt is recorded with a stable param fingerprint.
* FAIL     — the error is classified into a small deterministic taxonomy and appended
             to ``.specs/<item>/failed_approaches.jsonl``.
* RETRY    — an identical fingerprint that already failed ``MAX_IDENTICAL_FAILURES``
             times is REFUSED with the prior errors, forcing a changed approach
             (the doom-loop killer observed in the WI 239597 session).
* SUCCEED  — the fingerprint's failure streak resets.
* LEARN    — a success that follows recorded failures auto-records a ``correction``
             checkpoint, which triggers the existing ``reflection_due`` machinery so
             the delta becomes a durable lesson without anyone having to remember.

Design rules (poka-yoke): pure logic, injectable clock, no secrets in the ledger
(param values are hashed, never stored raw), fail-open on ledger I/O errors so a
broken disk never blocks delivery — only the guard is skipped, loudly.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root

# A fingerprint must fail this many times before the identical retry is refused.
MAX_IDENTICAL_FAILURES = 2

# EVIDENCE runs (executed=True, some tests red) are the test-fix-retest LOOP working
# as designed — the world usually changed between runs on OTHER ledgers (a dependency
# published, a sibling spec's file fixed) which active_failures() cannot see. Refusing
# the identical re-run forced param-noise workarounds (observed live three times on
# feature 239584: tenant="default" padding, id→name switching). Genuine infra failures
# (license, publish, auth…) keep the strict threshold.
MAX_EVIDENCE_RUN_FAILURES = 5
_EVIDENCE_RUN_CLASS = "evidence-run"

LEDGER_FILENAME = "failed_approaches.jsonl"

# Tools whose failures indicate an environment/approach problem worth guarding.
# Read-only/query tools are exempt: retrying a status call is harmless.
GUARDED_TOOLS = {
    "bc_implement",
    "bc_implement_write",
    "bc_run_tests",
    "bc_api_contract",
    "bc_quality_check",
    "bc_upgrade_codeunit",
    "bc_generate_tests",
    "bc_create_pr",
    "bc_resolve_review_comment",
    "bc_sync_item_state",
}

# Deterministic error taxonomy: first match wins. Patterns are matched case-insensitively
# against the combined error message text.
_ERROR_CLASSES: List[tuple] = [
    ("license", re.compile(r"license|\.bclicense|\.flf\b", re.IGNORECASE)),
    ("dependency-symbol", re.compile(r"AL1024|could not be loaded|symbols for the requested app", re.IGNORECASE)),
    ("missing-object", re.compile(r"AL0185|does not contain a definition|AL0118|AL0132", re.IGNORECASE)),
    ("schema-mismatch", re.compile(r"AL0122|cannot implicitly convert|field .* does not exist", re.IGNORECASE)),
    ("publish", re.compile(r"publish|sync-navapp|install-navapp|dev/apps", re.IGNORECASE)),
    ("auth", re.compile(r"401|403|unauthorized|forbidden|credential|PAT\b", re.IGNORECASE)),
    ("timeout", re.compile(r"timeout|timed out", re.IGNORECASE)),
    ("container", re.compile(r"container|docker|rolled back|service tier", re.IGNORECASE)),
]

_VOLATILE_KEYS = {"idempotency_key", "session_id"}


def classify_error(message: str) -> str:
    """Map an error message to a deterministic class. Unknown -> 'other'."""
    text = str(message or "")
    if text.startswith("evidence run red"):
        return _EVIDENCE_RUN_CLASS
    for name, pattern in _ERROR_CLASSES:
        if pattern.search(text):
            return name
    return "other"


def param_fingerprint(tool: str, kwargs: Dict[str, Any]) -> str:
    """Stable fingerprint of an attempt: tool + hashed, order-independent params.

    Param VALUES are hashed (sha256, truncated) so secrets/paths never enter the
    ledger; volatile keys (idempotency, session) are excluded so a re-key does not
    masquerade as a new approach.
    """
    parts: List[str] = [tool]
    for key in sorted(kwargs):
        if key in _VOLATILE_KEYS:
            continue
        value = kwargs[key]
        try:
            raw = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = str(value)
        parts.append(f"{key}={hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _ledger_path(project_root: Path, spec_name: str) -> Path:
    return specs_root(Path(project_root).resolve()) / spec_name / LEDGER_FILENAME


def _load(project_root: Path, spec_name: str) -> List[Dict[str, Any]]:
    path = _ledger_path(project_root, spec_name)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries


def _append(project_root: Path, spec_name: str, entry: Dict[str, Any]) -> None:
    path = _ledger_path(project_root, spec_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except OSError:
        # Fail-open: a broken ledger disk must never block delivery.
        pass


def active_failures(project_root: Path, spec_name: str, fingerprint: str) -> List[Dict[str, Any]]:
    """Failures for this fingerprint since the last success of ANY guarded tool.

    A success anywhere in the item means the world changed (new code written, new
    app published), so prior identical-call failures are no longer proof that the
    approach is doomed — the streak resets for every fingerprint, not just the one
    that succeeded. This keeps the guard aimed at true doom loops: identical retries
    with nothing succeeding in between (the observed WI 239597 publish loop).
    """
    streak: List[Dict[str, Any]] = []
    for entry in _load(project_root, spec_name):
        if entry.get("outcome") == "success":
            streak = []
            continue
        if entry.get("fingerprint") == fingerprint:
            streak.append(entry)
    return streak


def check_attempt(
    project_root: Optional[str],
    spec_name: Optional[str],
    tool: str,
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Doom-loop guard. Returns {allowed, fingerprint, prior_failures, refusal?}.

    An identical fingerprint with >= MAX_IDENTICAL_FAILURES active failures is refused
    with the prior error classes/messages, forcing a changed approach.
    """
    fingerprint = param_fingerprint(tool, kwargs)
    if not project_root or not spec_name or tool not in GUARDED_TOOLS:
        return {"allowed": True, "fingerprint": fingerprint, "prior_failures": []}
    streak = active_failures(Path(project_root), spec_name, fingerprint)
    # Red EVIDENCE runs earn a longer leash: the fix-retest loop legitimately
    # re-issues the identical call after out-of-ledger fixes.
    limit = (
        MAX_EVIDENCE_RUN_FAILURES
        if streak and all(e.get("error_class") == _EVIDENCE_RUN_CLASS for e in streak)
        else MAX_IDENTICAL_FAILURES
    )
    if len(streak) >= limit:
        classes = sorted({e.get("error_class", "other") for e in streak})
        return {
            "allowed": False,
            "fingerprint": fingerprint,
            "prior_failures": streak[-MAX_IDENTICAL_FAILURES:],
            "refusal": {
                "reason": (
                    f"This exact approach already failed {len(streak)} times "
                    f"(error classes: {', '.join(classes)}). Change the approach — "
                    "different parameters, a preflight fix, or a different tool — "
                    "instead of retrying the identical call."
                ),
                "error_classes": classes,
                "last_error": streak[-1].get("message", ""),
            },
        }
    return {"allowed": True, "fingerprint": fingerprint, "prior_failures": streak}


def record_failure(
    project_root: Optional[str],
    spec_name: Optional[str],
    tool: str,
    fingerprint: str,
    message: str,
) -> Dict[str, Any]:
    """FAIL step: classify + append to the ledger. Returns the entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "fingerprint": fingerprint,
        "outcome": "failure",
        "error_class": classify_error(message),
        "message": str(message or "")[:500],
    }
    if project_root and spec_name:
        _append(Path(project_root), spec_name, entry)
    return entry


def result_gate_blocked(result: Any) -> bool:
    """True when a result is a prerequisite gate-block (neutral for the learn loop:
    neither a failed approach nor a success that clears failure streaks)."""
    if not isinstance(result, dict):
        return False
    return result.get("blocked") is True or str(result.get("status", "")).startswith("blocked")


# Repeated POLICY/GATE refusals of the IDENTICAL call are invisible to the failure
# ledger by design (a gate says "fix the prerequisite, retry the same call") — but a
# weak agent hammering the same refused call forever is a live doom loop (repeater
# persona finding 2026-07-06). Track refusal streaks in-memory per fingerprint and
# escalate the guidance once the streak passes the leash.
_REFUSAL_STREAKS: Dict[str, int] = {}
MAX_IDENTICAL_REFUSALS = 3


def note_refusal(fingerprint: str) -> int:
    """Record one more refusal of this exact call; returns the streak length."""
    _REFUSAL_STREAKS[fingerprint] = _REFUSAL_STREAKS.get(fingerprint, 0) + 1
    return _REFUSAL_STREAKS[fingerprint]


def clear_refusals(fingerprint: str) -> None:
    _REFUSAL_STREAKS.pop(fingerprint, None)


def result_failure_signal(result: Any) -> Optional[str]:
    """Detect a failure expressed as a structured result (no exception raised).

    Deterministic signals only: explicit error markers, or an executed test run
    that did not pass. GATE BLOCKS (``blocked``/``status: blocked_*``) are NOT
    failures — a gate says "satisfy the prerequisite, then retry the SAME call",
    and counting them would make the doom-loop guard refuse that legitimate retry
    after the environment was fixed externally.
    """
    if not isinstance(result, dict):
        return None
    if result.get("blocked") is True or str(result.get("status", "")).startswith("blocked"):
        return None  # prerequisite gate, not a failed approach
    if result.get("isError") is True:
        return str(result.get("content", ""))[:300] or "tool returned isError"
    if result.get("executed") is True and result.get("all_passed") is False:
        return (
            f"evidence run red: passed={result.get('passed')}/{result.get('total')} "
            f"exit={result.get('exit_code')}"
        )
    return None


def record_success(
    project_root: Optional[str],
    spec_name: Optional[str],
    tool: str,
    fingerprint: str,
) -> Dict[str, Any]:
    """SUCCEED/LEARN step: reset the streak; report whether this is a recovery.

    A success that follows recorded failures is a *recovery* — the caller should
    record a ``correction`` checkpoint so the existing reflection machinery turns
    the delta into a durable lesson.
    """
    recovered_from: List[Dict[str, Any]] = []
    if project_root and spec_name:
        recovered_from = active_failures(Path(project_root), spec_name, fingerprint)
        _append(
            Path(project_root),
            spec_name,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool": tool,
                "fingerprint": fingerprint,
                "outcome": "success",
            },
        )
    return {
        "recovered": bool(recovered_from),
        "recovered_from": [
            {"error_class": e.get("error_class"), "message": e.get("message", "")[:160]}
            for e in recovered_from
        ],
    }
