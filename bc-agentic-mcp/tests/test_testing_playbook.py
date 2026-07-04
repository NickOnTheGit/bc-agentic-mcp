"""Tests for the testing_playbook module."""
from bc_agentic_mcp import testing_playbook as pb


def test_date_edge_cases_cover_reject_and_accept():
    cases = pb.date_edge_cases()
    values = {c["value"] for c in cases}
    assert "2026-02-30" in values  # invalid calendar day
    assert "10000-01-01" in values  # beyond BC max
    assert pb.BC_DATE_MAX in values  # accepted boundary
    assert pb.BC_BLANK_DATE_ODATA in values  # blank/0D accepted
    assert any(c["expect"] == "reject" for c in cases)
    assert any(c["expect"] == "accept" for c in cases)


def test_negative_cases_by_type():
    assert pb.negative_cases_for_field("OnHoldTill", "Date")  # past-date reject
    assert pb.negative_cases_for_field("OnHoldUser", "Code[50]")  # relation reject
    overflow = pb.negative_cases_for_field("Remark", "Text[250]")
    assert any("251" in c["hint"] for c in overflow)  # length+1


def test_boundary_cases_for_date():
    cases = pb.boundary_cases_for_field("OnHoldTill", "Date")
    scenarios = " ".join(c["scenario"] for c in cases)
    assert "Today" in scenarios and "blank" in scenarios and pb.BC_DATE_MAX in scenarios


def test_api_contract_negatives_gate_insert_delete_when_update():
    cases = pb.api_contract_negatives(update=True)
    text = " ".join(c["scenario"] for c in cases)
    assert "insert" in text.lower() and "delete" in text.lower()
    assert "unchanged" in text.lower()


def test_build_test_plan_is_layered():
    spec = {
        "business_rules": [{"description": "On hold persists"}],
        "operations": [{"type": "update"}],
        "objects": [{"type": "API Page"}],
        "data_model": [
            {"name": "OnHoldTill", "al_type": "Date"},
            {"name": "Remark", "al_type": "Text[250]"},
        ],
    }
    plan = pb.build_test_plan(spec)
    assert plan["happy-path"]
    assert plan["negative"]
    assert plan["boundary"]
    assert plan["business-logic"]
    assert plan["api-contract"]
    # Date edge cases routed into negative/boundary for API specs with a date field.
    neg_text = " ".join(c["scenario"] for c in plan["negative"])
    assert "2026-02-30" in neg_text


def test_render_plan_md_lists_layers():
    plan = pb.build_test_plan({"business_rules": [], "data_model": []})
    md = pb.render_plan_md("demo", plan)
    for layer in pb.TEST_LAYERS:
        assert f"## {layer}" in md
