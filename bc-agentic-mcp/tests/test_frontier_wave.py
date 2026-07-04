"""Frontier-wave tests: consistency gate, tool health, ADO write-back, review rubric."""
import json

import pytest

from bc_agentic_mcp import tool_health
from bc_agentic_mcp.ado_items import handle_push_items
from bc_agentic_mcp.consistency import handle_analyze_consistency
from bc_agentic_mcp.review import handle_review
from bc_agentic_mcp.workspace import specs_root


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("BC_AGENTIC_SPECS_ROOT", str(tmp_path / "ws"))
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _write_item(root, spec, *, requirements, allowed, design="", tasks="", criteria=None):
    sdir = specs_root(root) / spec
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "spec.json").write_text(json.dumps({
        "requirements": requirements,
        "scope_boundaries": {"allowed_files": allowed},
    }), encoding="utf-8")
    if design:
        (sdir / "DESIGN.md").write_text(design, encoding="utf-8")
    if tasks:
        (sdir / "TASKS.md").write_text(tasks, encoding="utf-8")
    if criteria:
        (sdir / "charter.json").write_text(json.dumps(
            {"acceptance_criteria": criteria}), encoding="utf-8")
    return sdir


# ---------------------------------------------------------------- consistency
def test_consistency_passes_on_coherent_artifacts(ws):
    _write_item(
        ws, "wi1-good",
        requirements=[{"id": "REQ-001",
                       "statement": "The system shall add field SpaceEntryNo to table RealtyObjectFacility."}],
        allowed=["src/RealtyObjectFacility.Table.al"],
        design="# Design\nREQ-001: extend RealtyObjectFacility.Table.al with SpaceEntryNo field.",
        tasks="- [ ] T-001 (REQ-001) add SpaceEntryNo to src/RealtyObjectFacility.Table.al",
        criteria=["field SpaceEntryNo added to table RealtyObjectFacility"],
    )
    out = handle_analyze_consistency(str(ws), "wi1-good")
    assert out["ok"] is True
    assert out["critical"] == []
    assert out["checklist"], "checklist must be generated"
    sdir = specs_root(ws) / "wi1-good"
    assert (sdir / "CONSISTENCY.md").exists() and (sdir / "consistency.json").exists()


def test_consistency_flags_requirement_without_task_as_critical(ws):
    _write_item(
        ws, "wi2-gap",
        requirements=[
            {"id": "REQ-001", "statement": "The system shall add field Alpha to table Foo."},
            {"id": "REQ-002", "statement": "The system shall validate Beta against Bar."},
        ],
        allowed=["src/Foo.Table.al"],
        design="REQ-001 and REQ-002 covered in design.",
        tasks="- [ ] T-001 (REQ-001) add Alpha to src/Foo.Table.al",
    )
    out = handle_analyze_consistency(str(ws), "wi2-gap")
    assert out["ok"] is False
    assert any("REQ-002" in c and "no task" in c for c in out["critical"])
    assert out["next_action"]["tool"] == "bc_breakdown_tasks"


def test_consistency_flags_scope_drift_as_critical(ws):
    _write_item(
        ws, "wi3-drift",
        requirements=[{"id": "REQ-001", "statement": "The system shall change Foo."}],
        allowed=["src/Foo.Table.al"],
        design="REQ-001 in design.",
        tasks="- [ ] T-001 (REQ-001) change src/Foo.Table.al\n- [ ] T-002 also edit src/Sneaky.Codeunit.al",
    )
    out = handle_analyze_consistency(str(ws), "wi3-drift")
    assert any("OUTSIDE" in c and "Sneaky" in c for c in out["critical"])


def test_consistency_blocks_without_spec(ws):
    (specs_root(ws) / "wi4-empty").mkdir(parents=True)
    out = handle_analyze_consistency(str(ws), "wi4-empty")
    assert out.get("blocked") is True


