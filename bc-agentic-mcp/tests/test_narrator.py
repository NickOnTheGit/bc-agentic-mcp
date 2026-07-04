"""narrator — every machine phase/engine must translate into plain human language."""
import json

import pytest

from bc_agentic_mcp import narrator, timeline
from bc_agentic_mcp.tools.status import handle_status


@pytest.fixture(autouse=True)
def _colocated(monkeypatch):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    yield


def test_every_lifecycle_phase_has_a_story():
    """No phase may exist that a human cannot follow: TOOL_PHASE and _PHASE_LABEL
    values must all have a plain-language WHERE/NEXT/WHO entry."""
    phases = set(timeline.TOOL_PHASE.values()) | set(timeline._PHASE_LABEL.keys())
    missing = sorted(p for p in phases if p not in narrator.PHASE_STORY)
    assert missing == [], f"phases without a human story: {missing}"


def test_every_engine_has_a_story():
    from bc_agentic_mcp import enforcement
    missing = sorted(e for e in enforcement.REQUIRED_ENGINES if e not in narrator.ENGINE_STORY)
    assert missing == [], f"engines without a human story: {missing}"


def test_explain_phase_shape_and_bugfix_lane():
    story = narrator.explain_phase("root_cause_identified", lane="bugfix")
    assert story["who_acts"] == "I act next."
    assert "reproduces the bug" in story["next"]
    assert "Bugfix lane" in story["lane"]
    assert narrator.explain_phase("nonexistent_phase") is None


def test_human_gates_say_the_human_acts():
    for phase in ("approval_requested", "pr_created", "feature_planned"):
        assert narrator.explain_phase(phase)["who_acts"] == "YOU act next."


def test_explain_blockers_translates_engines():
    engine_result = {
        "next_actions": [
            {"engine": "refinement", "tool": "bc_refine_item"},
            {"engine": "root_cause", "tool": "bc_root_cause"},
        ]
    }
    lines = narrator.explain_blockers(engine_result)
    assert len(lines) == 2
    assert "not yet checked against the actual code" in lines[0]
    assert "bc_refine_item" in lines[0]
    assert "root cause has not been diagnosed" in lines[1]


@pytest.mark.asyncio
async def test_status_carries_human_blockers(tmp_path):
    specs = tmp_path / ".specs"
    specs.mkdir(parents=True)
    (specs / "state.json").write_text(json.dumps({
        "active_spec": "wi-1", "total_specs": 1,
        "specs": {"wi-1": {"name": "wi-1", "phase": "specify",
                           "created": "2026-01-01T00:00:00+00:00",
                           "last_activity": "2026-01-01T00:00:00+00:00"}},
    }), encoding="utf-8")
    (specs / "wi-1").mkdir()
    res = await handle_status(str(tmp_path), "wi-1")
    assert res["human_blockers"], "blocked item must explain itself in plain language"
    assert any("never started properly" in b for b in res["human_blockers"])