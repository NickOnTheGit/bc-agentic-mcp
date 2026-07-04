"""Phase 4 — C4 auto-clarify + D2 test-app targeting + D4 ITEM.md + E2 metrics + F4 envelope."""
import asyncio
import json
from pathlib import Path

from bc_agentic_mcp import auto_clarify, metrics, test_app, timeline
from bc_agentic_mcp.tools.auto_clarify import handle_auto_clarify
from bc_agentic_mcp.workspace import specs_root


# ---------------------------------------------------------------------------
# C4: auto-clarify
# ---------------------------------------------------------------------------

_CLAR = """# Clarifications for: item-1

## Q-001: Which facility code filter should the justification report apply?
_Answer:_ 

## Q-002: Should deletion be: A) Always allowed B) Conditional C) Never
_Answer:_ 

## Q-003: How many angels fit on a pin?
_Answer:_ answered already
"""


def test_parse_open_questions_skips_answered():
    open_qs = auto_clarify.parse_open_questions(_CLAR)
    assert [q["id"] for q in open_qs] == ["Q-001", "Q-002"]


def test_keywords_significant_only():
    kws = auto_clarify.keywords("Which facility code filter should the justification report apply?")
    assert "facility" in kws and "filter" in kws and "justification" in kws
    assert "should" not in kws and "the" not in kws


def _seed_context(root: Path, spec="item-1"):
    cdir = specs_root(root) / spec / "context"
    (cdir / "wiki").mkdir(parents=True, exist_ok=True)
    (cdir / "wiki" / "1097_report-spec.md").write_text(
        "# Report spec\n\n"
        "The justification report must apply the facility code filter from the rental "
        "contract line (field FacilityCode on src/Reports/RentalJustification.al) so only "
        "matching lines are printed.\n\n"
        "Unrelated paragraph about colors and fonts in the layout.\n",
        encoding="utf-8",
    )
    return cdir


def test_propose_matches_grounded_question_only(tmp_path):
    cdir = _seed_context(tmp_path)
    questions = auto_clarify.parse_open_questions(_CLAR)
    corpus = auto_clarify.collect_corpus(cdir)
    result = auto_clarify.propose(questions, corpus)
    assert "Q-001" in result["proposals"]
    prop = result["proposals"]["Q-001"]
    assert "facility code filter" in prop["answer"].lower()
    assert prop["source"].startswith("context/wiki/1097")
    # Q-002 (deletion policy) has no grounding in the corpus -> human
    assert [q["id"] for q in result["needs_human"]] == ["Q-002"]


def test_handle_auto_clarify_end_to_end(tmp_path):
    spec_dir = specs_root(tmp_path) / "item-1"
    spec_dir.mkdir(parents=True)
    (spec_dir / "clarifications.md").write_text(_CLAR, encoding="utf-8")
    _seed_context(tmp_path)
    out = asyncio.run(handle_auto_clarify(str(tmp_path), "item-1"))
    assert out["status"] == "proposals_ready"
    assert "Q-001" in out["proposals"]
    assert out["next_action"]["tool"] == "bc_answer_clarification"
    assert "Q-002" in out["next_action"]["params_hint"]["answers"]


def test_handle_auto_clarify_auto_submit_writes_valid_answers(tmp_path):
    spec_dir = specs_root(tmp_path) / "item-1"
    spec_dir.mkdir(parents=True)
    (spec_dir / "clarifications.md").write_text(_CLAR, encoding="utf-8")
    _seed_context(tmp_path)  # proposal cites ...RentalJustification.al => passes validation
    out = asyncio.run(handle_auto_clarify(str(tmp_path), "item-1", auto_submit=True))
    assert out["auto_submitted"] == ["Q-001"]
    text = (spec_dir / "clarifications.md").read_text(encoding="utf-8")
    assert "facility code filter" in text.lower()
    # the unanswerable question is untouched and still routed to the human
    assert out["next_action"]["tool"] == "bc_answer_clarification"


def test_handle_auto_clarify_no_file(tmp_path):
    out = asyncio.run(handle_auto_clarify(str(tmp_path), "item-1"))
    assert out["status"] == "no_clarifications"


# ---------------------------------------------------------------------------
# D2: test-app resolution + ID allocation
# ---------------------------------------------------------------------------

def _write_app(root: Path, folder: str, name: str, deps=None, id_from=50900, id_to=50999):
    app_dir = root / folder
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "app.json").write_text(json.dumps({
        "name": name,
        "dependencies": deps or [],
        "idRanges": [{"from": id_from, "to": id_to}],
    }), encoding="utf-8")
    return app_dir


def test_is_test_app_by_dependency_and_name():
    assert test_app.is_test_app({"name": "My Tests", "dependencies": []}) is True
    assert test_app.is_test_app({
        "name": "Core", "dependencies": [{"name": "Library Assert", "id": "x"}]}) is True
    assert test_app.is_test_app({"name": "Core", "dependencies": [{"name": "Base Application"}]}) is False


def test_resolve_target_single_and_ambiguous(tmp_path):
    _write_app(tmp_path, "app", "Core App", deps=[{"name": "Base Application"}])
    _write_app(tmp_path, "testapp", "Core Tests", deps=[{"name": "Library Assert"}])
    r = test_app.resolve_target(tmp_path)
    assert r["status"] == "resolved" and r["app_name"] == "Core Tests"
    assert r["object_id"] == 50900  # nothing used yet -> first of range
    # second test app makes it ambiguous; hint disambiguates
    _write_app(tmp_path, "othertests", "Other Tests", deps=[{"name": "Library Assert"}])
    r = test_app.resolve_target(tmp_path)
    assert r["status"] == "ambiguous" and len(r["candidates"]) == 2
    r = test_app.resolve_target(tmp_path, hint="Other")
    assert r["status"] == "resolved" and r["app_name"] == "Other Tests"