# ---------------------------------------------------------------- tool health
def test_tool_health_aggregates_and_ranks(ws):
    audit_dir = specs_root(ws) / ".audit"
    audit_dir.mkdir(parents=True)
    lines = []
    for i in range(10):
        lines.append(json.dumps({"timestamp": f"2026-07-04T0{i % 9}:00:00+00:00",
                                 "tool": "bc_run_tests", "session_id": "s",
                                 "success": i % 2 == 0, "duration_ms": 100}))
    for i in range(20):
        lines.append(json.dumps({"timestamp": "2026-07-04T01:00:00+00:00",
                                 "tool": "bc_status", "session_id": "s",
                                 "success": True, "duration_ms": 5}))
    (audit_dir / "log.jsonl").write_text("\n".join(lines), encoding="utf-8")
    report = tool_health.handle_tool_health(str(ws))
    assert report["status"] == "ok"
    assert report["tools"]["bc_run_tests"]["failure_rate"] == 0.5
    assert report["tools"]["bc_status"]["failure_rate"] == 0.0
    candidates = [c["tool"] for c in report["improvement_candidates"]]
    assert "bc_run_tests" in candidates and "bc_status" not in candidates
    assert (specs_root(ws) / "policy" / "tool_health.md").exists()


def test_tool_health_no_data(ws):
    assert tool_health.handle_tool_health(str(ws))["status"] == "no_data"


# ---------------------------------------------------------------- ADO write-back
def _fake_requester(created_ids):
    def send(method, url, headers, body):
        assert method == "POST" and "workitems/$" in url
        patch = json.loads(body.decode("utf-8"))
        title = next(op["value"] for op in patch if op["path"] == "/fields/System.Title")
        new_id = 100000 + len(created_ids)
        created_ids.append((new_id, title))
        return 200, json.dumps({"id": new_id, "_links": {"html": {"href": f"http://x/{new_id}"}}})
    return send


def test_push_items_requires_confirmation(ws):
    out = handle_push_items(str(ws), "feature-x", "https://dev.azure.com/org", "Proj",
                            items=[{"title": "Child 1"}])
    assert out["status"] == "blocked_confirmation_required"


def test_push_items_creates_and_is_idempotent(ws):
    created = []
    args = dict(org_url="https://dev.azure.com/org", project="Proj",
                items=[{"title": "Child A", "description": "d"}, {"title": "Child B"}],
                parent_work_item_id="239584", confirm=True,
                requester=_fake_requester(created))
    first = handle_push_items(str(ws), "feature-x", **args)
    assert first["status"] == "pushed"
    assert len(first["created"]) == 2
    assert first["next_action"]["tool"] == "bc_capture_feature"
    # Idempotent by title: re-push creates nothing new.
    again = handle_push_items(str(ws), "feature-x", **args)
    assert again["created"] == [] and sorted(again["skipped_existing"]) == ["Child A", "Child B"]
    record = json.loads((specs_root(ws) / "feature-x" / "pushed_items.json").read_text())
    assert len(record["items"]) == 2


def test_push_items_reports_partial_failures(ws):
    def flaky(method, url, headers, body):
        patch = json.loads(body.decode("utf-8"))
        title = next(op["value"] for op in patch if op["path"] == "/fields/System.Title")
        if "bad" in title.lower():
            return 400, "TF401243: invalid"
        return 200, json.dumps({"id": 7, "_links": {"html": {"href": "http://x/7"}}})
    out = handle_push_items(str(ws), "feature-y", "https://dev.azure.com/org", "Proj",
                            items=[{"title": "Good"}, {"title": "Bad one"}],
                            confirm=True, requester=flaky)
    assert out["status"] == "partial"
    assert len(out["created"]) == 1 and len(out["failures"]) == 1


# ---------------------------------------------------------------- review rubric
def test_review_rubric_recorded_and_averaged(ws):
    out = handle_review(str(ws), "wi5-item",
                        rubric={"grounding": 1.0, "coverage": 0.8,
                                "conventions": 0.9, "risk": 0.7, "note": "solid"},
                        verdict="APPROVE")
    assert out["rubric"]["status"] == "rubric_recorded"
    assert out["rubric"]["overall"] == 0.85
    assert out["rubric"]["passed"] is True
    history = json.loads((specs_root(ws) / "wi5-item" / "review_rubric.json").read_text())
    assert len(history) == 1 and history[0]["scores"]["coverage"] == 0.8


def test_review_rubric_rejects_invalid_scores(ws):
    out = handle_review(str(ws), "wi6-item", rubric={"grounding": 1.5})
    assert out["status"] == "error"
    assert "outside 0.0-1.0" in out["reason"] or "missing" in out["reason"]


def test_review_rubric_fails_low_quality(ws):
    out = handle_review(str(ws), "wi7-item",
                        rubric={"grounding": 0.6, "coverage": 0.4,
                                "conventions": 0.8, "risk": 0.9})
    assert out["rubric"]["passed"] is False  # coverage 0.4 < 0.5 floor
