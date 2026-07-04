"""testing_playbook — durable Business Central test-design knowledge.

Encodes the layered testing strategy learned in practice so ``bc_generate_tests``
produces more than happy-path stubs. The layers are:

* happy-path   — the feature does what it should for valid input.
* negative     — invalid input / forbidden operations are rejected.
* boundary     — edge values at the limits of a field's domain.
* business-logic — behaviour derived from tracing how a field is *consumed*
                   (e.g. an "on hold until" date that auto-expires), not just I/O.
* api-contract — for API pages: insert/delete gating, concurrency, malformed bodies.

The BC date facts come from the Microsoft Learn "Date data type" reference:
BC ``Date`` ranges 1753-01-01 .. 9999-12-31; the undefined/blank date ``0D`` is
serialized by OData as ``0001-01-01`` and sorts before every other date.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# --- Business Central date facts (Microsoft Learn: Date data type) ---
BC_DATE_MIN = "1753-01-01"
BC_DATE_MAX = "9999-12-31"
BC_BLANK_DATE_ODATA = "0001-01-01"  # how 0D appears over OData

TEST_LAYERS = ("happy-path", "negative", "boundary", "business-logic", "api-contract")


def date_edge_cases() -> List[Dict[str, str]]:
    """Date values worth probing at the API text-parse boundary.

    These are only meaningful over the API (raw JSON): the AL ``Date`` type can
    never hold an invalid calendar date, so they cannot be exercised from AL.
    """
    return [
        {"value": "2026-02-30", "expect": "reject", "reason": "invalid calendar day (Feb 30)"},
        {"value": "2026-13-01", "expect": "reject", "reason": "month out of range"},
        {"value": "2026-06-00", "expect": "reject", "reason": "day zero"},
        {"value": "0000-06-01", "expect": "reject", "reason": "year zero"},
        {"value": "-0001-06-01", "expect": "reject", "reason": "negative year"},
        {"value": "10000-01-01", "expect": "reject", "reason": "beyond BC max year 9999"},
        {"value": BC_DATE_MAX, "expect": "accept", "reason": "BC maximum date"},
        {"value": BC_BLANK_DATE_ODATA, "expect": "accept", "reason": "blank/undefined date (0D)"},
    ]


def _bracket_length(al_type: str) -> Optional[int]:
    m = re.search(r"\[(\d+)\]", al_type or "")
    return int(m.group(1)) if m else None


def _looks_like_relation(name: str) -> bool:
    low = (name or "").lower()
    return any(tok in low for tok in ("user", "team", "code", "no.", "nummer"))


def negative_cases_for_field(name: str, al_type: str) -> List[Dict[str, str]]:
    """Negative (must-be-rejected) cases inferred from a field's AL type."""
    t = (al_type or "").strip()
    tl = t.lower()
    cases: List[Dict[str, str]] = []
    if tl.startswith("date"):
        cases.append({
            "field": name, "scenario": f"A past date for {name} is rejected",
            "hint": "Validate(<field>, CalcDate('<-1D>', Today())) must error (today-or-future guard).",
        })
    if tl.startswith("code"):
        if _looks_like_relation(name):
            cases.append({
                "field": name, "scenario": f"A value for {name} that violates its table relation is rejected",
                "hint": "Only add this if the field has a TableRelation; assign a non-existing key and expect an error.",
            })
    if tl.startswith("text") or tl.startswith("code"):
        length = _bracket_length(t)
        if length:
            cases.append({
                "field": name, "scenario": f"A value longer than {length} chars for {name} is rejected",
                "hint": f"Validate(<field>, PadStr('', {length + 1}, 'X')) must error (String length).",
            })
    if tl.startswith("boolean"):
        cases.append({
            "field": name, "scenario": f"A non-boolean value for {name} is rejected by the API",
            "hint": "API-layer only: PATCH a wrong-typed JSON value and expect 400.",
        })
    return cases


def boundary_cases_for_field(name: str, al_type: str) -> List[Dict[str, str]]:
    """Boundary (edge value) cases inferred from a field's AL type."""
    t = (al_type or "").strip().lower()
    cases: List[Dict[str, str]] = []
    if t.startswith("date"):
        cases.extend([
            {"field": name, "scenario": f"Today is accepted for {name}", "hint": "Boundary of the today-or-future guard."},
            {"field": name, "scenario": f"A blank date (0D) is accepted for {name}", "hint": "0D must always be allowed."},
            {"field": name, "scenario": f"The BC maximum date ({BC_DATE_MAX}) is accepted for {name}", "hint": "Upper edge of the BC Date range."},
        ])
    if t.startswith("text") or t.startswith("code"):
        length = _bracket_length(t)
        if length:
            cases.append({
                "field": name, "scenario": f"A value of exactly {length} chars for {name} is accepted",
                "hint": f"Validate(<field>, PadStr('', {length}, 'X')) must succeed.",
            })
    return cases


