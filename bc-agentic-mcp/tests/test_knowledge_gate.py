"""Tests for the BCQuality knowledge-coverage verification gate."""
import json
import pytest

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import knowledge, review
from bc_agentic_mcp.tools.knowledge_tool import handle_get_knowledge_article
from bc_agentic_mcp.verification import check_knowledge_coverage, validation_class_status


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    monkeypatch.delenv(knowledge.ENV_VENDOR_ROOT, raising=False)
    # Isolate from bundled BCQuality vendor so gate tests control their own corpus.
    monkeypatch.setattr(knowledge, "_bundled_vendor_root", lambda: None)


ARTICLE = """---
domain: upgrade
bc-version: [all]
technologies: [al]
countries: [w1]
application-area: [all]
keywords: [DataPerCompany, upgrade, shared]
---

# Upgrade scope must match DataPerCompany

## Description

A data-upgrade codeunit for a shared table must run per-database.

## Best Practice

Match upgrade codeunit scope to DataPerCompany.

## Anti Pattern

Per-company upgrade over a shared table.
"""


def _setup_spec(root, spec_name="demo"):
    memory.write_charter(
        root, spec_name,
        purpose="upgrade shared DataPerCompany table",
        acceptance_criteria=["AC1: upgrade runs per-database"],
        operations={"update": True},
    )
    kb = knowledge.repo_knowledge_root(root) / "upgrade" / "datapercompany.md"
    kb.parent.mkdir(parents=True, exist_ok=True)
    kb.write_text(ARTICLE, encoding="utf-8")


# --- check_knowledge_coverage unit tests -------------------------------------

def test_coverage_not_required_when_no_packet_meta(tmp_path):
    """If no review_packet_meta.json exists (no review yet), gate is not required."""
    kc = check_knowledge_coverage(tmp_path, "demo")
    assert kc["required"] is False
    assert kc["ok"] is True


def test_coverage_not_required_when_packet_had_zero_articles(tmp_path):
    from bc_agentic_mcp.workspace import specs_root as _sr
    meta_path = _sr(tmp_path) / "demo" / "review_packet_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"packet_article_count": 0}), encoding="utf-8")
    kc = check_knowledge_coverage(tmp_path, "demo")
    assert kc["required"] is False
    assert kc["ok"] is True


def test_coverage_required_and_fails_without_trace(tmp_path):
    """Gate is required and fails when packet had articles but no trace exists."""
    from bc_agentic_mcp.workspace import specs_root as _sr
    meta_path = _sr(tmp_path) / "demo" / "review_packet_meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"packet_article_count": 3}), encoding="utf-8")
    kc = check_knowledge_coverage(tmp_path, "demo")
    assert kc["required"] is True
    assert kc["ok"] is False
    assert "bc_get_knowledge_article" in kc["reason"]


def test_coverage_passes_with_valid_trace(tmp_path):
    """Gate passes only after real article-read receipts are recorded."""
    _setup_spec(tmp_path)
    packet = review.build_review_packet(tmp_path, "demo", changed_files=["Upgrade.al"])
    for path in packet["packet_article_paths"]:
        read = handle_get_knowledge_article(
            str(tmp_path), path, spec_name="demo", packet_id=packet["packet_id"]
        )
        assert "knowledge_receipt" in read
    review.handle_review(
        str(tmp_path), "demo", knowledge_applied=packet["packet_article_paths"]
    )
    kc = check_knowledge_coverage(tmp_path, "demo")
    assert kc["required"] is True
    assert kc["ok"] is True
    assert kc["reason"] == ""


# --- integration: build_review_packet + handle_review + gate -----------------

def test_full_flow_packet_then_trace_then_gate_passes(tmp_path):
    """End-to-end: build packet -> reviewer applies article -> gate passes."""
    _setup_spec(tmp_path)
    # Step 1: build review packet (writes review_packet_meta.json)
    packet = review.build_review_packet(tmp_path, "demo", changed_files=["Upgrade.Codeunit.al"])
    assert packet["packet_article_count"] > 0

    # Step 2: reviewer reads every worklisted article through the server tool.
    for path in packet["packet_article_paths"]:
        read = handle_get_knowledge_article(
            str(tmp_path), path, spec_name="demo", packet_id=packet["packet_id"]
        )
        assert "knowledge_receipt" in read

    # Step 3: reviewer submits findings + the verified article paths.
    review.handle_review(
        str(tmp_path), "demo",
        findings=[{"id": "F1", "kind": "correction", "severity": "warning",
                   "summary": "Upgrade scope correct",
                   "bcquality_refs": ["upgrade/datapercompany.md"]}],
        knowledge_applied=packet["packet_article_paths"],
    )

    # Step 4: verify gate passes
    kc = check_knowledge_coverage(tmp_path, "demo")
    assert kc["ok"] is True


def test_full_flow_packet_no_trace_gate_blocks(tmp_path):
    """End-to-end: build packet -> reviewer forgets knowledge_applied -> gate blocks."""
    _setup_spec(tmp_path)
    review.build_review_packet(tmp_path, "demo", changed_files=["Upgrade.Codeunit.al"])

    # Reviewer submits findings but omits knowledge_applied
    review.handle_review(
        str(tmp_path), "demo",
        findings=[{"id": "F1", "kind": "correction", "severity": "info",
                   "summary": "Looks fine"}],
    )

    kc = check_knowledge_coverage(tmp_path, "demo")
    # packet had articles -> required; no trace -> ok=False
    assert kc["required"] is True
    assert kc["ok"] is False


# --- validation_class_status integration -------------------------------------

def test_knowledge_coverage_class_in_validation_classes(tmp_path):
    """knowledge-coverage appears in validation_class_status output."""
    classes = validation_class_status(tmp_path, "demo", tests=[], spec_json=None)
    assert "knowledge-coverage" in classes
    kc = classes["knowledge-coverage"]
    assert "required" in kc
    assert "ok" in kc


def test_knowledge_coverage_class_not_required_when_no_packet(tmp_path):
    classes = validation_class_status(tmp_path, "demo", tests=[], spec_json=None)
    assert classes["knowledge-coverage"]["required"] is False
    assert classes["knowledge-coverage"]["ok"] is True
