"""traceability — deterministic requirement→test tracing (OpenFastTrace model).

Industry practice for proving a specification is fully implemented is *requirement tracing*:
each requirement has a stable UID, downstream artifacts declare which UID they ``cover``, and a
tracer computes — by pure set logic — which requirements are covered, which are uncovered, and
which downstream artifacts are orphaned (cover a UID that does not exist). This mirrors
OpenFastTrace / StrictDoc.

Our spec already carries the UID/link data: ``requirements[* ].id`` (REQ-###) each listing its
``acceptance_tests``; ``acceptance_tests[* ].id`` (AT-###) each with a ``requirement_ref``. This
module turns that into a deterministic trace report + Markdown, with no heuristics.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_trace(
    requirements: List[Dict[str, Any]],
    acceptance_tests: List[Dict[str, Any]],
    passing_test_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compute the requirement↔acceptance-test trace (OFT semantics).

    - ``covered``: requirements linked (either direction) to at least one existing acceptance test.
    - ``uncovered``: requirement UIDs with no existing covering acceptance test.
    - ``orphaned``: acceptance-test UIDs whose ``requirement_ref`` points to a non-existent
      requirement, or that are linked to no requirement at all.
    - ``unverified`` (only when ``passing_test_refs`` given): covered requirements whose
      acceptance tests are not backed by a passing recorded test.
    - ``ok``: there is ≥1 requirement, nothing uncovered, and nothing orphaned.
    """
    reqs = requirements or []
    ats = acceptance_tests or []
    req_ids = {r.get("id") for r in reqs if r.get("id")}
    at_ids = {a.get("id") for a in ats if a.get("id")}

    # Build the bidirectional link map between requirements and acceptance tests.
    at_by_req: Dict[str, set] = {rid: set() for rid in req_ids}
    for r in reqs:
        rid = r.get("id")
        for at in r.get("acceptance_tests", []) or []:
            if at in at_ids and rid in at_by_req:
                at_by_req[rid].add(at)
    for a in ats:
        ref = a.get("requirement_ref")
        aid = a.get("id")
        if ref in at_by_req and aid:
            at_by_req[ref].add(aid)

    covered = sorted(rid for rid, ats_ in at_by_req.items() if ats_)
    uncovered = sorted(rid for rid, ats_ in at_by_req.items() if not ats_)

    # Orphaned acceptance tests: ref to a missing requirement, or not linked anywhere.
    linked_ats = {at for ats_ in at_by_req.values() for at in ats_}
    orphaned: List[str] = []
    for a in ats:
        aid = a.get("id")
        ref = a.get("requirement_ref")
        if not aid:
            continue
        if (ref and ref not in req_ids) or (aid not in linked_ats):
            orphaned.append(aid)
    orphaned = sorted(set(orphaned))

    unverified: List[str] = []
    if passing_test_refs is not None:
        passing = set(passing_test_refs)
        for rid in covered:
            if not (at_by_req[rid] & passing):
                unverified.append(rid)
        unverified = sorted(unverified)

    total = len(req_ids)
    coverage_pct = round(100.0 * len(covered) / total, 1) if total else 0.0
    return {
        "ok": bool(total) and not uncovered and not orphaned,
        "total_requirements": total,
        "total_acceptance_tests": len(at_ids),
        "covered": covered,
        "uncovered": uncovered,
        "orphaned": orphaned,
        "unverified": unverified,
        "coverage_pct": coverage_pct,
        "links": {rid: sorted(ats_) for rid, ats_ in at_by_req.items()},
    }


def trace_spec(spec_json: Dict[str, Any], passing_test_refs: Optional[List[str]] = None) -> Dict[str, Any]:
    """Convenience: trace directly from a spec.json dict."""
    return build_trace(
        spec_json.get("requirements", []),
        spec_json.get("acceptance_tests", []),
        passing_test_refs=passing_test_refs,
    )


def render_trace_md(trace: Dict[str, Any], spec_name: str = "") -> str:
    lines = [
        f"# Traceability: {spec_name}".rstrip(),
        "",
        f"- Requirements: {trace['total_requirements']} | Acceptance tests: {trace['total_acceptance_tests']}",
        f"- Coverage: {trace['coverage_pct']}%  |  OK: {trace['ok']}",
        "",
    ]
    if trace["uncovered"]:
        lines += ["## ✗ Uncovered requirements (no acceptance test)"]
        lines += [f"- {u}" for u in trace["uncovered"]] + [""]
    if trace["orphaned"]:
        lines += ["## ✗ Orphaned acceptance tests (cover a missing/none requirement)"]
        lines += [f"- {o}" for o in trace["orphaned"]] + [""]
    if trace.get("unverified"):
        lines += ["## ⚠ Covered but not backed by a passing test"]
        lines += [f"- {u}" for u in trace["unverified"]] + [""]
    lines += ["## Links (requirement → acceptance tests)"]
    for rid, ats in trace["links"].items():
        lines.append(f"- {rid} → {', '.join(ats) if ats else '(none)'}")
    return "\n".join(lines) + "\n"