def api_contract_negatives(update: bool) -> List[Dict[str, str]]:
    """Standard API-contract negatives for an API page (proven at the HTTP layer)."""
    cases = [
        {"scenario": "Malformed request body is rejected (400)", "hint": "PATCH invalid JSON / wrong types."},
        {"scenario": "A stale ETag is rejected (optimistic concurrency)", "hint": "PATCH with an old If-Match returns 4xx and does not mutate."},
    ]
    if update:
        cases.append({"scenario": "POST insert is blocked when the page is update-only (405)", "hint": "InsertAllowed=false -> method not allowed."})
        cases.append({"scenario": "DELETE is blocked when the page is update-only", "hint": "DeleteAllowed=false -> rejected, record still present."})
    cases.append({"scenario": "After every rejected request the record is unchanged", "hint": "Re-read and assert no partial/garbage data was written."})
    return cases


def business_logic_prompts(has_date_field: bool) -> List[Dict[str, str]]:
    """Reminders that turn 'field I/O' tests into 'behaviour' tests."""
    prompts = [
        {"scenario": "Pull the real work item and trace who CONSUMES each field",
         "hint": "Business logic lives in the consumers, not the field. Find every read of the field across the app to discover derived rules."},
    ]
    if has_date_field:
        prompts.append({
            "scenario": "Check whether an 'until/till' date is an EXPIRY that auto-releases state",
            "hint": "A date field named like '...Till'/'...Until' often gates state: once it is in the past the associated flag may be auto-cleared. Assert both the still-active and the expired transitions.",
        })
    return prompts


def build_test_plan(spec: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """Build a layered test plan (per TEST_LAYERS) from a loaded spec dict."""
    rules = spec.get("business_rules", []) or []
    data_model = spec.get("data_model", []) or []
    operations = spec.get("operations", []) or []
    is_update = any(
        (op.get("type") if isinstance(op, dict) else op) in ("update", "delete", "create")
        for op in operations
    )
    is_api = any(
        "api" in str((obj.get("type") if isinstance(obj, dict) else obj)).lower()
        for obj in spec.get("objects", []) or []
    ) or bool(data_model)

    has_date = any(str(f.get("al_type", "")).lower().startswith("date") for f in data_model)

    plan: Dict[str, List[Dict[str, str]]] = {layer: [] for layer in TEST_LAYERS}

    for i, rule in enumerate(rules, start=1):
        desc = rule.get("description", rule.get("id", f"Rule {i}"))
        plan["happy-path"].append({"scenario": desc, "hint": "Arrange valid input, act, assert the expected result persists."})
    for field in data_model:
        name = field.get("name", "")
        al_type = field.get("al_type", "")
        plan["negative"].extend(negative_cases_for_field(name, al_type))
        plan["boundary"].extend(boundary_cases_for_field(name, al_type))

    plan["business-logic"].extend(business_logic_prompts(has_date))
    if is_api:
        plan["api-contract"].extend(api_contract_negatives(is_update))
        if has_date:
            for edge in date_edge_cases():
                verb = "rejected" if edge["expect"] == "reject" else "accepted"
                plan["boundary" if edge["expect"] == "accept" else "negative"].append({
                    "scenario": f"API date '{edge['value']}' is {verb} ({edge['reason']})",
                    "hint": "Date edge cases are only testable via raw JSON PATCH (the AL Date type can't hold them).",
                })
    return plan


def render_plan_md(spec_name: str, plan: Dict[str, List[Dict[str, str]]]) -> str:
    """Human-readable test plan."""
    lines = [f"# Test Plan: {spec_name}", "",
             "Layered coverage — happy paths alone are not sufficient.", ""]
    for layer in TEST_LAYERS:
        cases = plan.get(layer, [])
        lines.append(f"## {layer} ({len(cases)})")
        if not cases:
            lines.append("_none inferred_")
        for c in cases:
            field = f" [{c['field']}]" if c.get("field") else ""
            lines.append(f"- {c['scenario']}{field}")
            if c.get("hint"):
                lines.append(f"  - {c['hint']}")
        lines.append("")
    return "\n".join(lines)
