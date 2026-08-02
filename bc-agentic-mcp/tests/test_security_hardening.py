"""Regression tests for server-bound security controls."""
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp import knowledge, review, security
from bc_agentic_mcp.mission_control.app import _BearerAuthMiddleware, create_app
from bc_agentic_mcp.mission_control.views import atlas
from bc_agentic_mcp.tools.approval import handle_request_approval, handle_submit_decision
from bc_agentic_mcp.tools.verify import handle_record_test


async def _ok(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def test_cockpit_api_requires_bearer_and_rejects_cross_origin_actions():
    app = Starlette(
        routes=[Route("/api/action", _ok, methods=["GET", "POST"])],
        middleware=[Middleware(_BearerAuthMiddleware, token="secret")],
    )
    with TestClient(app) as client:
        assert client.get("/api/action").status_code == 401
        assert client.get("/api/action", headers={"Authorization": "Bearer secret"}).status_code == 200
        assert client.post(
            "/api/action",
            headers={"Authorization": "Bearer secret", "Origin": "https://evil.test"},
        ).status_code == 403


def test_remote_cockpit_requires_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("BC_MISSION_CONTROL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BC_MISSION_CONTROL_TOKEN"):
        create_app(str(tmp_path), remote_access=True)


def test_atlas_edges_have_matching_nodes(tmp_path):
    payload = atlas(Path(tmp_path))
    node_ids = {node["id"] for section in payload["sections"] for node in section["nodes"]}
    missing = {
        endpoint
        for edge in payload["edges"]
        for endpoint in (edge["from"], edge["to"])
        if endpoint not in node_ids
    }
    assert missing == set()


@pytest.mark.asyncio
async def test_approval_artifact_tampering_is_blocked(tmp_path):
    review_path = tmp_path / ".specs" / "item" / "REVIEW.md"
    review_path.parent.mkdir(parents=True)
    review_path.write_text("# REVIEW\npacket\n", encoding="utf-8")
    await handle_request_approval(
        str(tmp_path), "item", "plan", str(tmp_path / "wrong.md"), "summary", "id-1"
    )
    approval = tmp_path / ".specs" / "item" / "approvals" / "plan.md"
    approval.write_text(
        approval.read_text(encoding="utf-8").replace("**Status:** pending", "**Status:** approve"),
        encoding="utf-8",
    )
    result = await handle_submit_decision(str(tmp_path), "item", "plan", "approve")
    assert result["status"] == "blocked_approval_integrity"


@pytest.mark.asyncio
async def test_caller_supplied_runtime_evidence_is_blocked(tmp_path):
    result = await handle_record_test(
        project_root=str(tmp_path), spec_name="item", name="claimed", result="pass", covers="all",
        layer="al-unit", evidence="container=acctest passed=1/1",
    )
    assert result["status"] == "blocked_caller_evidence"


def test_server_evidence_receipt_is_bound_to_the_result(tmp_path):
    evidence = "container=acctest passed=1/1 exit=0"
    receipt = security.issue_evidence(
        project_root=tmp_path, spec_name="item", producer="bc_run_tests", name="run",
        result="pass", covers="all", layer="al-unit", evidence=evidence,
    )
    assert security.verify_evidence(
        receipt, project_root=tmp_path, spec_name="item", name="run", result="pass",
        covers="all", layer="al-unit", evidence=evidence,
    )
    assert security.verify_evidence(
        receipt, project_root=tmp_path, spec_name="item", name="run", result="fail",
        covers="all", layer="al-unit", evidence=evidence,
    ) is None


def test_review_retrieval_failure_remains_a_blocker(tmp_path, monkeypatch):
    memory.write_charter(tmp_path, "item", purpose="upgrade", acceptance_criteria=["AC1"], operations={"update": True})
    monkeypatch.setattr(knowledge, "select_articles", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken index")))
    packet = review.build_review_packet(tmp_path, "item", changed_files=["Upgrade.al"])
    assert packet["knowledge_error"] is True
    assert review.handle_review(str(tmp_path), "item", findings=[{"summary": "x"}])["status"] == "blocked_knowledge_coverage"
