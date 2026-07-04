"""bc_verify / bc_record_test — traceable proof that every acceptance criterion of an
item is covered by a passing test.
"""
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Any, Dict, List, Union

from bc_agentic_mcp import verification
from bc_agentic_mcp.validation import validate_covers, validate_evidence_layer


async def handle_record_test(
    project_root: str,
    spec_name: str,
    name: str,
    result: str,
    covers: Union[str, List[int]],
    layer: str = "",
    evidence: str = "",
) -> Dict[str, Any]:
    """Record a test result as durable evidence covering acceptance criteria.

    ``covers`` = "all" or a list of 1-based acceptance-criterion indices (Charter order).
    """
    # Poka-yoke (F1): a typo'd layer or covers value must fail loudly, not silently
    # record weak/misclassified evidence.
    validate_covers(covers)
    normalized_layer = validate_evidence_layer(layer)
    root = Path(project_root).resolve()
    entry = verification.record_test(
        root, spec_name, name=name, result=result, covers=covers,
        layer=normalized_layer, evidence=evidence,
    )
    return {"recorded": True, "test": entry}


async def handle_verify(project_root: str, spec_name: str) -> Dict[str, Any]:
    """Build the verification/coverage report; write VERIFICATION.md; return the digest."""
    root = Path(project_root).resolve()
    digest = verification.build_verification(root, spec_name)
    report = verification.render_verification_md(digest)
    out = specs_root(root) / spec_name / "VERIFICATION.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    # Persist the MACHINE verdict too: consumers (feature review's proven-by-execution
    # exemption) must read the recorded verdict, never recompute the gate — a cold
    # recompute rebuilt the 12k-file object index and blew the tool timeout (observed
    # live 2026-07-04, 309s).
    import json as _json
    (out.parent / "verification.json").write_text(
        _json.dumps({
            "generated_at": digest.get("generated_at"),
            "coverage_pct": digest.get("coverage_pct"),
            "fully_validated": digest.get("fully_validated"),
            "validation_classes": digest.get("validation_classes"),
        }, indent=1), encoding="utf-8")
    digest["report_path"] = str(out)
    # LOUD validation-class deficits: 'fully_validated' covers criteria coverage only —
    # a required class (e.g. path-coverage: no negative tests) failing must never hide
    # behind a green headline (observed live on Bug 267600).
    blockers = [
        f"{name}: {state.get('reason') or 'required class not satisfied'}"
        for name, state in (digest.get("validation_classes") or {}).items()
        if state.get("required") and not state.get("ok")
    ]
    if blockers:
        digest["validation_blockers"] = blockers
        digest["human"] = {
            "where": "Evidence was weighed: criteria are covered, BUT required validation "
                     "classes are still missing.",
            "next": "; ".join(blockers),
            "who_acts": "I act next.",
        }
    return digest