def test_allocate_object_id_skips_used(tmp_path):
    app_dir = _write_app(tmp_path, "testapp", "T", deps=[{"name": "Library Assert"}])
    src = app_dir / "src"
    src.mkdir()
    (src / "A.Test.al").write_text('codeunit 50900 "A" { }', encoding="utf-8")
    (src / "B.Test.al").write_text('codeunit 50901 "B" { }\ntableextension 50902 "C" extends X { }',
                                   encoding="utf-8")
    assert test_app.allocate_object_id(app_dir, [{"from": 50900, "to": 50999}]) == 50903


def test_generate_tests_targets_real_app(tmp_path):
    from bc_agentic_mcp.tools.generate_tests import handle_generate_tests

    _write_app(tmp_path, "testapp", "Core Tests", deps=[{"name": "Library Assert"}],
               id_from=51200, id_to=51299)
    spec_dir = specs_root(tmp_path) / "item-1"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.json").write_text(json.dumps({
        "feature_name": "item-1",
        "business_rules": [{"id": "BR-1", "description": "filter applies"}],
        "objects": [], "data_model": [],
        "scope_boundaries": {"allowed_files": ["testapp/src/Item1.Test.al"]},
    }), encoding="utf-8")
    out = asyncio.run(handle_generate_tests(str(tmp_path), "item-1"))
    assert out["status"] == "scaffold_generated"
    assert out["object_id"] == 51200
    assert out["target"]["app_name"] == "Core Tests"
    scaffold = Path(out["test_path"]).read_text(encoding="utf-8")
    assert scaffold.startswith('codeunit 51200 "item-1 Tests"')
    assert out["next_action"]["tool"] == "bc_implement_write"


# ---------------------------------------------------------------------------
# D4: ITEM.md reanchor digest
# ---------------------------------------------------------------------------

def test_item_md_written_and_capped(tmp_path):
    for phase in ("item_received", "spec_written", "implemented", "tests_run"):
        timeline.record_phase(tmp_path, "item-1", phase,
                              artifacts=[f".specs/item-1/{phase}.md"])
    item_md = specs_root(tmp_path) / "item-1" / "ITEM.md"
    assert item_md.exists()
    text = item_md.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) <= timeline.ITEM_MD_MAX_BYTES
    assert "phase: tests_run" in text
    assert "TIMELINE.md" in text


def test_item_md_cap_holds_under_noise(tmp_path):
    events = [{"kind": "phase", "ts": "2026-07-03T10:00:00",
               "summary": "S" * 200, "details": {"phase": "implemented",
                                                 "artifacts": ["a" * 150] * 5}}] * 10
    text = timeline.render_item_md("item-with-a-very-long-name-" + "x" * 60, events)
    assert len(text.encode("utf-8")) <= timeline.ITEM_MD_MAX_BYTES


# ---------------------------------------------------------------------------
# E2: metrics from audit.jsonl
# ---------------------------------------------------------------------------

def _audit_entry(tool, ts, ok=True, ms=100, spec=None):
    e = {"timestamp": ts, "tool": tool, "session_id": "s", "success": ok, "duration_ms": ms}
    if spec:
        e["spec_name"] = spec
    return e


def test_metrics_summarize(tmp_path):
    audit_dir = specs_root(tmp_path) / ".audit"
    audit_dir.mkdir(parents=True)
    entries = [
        _audit_entry("bc_run_tests", "2026-07-03T10:00:00+00:00", ok=True, ms=120000, spec="item-1"),
        _audit_entry("bc_run_tests", "2026-07-03T10:10:00+00:00", ok=False, ms=90000, spec="item-1"),
        _audit_entry("bc_verify", "2026-07-03T11:00:00+00:00", ok=True, ms=50, spec="item-1"),
        _audit_entry("bc_status", "2026-07-03T09:00:00+00:00", ok=True, ms=10),
    ]
    (audit_dir / "log.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\nnot-json\n", encoding="utf-8")

    loaded = metrics.load_entries(tmp_path)
    assert len(loaded) == 4  # torn line skipped

    report = metrics.summarize(loaded)
    assert report["total_calls"] == 4 and report["total_failures"] == 1
    rt = report["per_tool"]["bc_run_tests"]
    assert rt["calls"] == 2 and rt["failures"] == 1 and rt["max_ms"] == 120000
    item = report["per_spec"]["item-1"]
    assert item["events"] == 3 and item["cycle_seconds"] == 3600

    scoped = metrics.summarize(loaded, spec_name="item-1")
    assert scoped["total_calls"] == 3


def test_metrics_handler_empty(tmp_path):
    out = asyncio.run(metrics.handle_metrics(str(tmp_path)))
    assert out["status"] == "no_audit_data" and out["total_calls"] == 0


# ---------------------------------------------------------------------------
# F4: result envelope
# ---------------------------------------------------------------------------

def test_apply_envelope_derives_ok_stage_artifacts(tmp_path):
    from bc_agentic_mcp.server import _apply_envelope

    r = _apply_envelope({"status": "scaffold_generated", "test_path": "x/y.al"},
                        {"project_root": str(tmp_path), "spec_name": "item-1"})
    assert r["ok"] is True and r["stage"] == "plan"
    assert r["artifacts"] == ["x/y.al"]
    r = _apply_envelope({"status": "blocked_env_preflight", "blocked": True}, {})
    assert r["ok"] is False
    # existing keys always win
    r = _apply_envelope({"ok": False, "stage": "verify", "artifacts": ["keep"]}, {})
    assert r["ok"] is False and r["stage"] == "verify" and r["artifacts"] == ["keep"]
