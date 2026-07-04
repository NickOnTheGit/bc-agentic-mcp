"""Tests for the EARS requirement-syntax linter (enforced spec standard)."""
from bc_agentic_mcp import ears


def test_ears_classifies_each_pattern():
    assert ears.classify("The system shall expose the field.") == "ubiquitous"
    assert ears.classify("When the upgrade runs, the system shall populate records.") == "event"
    assert ears.classify("While installing, the system shall lock the table.") == "state"
    assert ears.classify("Where the feature is on, the system shall show it.") == "optional"
    assert ears.classify("If input is invalid, then the system shall reject it.") == "unwanted"


def test_ears_rejects_non_ears():
    assert ears.classify("The system does the thing.") is None      # no 'shall'
    assert ears.classify("") is None


def test_ears_lint_reports_violations():
    reqs = [
        {"id": "REQ-001", "statement": "The system shall expose the field."},
        {"id": "REQ-002", "statement": "Field is added to the table."},
    ]
    res = ears.lint(reqs)
    assert res["ok"] is False
    assert res["violations"][0]["id"] == "REQ-002"


def test_ears_lint_all_valid():
    reqs = [{"id": "REQ-001", "statement": "When X, the system shall Y."}]
    assert ears.lint(reqs)["ok"] is True
