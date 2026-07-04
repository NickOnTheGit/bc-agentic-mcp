"""provenance — stamp generated artifacts + detect staleness.

Every generated spec records the planner version and the hash of the item-context bundle it
was built from. A spec is STALE when either changed since — the antidote to acting on
out-of-date generated files (the failure where a regenerated plan still showed old content).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Bump when the planner logic/templates change materially, so old specs are flagged stale.
GENERATOR_VERSION = "planner/2"


def stamp(spec_json: Dict[str, Any], context_sha: Optional[str]) -> Dict[str, Any]:
    prov = spec_json.setdefault("provenance", {})
    prov["generator_version"] = GENERATOR_VERSION
    prov["context_sha"] = context_sha
    return spec_json


def staleness(spec_json: Dict[str, Any], current_context_sha: Optional[str]) -> Optional[str]:
    """Return a human reason if the spec is stale vs the current planner/context, else None."""
    prov = spec_json.get("provenance", {}) or {}
    gen = prov.get("generator_version")
    if gen and gen != GENERATOR_VERSION:
        return f"planner changed since generation ({gen} -> {GENERATOR_VERSION})"
    stored = prov.get("context_sha")
    if current_context_sha and stored and current_context_sha != stored:
        return "item context changed since the spec was generated"
    return None
