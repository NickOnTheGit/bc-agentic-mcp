"""ears — validate requirement statements against EARS syntax (Mavin, Rolls-Royce).

Patterns (fixed clause order), all containing the keyword `shall`:
  Unwanted:   If <trigger>, then the <system> shall <response>
  Complex:    While <precondition>, When <trigger>, the <system> shall <response>
  State:      While <precondition>, the <system> shall <response>
  Event:      When <trigger>, the <system> shall <response>
  Optional:   Where <feature>, the <system> shall <response>
  Ubiquitous: The <system> shall <response>
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_PATTERNS = [
    ("unwanted", re.compile(r"(?is)^\s*if\b.*\bthen\b.*\bshall\b")),
    ("complex", re.compile(r"(?is)^\s*while\b.*\bwhen\b.*\bshall\b")),
    ("state", re.compile(r"(?is)^\s*while\b.*\bshall\b")),
    ("event", re.compile(r"(?is)^\s*when\b.*\bshall\b")),
    ("optional", re.compile(r"(?is)^\s*where\b.*\bshall\b")),
    ("ubiquitous", re.compile(r"(?is)\bshall\b")),
]


def classify(statement: str) -> Optional[str]:
    """Return the EARS pattern name a statement matches, or None if it is not EARS-shaped."""
    for name, pat in _PATTERNS:
        if pat.search(statement or ""):
            return name
    return None


def lint(requirements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Check each requirement's statement is EARS-shaped. Returns ok + violations."""
    violations: List[Dict[str, Any]] = []
    for r in requirements or []:
        stmt = r.get("statement", "")
        if classify(stmt) is None:
            violations.append({
                "id": r.get("id"),
                "statement": stmt,
                "reason": "not in EARS syntax (needs 'shall' and a When/While/Where/If/ubiquitous shape)",
            })
    return {"ok": not violations, "violations": violations, "count": len(requirements or [])}
