"""Tests for the deterministic self-challenge (grill) engine."""
import pytest

from bc_agentic_mcp import grill
from bc_agentic_mcp import checkpoints as memory


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    monkeypatch.delenv("BC_MCP_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("BC_MCP_GLOBAL_LESSONS", raising=False)


def _charter(root, spec="s1", purpose="add upgrade codeunit for shared DataPerCompany table"):
    memory.write_charter(root, spec, purpose=purpose,
                         operations={"update": True},
                         acceptance_criteria=["existing rows initialized once"])


ARTICLE = """---
domain: upgrade
keywords: [DataPerCompany, upgrade codeunit, company guard]
---

# Upgrade scope must match DataPerCompany

## Description

A data-upgrade codeunit for a shared table must run per-database.

## Best Practice

Match scope to DataPerCompany.

## Anti Pattern

Per-company upgrade over a shared table.
"""


def test_packet_always_carries_core_challenges(tmp_path):
    _charter(tmp_path)
    packet = grill.build_grill_packet(tmp_path, "s1")
    ids = [c["id"] for c in packet["challenges"]]
    for core in ("simpler", "alternative", "extend_not_create", "sibling_precedent"):
        assert core in ids
    assert "bc_checkpoint" in packet["answer_via"]
    assert "grill_answer" in packet["answer_via"]


def test_packet_includes_matched_knowledge_articles(tmp_path):
    _charter(tmp_path)
    art = tmp_path / ".specs" / "knowledge" / "upgrade" / "scope.md"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(ARTICLE, encoding="utf-8")
    packet = grill.build_grill_packet(tmp_path, "s1")
    knowledge_challenges = [c for c in packet["challenges"] if c["id"].startswith("knowledge:")]
    assert knowledge_challenges
    assert knowledge_challenges[0]["article"]["path"] == "upgrade/scope.md"


def test_packet_without_charter_still_asks_core_questions(tmp_path):
    packet = grill.build_grill_packet(tmp_path, "nospec")
    assert len(packet["challenges"]) == len(grill.CORE_CHALLENGES)


def test_grill_status_lifecycle(tmp_path):
    _charter(tmp_path)
    # never grilled -> answered (nothing pending)
    assert grill.grill_status(tmp_path, "s1") == {"grilled": False, "answered": True}
    packet = grill.build_grill_packet(tmp_path, "s1")
    grill.record_grill(tmp_path, "s1", packet)
    assert grill.grill_status(tmp_path, "s1") == {"grilled": True, "answered": False}
    memory.append_checkpoint(tmp_path, "s1", kind="grill_answer",
                             summary="simpler: no — criterion AC-1 forces the upgrade codeunit")
    assert grill.grill_status(tmp_path, "s1") == {"grilled": True, "answered": True}
    # a NEW grill reopens the loop
    grill.record_grill(tmp_path, "s1", packet)
    assert grill.grill_status(tmp_path, "s1")["answered"] is False


def test_record_grill_is_a_visible_checkpoint(tmp_path):
    _charter(tmp_path)
    packet = grill.build_grill_packet(tmp_path, "s1")
    out = grill.record_grill(tmp_path, "s1", packet)
    assert out["recorded"] is True
    cps = memory.load_checkpoints(tmp_path, "s1")
    grills = [c for c in cps if c.get("kind") == "grill"]
    assert len(grills) == 1
    assert grills[0]["details"]["challenge_ids"][0] == "simpler"


def test_grill_sweep_is_a_registered_routine_action():
    from bc_agentic_mcp.mission_control import routines
    assert "grill_sweep" in routines.ACTIONS
    normalized = routines.validate_routine(
        {"action": "grill_sweep", "time": "07:30", "days": "weekdays"})
    assert normalized["action"] == "grill_sweep"
