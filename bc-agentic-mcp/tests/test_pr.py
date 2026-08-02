"""Phase 3 (B1 + B2 + B3 + C1 + F2): PR lifecycle, rework loop, state sync,
gate collapse, implement split. All network via the injected requester seam.
"""
import asyncio
import json
from pathlib import Path

import pytest

from bc_agentic_mcp import authorization, pr as pr_core, timeline, workflow_policy
from bc_agentic_mcp.errors import MCPError
from bc_agentic_mcp.tools.implement import handle_implement_write
from bc_agentic_mcp.tools.pr import (
    handle_create_pr,
    handle_get_review_comments,
    handle_merge_status,
    handle_prepare_pr,
    handle_resolve_review_comment,
    handle_sync_item_state,
)
from bc_agentic_mcp.tools import pr as pr_tools
from bc_agentic_mcp.tools import approval as approval_tool


# ---------------------------------------------------------------------------
# Pure builders + classification
# ---------------------------------------------------------------------------

def test_repo_api_url_shapes():
    url = pr_core.repo_api_url("https://dev.azure.com/org", "Proj", "Repo")
    assert url == "https://dev.azure.com/org/Proj/_apis/git/repositories/Repo/pullrequests?api-version=7.0"
    url = pr_core.repo_api_url("https://dev.azure.com/org/", "Proj", "Repo", "/12/threads")
    assert "/pullrequests/12/threads?api-version=7.0" in url


def test_repo_api_url_encodes_spaces_in_names():
    """ADO project/repo names may contain spaces ('ERP AL') — unencoded they break
    urllib with \"URL can't contain control characters\" (observed live on 267600)."""
    url = pr_core.repo_api_url("https://dev.azure.com/org", "My Proj", "ERP AL")
    assert "/My%20Proj/_apis/git/repositories/ERP%20AL/pullrequests" in url
    assert " " not in url
    wi = pr_core.workitem_api_url("https://dev.azure.com/org", "My Proj", 267600)
    assert "/My%20Proj/_apis/wit/workitems/267600" in wi
    assert " " not in wi


def test_build_create_payload_normalizes_refs():
    p = pr_core.build_create_payload(
        source_branch="feature/x", target_branch="refs/heads/main",
        title="t", description="d", work_item_id=239597,
    )
    assert p["sourceRefName"] == "refs/heads/feature/x"
    assert p["targetRefName"] == "refs/heads/main"
    assert p["workItemRefs"] == [{"id": "239597"}]


def test_classify_votes_rule():
    # approved = no vote < 0 AND at least one vote >= 5
    assert pr_core.classify_votes([{"vote": 10}])["approved"] is True
    assert pr_core.classify_votes([{"vote": 5}, {"vote": 0}])["approved"] is True
    assert pr_core.classify_votes([{"vote": 10}, {"vote": -5}])["approved"] is False
    assert pr_core.classify_votes([{"vote": 0}])["approved"] is False
    assert pr_core.classify_votes([])["approved"] is False


def test_classify_threads_open_vs_resolved():
    threads = [
        {"id": 1, "status": "active", "comments": [{"content": "fix naming", "author": {"displayName": "Rev"}}],
         "threadContext": {"filePath": "/src/a.al"}},
        {"id": 2, "status": "fixed", "comments": []},
        {"id": 3, "status": "pending", "comments": []},
        {"id": 4, "status": "active", "isDeleted": True, "comments": []},
        {"id": 5, "status": ""},  # system thread without status — ignored
    ]
    out = pr_core.classify_threads(threads)
    assert out["open_count"] == 2 and out["resolved_count"] == 1
    assert out["open"][0]["comment"] == "fix naming"
    assert out["open"][0]["file"] == "src/a.al"  # repo-relative (leading slash stripped, 2026-07-06)


# ---------------------------------------------------------------------------
# REST ops via requester seam (PAT never leaks)
# ---------------------------------------------------------------------------

def test_create_pr_posts_and_parses():
    seen = {}

    def requester(method, url, headers, body):
        seen.update(method=method, url=url, auth=headers.get("Authorization", ""), body=body)
        return 201, json.dumps({
            "pullRequestId": 77, "status": "active",
            "_links": {"web": {"href": "https://dev.azure.com/org/p/_git/r/pullrequest/77"}},
        })

    r = pr_core.create_pr(
        org_url="https://dev.azure.com/org", project="p", repository="r",
        source_branch="feature/x", target_branch="main", title="T", description="D",
        requester=requester,
    )
    assert r["ok"] is True and r["pr_id"] == 77
    assert seen["method"] == "POST"
    assert seen["auth"].startswith("Basic ")
    assert json.loads(seen["body"])["title"] == "T"


def test_create_pr_http_error_is_structured():
    r = pr_core.create_pr(
        org_url="https://dev.azure.com/org", project="p", repository="r",
        source_branch="s", target_branch="t", title="T", description="D",
        requester=lambda m, u, h, b: (403, "forbidden"),
    )
    assert r["ok"] is False and r["status"] == 403


def test_no_pat_and_no_requester_refuses(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    r = pr_core.create_pr(
        org_url="https://dev.azure.com/org", project="p", repository="r",
        source_branch="s", target_branch="t", title="T", description="D",
    )
    assert r["ok"] is False and "AZURE_DEVOPS_EXT_PAT" in r["reason"]


def test_resolve_thread_replies_then_patches():
    calls = []

    def requester(method, url, headers, body):
        calls.append((method, url))
        return 200, json.dumps({"id": 1})

    r = pr_core.resolve_thread(
        org_url="https://dev.azure.com/org", project="p", repository="r",
        pr_id=77, thread_id=5, reply="fixed in abc123", requester=requester,
    )
    assert r["ok"] is True and r["replied"] is True
    assert calls[0][0] == "POST" and "/threads/5/comments" in calls[0][1]
    assert calls[1][0] == "PATCH" and calls[1][1].endswith("/threads/5?api-version=7.0")


def test_resolve_thread_invalid_status_refused():
    r = pr_core.resolve_thread(
        org_url="x", project="p", repository="r", pr_id=1, thread_id=1,
        status="done", requester=lambda m, u, h, b: (200, "{}"),
    )
    assert r["ok"] is False and "invalid thread status" in r["reason"]


def test_merge_status_classifies():
    def requester(method, url, headers, body):
        return 200, json.dumps({
            "status": "active", "mergeStatus": "succeeded",
            "reviewers": [{"displayName": "A", "vote": 10}, {"displayName": "B", "vote": 0}],
        })

    r = pr_core.merge_status(
        org_url="https://dev.azure.com/org", project="p", repository="r",
        pr_id=77, requester=requester,
    )
    assert r["ok"] is True and r["approved"] is True and r["completed"] is False


def test_sync_workitem_state_patches_json_patch():
    seen = {}

    def requester(method, url, headers, body):
        seen.update(method=method, url=url, ctype=headers.get("Content-Type"), body=body)
        return 200, json.dumps({"fields": {"System.State": "Resolved"}})

    r = pr_core.sync_workitem_state(
        org_url="https://dev.azure.com/org", project="p", work_item_id=239597,
        state="Resolved", requester=requester,
    )
    assert r["ok"] is True and r["new_state"] == "Resolved"
    assert seen["method"] == "PATCH"
    assert seen["ctype"] == "application/json-patch+json"
    patch = json.loads(seen["body"])
    assert patch == [{"op": "add", "path": "/fields/System.State", "value": "Resolved"}]
    assert "/workitems/239597" in seen["url"]


def test_sync_workitem_state_requires_state():
    r = pr_core.sync_workitem_state(
        org_url="x", project="p", work_item_id=1, state="  ",
        requester=lambda m, u, h, b: (200, "{}"),
    )
    assert r["ok"] is False


# ---------------------------------------------------------------------------
# C1 bridge: PR approval satisfies the internal `code` gate
# ---------------------------------------------------------------------------

def test_record_code_gate_written_on_approval(tmp_path):
    record = {"pr_id": 77, "url": "https://x/pr/77"}
    verdict = {"approved": True, "completed": False,
               "reviewers": [{"name": "Rev", "vote": 10}]}
    path = pr_core.record_code_gate_from_pr(tmp_path, "item-1", record, verdict)
    assert path and Path(path).exists()
    # authorization reads it as a real approval => implementation authorized
    assert authorization.read_decision(tmp_path, "item-1", "code") == "approve"
    assert authorization.implementation_authorized(tmp_path, "item-1") is True
    # idempotent: second call leaves it as approve
    assert pr_core.record_code_gate_from_pr(tmp_path, "item-1", record, verdict) == path


def test_record_code_gate_not_written_when_pending(tmp_path):
    verdict = {"approved": False, "completed": False, "reviewers": []}
    assert pr_core.record_code_gate_from_pr(tmp_path, "item-1", {"pr_id": 1}, verdict) is None
    assert authorization.read_decision(tmp_path, "item-1", "code") is None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _seed_pr_record(root: Path, spec="item-1", **overrides):
    record = {
        "pr_id": 77, "url": "https://x/pr/77",
        "org_url": "https://dev.azure.com/org", "project": "p", "repository": "r",
    }
    record.update(overrides)
    pr_core.save_pr_record(root, spec, record)
    return record


def test_prepare_pr_blocked_without_evidence(tmp_path, monkeypatch):
    from bc_agentic_mcp import verification

    monkeypatch.setattr(verification, "gate", lambda root, spec: {
        "passed": False, "blockers": ["Uncovered criterion: X"], "digest": {}})
    out = asyncio.run(handle_prepare_pr(str(tmp_path), "item-1"))
    assert out["status"] == "blocked_verification" and out["blocked"] is True
    assert out["next_action"]["tool"] == "bc_verify"


def _seed_passing_review(root: Path, spec="item-1"):
    """Satisfy the mandatory reviewer gate (blocked_reviewer_required) in fixtures."""
    import json as _json
    from datetime import datetime, timezone
    from bc_agentic_mcp.workspace import specs_root as _sr
    d = _sr(root) / spec
    d.mkdir(parents=True, exist_ok=True)
    (d / "review_rubric.json").write_text(_json.dumps([{
        "ts": datetime.now(timezone.utc).isoformat(),
        "scores": {"grounding": 1.0, "coverage": 1.0, "conventions": 1.0, "risk": 1.0},
        "overall": 1.0, "passed": True, "verdict": "approve", "note": "fixture",
    }]), encoding="utf-8")


def test_prepare_pr_builds_description_from_evidence(tmp_path, monkeypatch):
    from bc_agentic_mcp import verification

    digest = {
        "rows": [{"criterion": "filter works", "validated": True, "strength_label": "al-unit"}],
        "coverage_pct": 100.0, "criteria_count": 1,
        "required_strength_label": "runtime", "tests_recorded": 7,
    }
    monkeypatch.setattr(verification, "gate", lambda root, spec: {
        "passed": True, "blockers": [], "digest": digest})
    _seed_passing_review(tmp_path)
    out = asyncio.run(handle_prepare_pr(str(tmp_path), "item-1", target_branch="develop"))
    assert out["status"] == "pr_prepared"
    assert out["_timeline_phase"] == "pr_prepared"
    assert out["source_branch"] == "feature/item-1"
    # human-first story contract: prose sections, evidence summarized in a sentence
    assert "## What this delivers" in out["description"]
    assert "## What was proven" in out["description"]
    assert "7 recorded runs" in out["description"]
    assert Path(out["pr_path"]).exists()
    assert out["next_action"]["tool"] == "bc_create_pr"


def test_create_pr_requires_preparation(tmp_path):
    out = asyncio.run(handle_create_pr(
        str(tmp_path), "item-1", org_url="https://dev.azure.com/org",
        project="p", repository="r",
    ))
    assert out["status"] == "blocked_not_prepared"
    assert out["next_action"]["tool"] == "bc_prepare_pr"


def test_create_pr_saves_record(tmp_path, monkeypatch):
    directory = pr_core.pr_dir(tmp_path, "item-1")
    directory.mkdir(parents=True)
    (directory / "PR.md").write_text(
        "# T\n\n## What this delivers\n\nA thing.\n\n## Where to look first\n\n- X\n\n"
        "## What was proven\n\nTests ran.\n", encoding="utf-8")
    (directory / "prepared.json").write_text(json.dumps({
        "title": "T", "source_branch": "feature/item-1", "target_branch": "main",
    }), encoding="utf-8")
    monkeypatch.setattr(pr_core, "create_pr", lambda **kw: {
        "ok": True, "pr_id": 88, "url": "https://x/88",
        "source_branch": kw["source_branch"], "target_branch": kw["target_branch"],
    })
    out = asyncio.run(handle_create_pr(
        str(tmp_path), "item-1", org_url="https://dev.azure.com/org",
        project="p", repository="r", work_item_id=239597, confirm=True,
    ))
    assert out["status"] == "pr_created" and out["_timeline_phase"] == "pr_created"
    saved = pr_core.load_pr_record(tmp_path, "item-1")
    assert saved["pr_id"] == 88 and saved["work_item_id"] == 239597


def test_get_review_comments_without_pr_blocked(tmp_path):
    out = asyncio.run(handle_get_review_comments(str(tmp_path), "item-1"))
    assert out["status"] == "blocked_no_pr"
    assert out["next_action"]["tool"] == "bc_create_pr"


def test_get_review_comments_open_enters_rework(tmp_path, monkeypatch):
    _seed_pr_record(tmp_path)
    monkeypatch.setattr(pr_core, "get_threads", lambda **kw: {
        "ok": True, "open_count": 2, "resolved_count": 1,
        "open": [{"thread_id": 5, "comment": "rename var"}],
    })
    out = asyncio.run(handle_get_review_comments(str(tmp_path), "item-1"))
    assert out["status"] == "review_comments_open"
    assert out["_timeline_phase"] == "review_comments_open"
    assert out["next_action"]["tool"] == "bc_implement_write"


def test_get_review_comments_none_points_to_merge(tmp_path, monkeypatch):
    _seed_pr_record(tmp_path)
    monkeypatch.setattr(pr_core, "get_threads", lambda **kw: {
        "ok": True, "open_count": 0, "resolved_count": 3, "open": []})
    out = asyncio.run(handle_get_review_comments(str(tmp_path), "item-1"))
    assert out["status"] == "no_open_comments"
    assert "_timeline_phase" not in out
    assert out["next_action"]["tool"] == "bc_merge_status"


def test_resolve_review_comment_flow(tmp_path, monkeypatch):
    _seed_pr_record(tmp_path)
    monkeypatch.setattr(pr_core, "resolve_thread", lambda **kw: {
        "ok": True, "thread_id": kw["thread_id"], "new_status": kw["status"], "replied": True})
    out = asyncio.run(handle_resolve_review_comment(
        str(tmp_path), "item-1", thread_id=5, reply="fixed", confirm=True,
        judgment="correct",
        analysis="Reviewer flagged a real formatting slip; verified against the file and fixed in commit."))
    assert out["status"] == "comment_resolved" and out["thread_id"] == 5
    assert out["next_action"]["tool"] == "bc_get_review_comments"


def test_external_writes_dry_run_by_default(tmp_path, monkeypatch):
    """Nothing leaves the machine without confirm=true — the preview carries the
    exact outbound payload plus lint (PR 41673 shipped machine-speak because
    nothing forced a look between prepare and create)."""
    directory = pr_core.pr_dir(tmp_path, "item-1")
    directory.mkdir(parents=True)
    (directory / "PR.md").write_text(
        "# T\n- Wi239589-mandatory-space-link.\n", encoding="utf-8")
    (directory / "prepared.json").write_text(json.dumps({
        "title": "T", "source_branch": "feature/item-1", "target_branch": "main",
    }), encoding="utf-8")
    called = []
    monkeypatch.setattr(pr_core, "create_pr", lambda **kw: called.append(kw) or {"ok": True})

    out = asyncio.run(handle_create_pr(
        str(tmp_path), "item-1", org_url="https://dev.azure.com/org",
        project="p", repository="r"))
    assert out["status"] == "dry_run" and out["executed"] is False
    assert called == []                                   # network untouched
    assert out["would_create"]["title"] == "T"
    assert any("slug" in w for w in out["lint_warnings"])  # lint caught the machine bullet
    assert out["next_action"]["params_hint"]["confirm"] is True

    # THE STANDARD IS A GATE: confirm=true still refuses a substandard description
    # ("make this enforced for every pr", user 2026-07-04)
    out_blocked = asyncio.run(handle_create_pr(
        str(tmp_path), "item-1", org_url="https://dev.azure.com/org",
        project="p", repository="r", confirm=True))
    assert out_blocked["status"] == "blocked_description_standard"
    assert called == []                                   # STILL nothing sent
    assert out_blocked["next_action"]["tool"] == "bc_prepare_pr"

    # resolve + state sync follow the same law (triage satisfied — the wall itself
    # is covered in test_review_loop_walls.py)
    _seed_pr_record(tmp_path)
    out2 = asyncio.run(handle_resolve_review_comment(
        str(tmp_path), "item-1", thread_id=5, reply="done",
        judgment="correct",
        analysis="Reviewer remark verified against the code: the flagged line did carry the issue."))
    assert out2["status"] == "dry_run" and out2["would_post"]["thread_id"] == 5
    out3 = asyncio.run(handle_sync_item_state(
        org_url="https://dev.azure.com/org", project="p",
        work_item_id=239597, state="Resolved"))
    assert out3["status"] == "dry_run" and out3["would_set"]["state"] == "Resolved"


def test_merge_status_approved_records_code_gate(tmp_path, monkeypatch):
    _seed_pr_record(tmp_path)
    monkeypatch.setattr(pr_core, "merge_status", lambda **kw: {
        "ok": True, "pr_status": "active", "merge_status": "succeeded",
        "approved": True, "completed": False,
        "votes": [10], "rejections": 0,
        "reviewers": [{"name": "Rev", "vote": 10}],
    })
    out = asyncio.run(handle_merge_status(str(tmp_path), "item-1"))
    assert out["status"] == "approved"
    assert "code_gate_recorded" in out
    assert authorization.read_decision(tmp_path, "item-1", "code") == "approve"
    assert out["next_action"]["tool"] == "bc_merge_status"  # merge is the human's gate


def test_merge_status_completed_advances_to_merged(tmp_path, monkeypatch):
    _seed_pr_record(tmp_path)
    monkeypatch.setattr(pr_core, "merge_status", lambda **kw: {
        "ok": True, "pr_status": "completed", "merge_status": "succeeded",
        "approved": True, "completed": True,
        "votes": [10], "rejections": 0,
        "reviewers": [{"name": "Rev", "vote": 10}],
    })
    out = asyncio.run(handle_merge_status(str(tmp_path), "item-1"))
    assert out["status"] == "merged" and out["_timeline_phase"] == "merged"
    assert out["next_action"]["tool"] == "bc_archive"


def test_sync_item_state_handler(monkeypatch):
    monkeypatch.setattr(pr_core, "sync_workitem_state", lambda **kw: {
        "ok": True, "work_item_id": kw["work_item_id"], "new_state": kw["state"]})
    out = asyncio.run(handle_sync_item_state(
        org_url="https://dev.azure.com/org", project="p",
        work_item_id=239597, state="Resolved", confirm=True))
    assert out["status"] == "state_synced" and out["new_state"] == "Resolved"


# ---------------------------------------------------------------------------
# Feature PR (one PR per feature): gate + description aggregate over children
# ---------------------------------------------------------------------------

def _feature_with_children(root: Path) -> None:
    fd = root / ".specs" / "feature-88001-demo"
    (fd / "context").mkdir(parents=True)
    (fd / "feature_plan.json").write_text("{}", encoding="utf-8")
    (fd / "context" / "feature.json").write_text(json.dumps({
        "id": "88001", "title": "Demo feature",
        "children": [{"id": "1", "title": "A", "state": "Active"},
                     {"id": "2", "title": "B", "state": "Active"}],
    }), encoding="utf-8")
    for spec, cid in (("f-a", "1"), ("f-b", "2")):
        d = root / ".specs" / spec / "context"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"item_id": cid}), encoding="utf-8")


def test_verification_gate_feature_aggregates_children(tmp_path, monkeypatch):
    from bc_agentic_mcp import verification

    _feature_with_children(tmp_path)

    def fake_item_gate(root, spec, strength=None):
        if spec == "f-a":
            return {"passed": True, "blockers": [],
                    "digest": {"criteria_count": 2, "tests_recorded": 5, "coverage_pct": 100.0,
                               "required_strength_label": "container", "rows": [
                                   {"criterion": "works", "validated": True, "strength_label": "container"}]}}
        return {"passed": False, "blockers": ["Uncovered criterion: Y"],
                "digest": {"criteria_count": 1, "tests_recorded": 0, "coverage_pct": 0.0,
                           "required_strength_label": "container", "rows": [],
                           "uncovered": ["Y"]}}

    monkeypatch.setattr(verification, "build_verification",
                        lambda root, spec, s=None: fake_item_gate(root, spec, s)["digest"])
    # drive through the real gate: children resolve via the fixture manifests
    out = verification.gate(tmp_path, "feature-88001-demo")
    assert out["passed"] is False
    assert any(b.startswith("[f-b]") for b in out["blockers"]), out["blockers"]
    assert out["digest"]["criteria_count"] == 3
    assert out["digest"]["rows"][0]["criterion"].startswith("[f-a]")


def test_verification_gate_skips_done_children_without_spec(tmp_path, monkeypatch):
    """A child DONE before the feature work (e.g. a technical-design item) ships
    nothing in this PR — it must not block, only be noted (observed live:
    264365 'Technical design' blocked a fully verified feature)."""
    import json as _json
    from bc_agentic_mcp import verification

    fd = tmp_path / ".specs" / "feature-88002-demo"
    (fd / "context").mkdir(parents=True)
    (fd / "feature_plan.json").write_text("{}", encoding="utf-8")
    (fd / "context" / "feature.json").write_text(_json.dumps({
        "children": [{"id": "1", "title": "A", "state": "Active"},
                     {"id": "9", "title": "Technical design", "state": "Done"}],
    }), encoding="utf-8")
    d = tmp_path / ".specs" / "g-a" / "context"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(_json.dumps({"item_id": "1"}), encoding="utf-8")

    monkeypatch.setattr(verification, "build_verification", lambda root, spec, s=None: {
        "criteria_count": 1, "tests_recorded": 2, "coverage_pct": 100.0,
        "required_strength_label": "container",
        "rows": [{"criterion": "works", "validated": True, "strength_label": "container"}]})

    out = verification.gate(tmp_path, "feature-88002-demo")
    assert out["passed"] is True, out["blockers"]
    assert any("Done before this PR" in x for x in out["digest"]["external_children"])


def test_reviewer_gate_feature_demands_review_per_child(tmp_path):
    _feature_with_children(tmp_path)
    # f-a reviewed (fresh, no git repo so no commit-freshness check), f-b not
    (tmp_path / ".specs" / "f-a" / "review_rubric.json").write_text(json.dumps(
        [{"passed": True, "ts": "2099-01-01T00:00:00+00:00"}]), encoding="utf-8")
    block = pr_tools._reviewer_gate(tmp_path, "feature-88001-demo")
    assert block is not None
    assert block["unreviewed_children"] == ["f-b"]
    (tmp_path / ".specs" / "f-b" / "review_rubric.json").write_text(json.dumps(
        [{"passed": True, "ts": "2099-01-01T00:00:00+00:00"}]), encoding="utf-8")
    assert pr_tools._reviewer_gate(tmp_path, "feature-88001-demo") is None


def test_feature_aggregate_spec_merges_children_objects(tmp_path):
    _feature_with_children(tmp_path)
    (tmp_path / ".specs" / "f-a" / "spec.json").write_text(json.dumps({
        "work_item_id": 111, "goal": "do A",
        "objects_to_modify": [{"type": "table", "target": "Foo", "change": "Modify"}],
    }), encoding="utf-8")
    (tmp_path / ".specs" / "f-b" / "spec.json").write_text(json.dumps({
        "work_item_id": 222, "goal": "do B",
        "objects_to_create": [{"type": "codeunit", "name": "Bar_UpgradeX", "target": "Bar_Upgrade"}],
    }), encoding="utf-8")
    agg = pr_tools._feature_aggregate_spec(
        tmp_path, "feature-88001-demo", ["f-a", "f-b"], {})
    assert agg["feature_name"] == "Demo feature"
    assert agg["work_item_id"] == "88001"
    assert agg["objects_to_modify"][0]["target"] == "[111] Foo"
    assert agg["objects_to_create"][0]["name"] == "[222] Bar_UpgradeX"
    assert "do A" in agg["goal"] and "do B" in agg["goal"]


def test_reflection_gate_blocks_pr_until_lessons_distilled(tmp_path):
    """LEARN-BEFORE-SHIP: unreflected mistake checkpoints block PR preparation —
    a whole feature shipped with 65 unreflected signals while lessons.json stayed
    frozen (retro 2026-07-04)."""
    from bc_agentic_mcp import checkpoints as memory, reflection

    _feature_with_children(tmp_path)
    memory.append_checkpoint(tmp_path, "f-a", kind="mistake",
                             summary="compile broke on missing using")
    block = pr_tools._reflection_gate(tmp_path, "feature-88001-demo")
    assert block is not None and block["status"] == "blocked_reflection_due"
    assert block["reflection_due"] == {"f-a": 1}
    assert block["next_action"]["tool"] == "bc_reflect"

    reflection.record_reflection(tmp_path, "f-a", note="distilled", lessons=[
        {"mistake": "missing using", "correction": "added it", "rule": "usings first"}])
    assert pr_tools._reflection_gate(tmp_path, "feature-88001-demo") is None


def test_feature_story_reads_like_prose_not_dumps(tmp_path):
    """The description tells a story: no [id]-prefixed object dumps, no bare
    checkbox matrices (user verdict 2026-07-04: 'not human friendly'). The
    team-written work-item TITLE wins over agent-written purposes."""
    fd = tmp_path / ".specs" / "feature-88001-demo"
    (fd / "context").mkdir(parents=True)
    (fd / "feature_plan.json").write_text("{}", encoding="utf-8")
    (fd / "context" / "feature.json").write_text(json.dumps({
        "feature": {"id": "88001", "title": "[Facility] Link facilities to spaces"},
        "children": [
            {"id": "111", "title": "[Facilities] - Link each facility to a space of the same object", "state": "Active"},
            {"id": "222", "title": "[Facilities] - Convert existing data once", "state": "Active"},
        ],
    }), encoding="utf-8")
    for spec, cid in (("f-a", "111"), ("f-b", "222")):
        d = tmp_path / ".specs" / spec / "context"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"item_id": cid}), encoding="utf-8")
    (tmp_path / ".specs" / "f-a" / "spec.json").write_text(json.dumps({
        "work_item_id": 111,
        "objects_to_modify": [{"type": "table", "target": "[111] extensions\\src\\RealtyObjectFacilityFDN.Table.al"}],
    }), encoding="utf-8")
    (tmp_path / ".specs" / "f-b" / "spec.json").write_text(json.dumps({
        "work_item_id": 222,
        "objects_to_create": [{"type": "codeunit", "name": "[222] FacilitiesConv_UpgradeX",
                               "target": "FacilitiesConv_Upgrade"}],
    }), encoding="utf-8")
    spec = pr_tools._feature_aggregate_spec(tmp_path, "feature-88001-demo", ["f-a", "f-b"], {})
    digest = {"criteria_count": 9, "tests_recorded": 12, "coverage_pct": 100.0}
    text = "\n".join(pr_tools._feature_story_lines(
        tmp_path, "feature-88001-demo", ["f-a", "f-b"], spec, digest, "88001"))
    assert "# Link facilities to spaces" in text          # human feature title
    assert "## What this delivers" in text
    assert "Link each facility to a space of the same object. (WI 111)" in text
    assert "[111]" not in text and "[222]" not in text    # no machine prefixes
    assert "RealtyObjectFacilityFDN" in text              # schema named as an OBJECT, not a path
    assert ".Table.al" not in text
    assert "second developer" in text
    assert "FIRST COMMENT" in text
    assert len(text) < 3000                               # fits ADO cap by design


def test_update_pr_description_patches_in_place():
    captured = {}

    def requester(method, url, headers, body):
        captured.update(method=method, url=url, body=json.loads(body))
        return 200, "{}"

    import os
    os.environ.setdefault("AZURE_DEVOPS_EXT_PAT", "x")
    out = pr_core.update_pr_description(
        org_url="https://dev.azure.com/org", project="P", repository="ERP AL",
        pr_id=41673, description="new story", requester=requester)
    assert out["ok"] is True
    assert captured["method"] == "PATCH"
    assert "/pullrequests/41673?" in captured["url"]
    assert captured["body"]["description"] == "new story"


# ---------------------------------------------------------------------------
# Timeline: result-driven phases + stage routing (B2)
# ---------------------------------------------------------------------------

def test_timeline_phase_override_from_result(tmp_path, monkeypatch):
    recorded = {}

    def fake_record_phase(root, spec, phase, **kw):
        recorded["phase"] = phase

    monkeypatch.setattr(timeline, "record_phase", fake_record_phase)
    timeline.record_tool_phase(str(tmp_path), "item-1", "bc_get_review_comments",
                               {"_timeline_phase": "review_comments_open"})
    assert recorded["phase"] == "review_comments_open"
    # no marker + not in TOOL_PHASE => nothing recorded
    recorded.clear()
    timeline.record_tool_phase(str(tmp_path), "item-1", "bc_get_review_comments",
                               {"status": "no_open_comments"})
    assert recorded == {}


def test_stage_routing_for_pr_phases():
    assert workflow_policy._phase_to_stage("pr_prepared") == "verify"
    assert workflow_policy._phase_to_stage("pr_created") == "verify"
    # B2: open review comments re-admit implement-stage tools
    assert workflow_policy._phase_to_stage("review_comments_open") == "implement"
    # merged stays in verify so bc_archive remains callable
    assert workflow_policy._phase_to_stage("merged") == "verify"


def test_implement_stage_allows_rework_loop_tools():
    allowed = workflow_policy.STAGE_ALLOWLIST["implement"]
    assert {"bc_implement_write", "bc_get_review_comments",
            "bc_resolve_review_comment", "bc_run_tests"} <= allowed


# ---------------------------------------------------------------------------
# C1: canonical plan/code gates
# ---------------------------------------------------------------------------

def test_approval_accepts_canonical_and_legacy_phases():
    assert approval_tool.CANONICAL_GATES == {"plan", "code"}
    assert approval_tool.VALID_PHASES == {"plan", "code", "spec", "design",
                                          "tasks", "implement", "complete"}
    assert "code" in approval_tool._VERIFIED_PHASES
    assert "plan" in approval_tool._TRACEABILITY_PHASES


def test_plan_approval_authorizes_implementation(tmp_path):
    art = tmp_path / "spec.json"
    art.write_text("{}", encoding="utf-8")
    specs_dir = tmp_path / ".specs" / "item-1"
    specs_dir.mkdir(parents=True)
    (specs_dir / "REVIEW.md").write_text("# REVIEW\nplan review packet\n", encoding="utf-8")

    async def flow():
        await approval_tool.handle_request_approval(
            str(tmp_path), "item-1", "plan", str(art), "plan review", "key-1")
        return await approval_tool.handle_submit_decision(
            str(tmp_path), "item-1", "plan", "approve")

    out = asyncio.run(flow())
    assert out["status"] == "approve"
    assert out["next_action"] == "proceed_to_bc_implement"
    assert "deprecation" not in out
    assert authorization.implementation_authorized(tmp_path, "item-1") is True


def test_legacy_phase_flags_deprecation(tmp_path):
    art = tmp_path / "TASKS.md"
    art.write_text("# tasks", encoding="utf-8")
    review = tmp_path / ".specs" / "item-1" / "REVIEW.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("# REVIEW\ncanonical packet\n", encoding="utf-8")

    async def flow():
        await approval_tool.handle_request_approval(
            str(tmp_path), "item-1", "tasks", str(art), "tasks review", "key-2")
        return await approval_tool.handle_submit_decision(
            str(tmp_path), "item-1", "tasks", "approve")

    out = asyncio.run(flow())
    assert out["canonical_gate"] == "plan"
    assert "deprecated" in out["deprecation"] or "legacy" in out["deprecation"]


# ---------------------------------------------------------------------------
# F2: split implement tools
# ---------------------------------------------------------------------------

def test_implement_write_requires_code_and_path(tmp_path):
    with pytest.raises(MCPError):
        asyncio.run(handle_implement_write(str(tmp_path), "item-1", code="", file_path="a.al"))
    with pytest.raises(MCPError):
        asyncio.run(handle_implement_write(str(tmp_path), "item-1", code="codeunit 1 X {}", file_path=" "))


def test_id_collision_wall_blocks_sibling_worktree_id(tmp_path, monkeypatch):
    # wi267598 live: 66190 taken in-repo, 66189 taken by an UNPUSHED sibling branch.
    # The wall must consult every live worktree before a new object id is minted.
    from bc_agentic_mcp.tools import implement as imp

    mine = tmp_path / "wt-mine"
    theirs = tmp_path / "wt-theirs"
    (theirs / "extensions").mkdir(parents=True)
    (theirs / "extensions" / "Other.Codeunit.al").write_text(
        "codeunit 66189 FacilityCountsSyncFDNT\n{\n}\n", encoding="utf-8")
    mine.mkdir()

    monkeypatch.setattr(imp, "_live_worktrees", lambda root: [mine, theirs])

    def fake_run(cmd, **kw):
        class R:
            stdout = ""
        r = R()
        if "grep" in cmd and str(theirs) in cmd[2]:
            r.stdout = "extensions/Other.Codeunit.al\n"
        return r
    monkeypatch.setattr(imp.subprocess, "run", fake_run)

    block = imp._id_collision_wall(mine, "codeunit 66189 MyNewTestFDNT\n{\n}\n", "ext/New.al")
    assert block and block["status"] == "blocked_id_collision"
    assert "66189" in block["reason"] and "Other.Codeunit.al" in block["colliding_file"]

    # A free id passes.
    assert imp._id_collision_wall(mine, "codeunit 66231 MyNewTestFDNT\n{\n}\n", "ext/New.al") is None


def test_fixture_archaeology_flags_missing_sibling_setup(tmp_path):
    # wi267598 live: siblings' Initialize() already called the DAEB deactivation the
    # new test file lacked — a full container cycle discovered what a folder read knew.
    from bc_agentic_mcp.tools.implement import _fixture_archaeology

    folder = tmp_path / "ext" / "Tests"
    folder.mkdir(parents=True)
    sibling = ("codeunit 66001 SiblingFDNT\n{\n    Subtype = Test;\n"
               "    local procedure Initialize()\n    begin\n"
               "        LibraryLiving.CreateProlongationCluster();\n"
               "        DeactivateDaebNonDaebIntegration();\n"
               "        Commit();\n    end;\n}\n")
    (folder / "SiblingA.Codeunit.al").write_text(sibling, encoding="utf-8")
    (folder / "SiblingB.Codeunit.al").write_text(sibling.replace("66001", "66002"), encoding="utf-8")

    new_code = ("codeunit 66003 NewFDNT\n{\n    Subtype = Test;\n"
                "    local procedure Initialize()\n    begin\n"
                "        LibraryLiving.CreateProlongationCluster();\n    end;\n}\n")
    out = _fixture_archaeology(tmp_path, "ext/Tests/New.Codeunit.al", new_code)
    assert out and "DeactivateDaebNonDaebIntegration" in out["common_setup_missing"]
    assert out["siblings_with_initialize"] == 2

    # Non-test files and complete files stay silent.
    assert _fixture_archaeology(tmp_path, "ext/Tests/New.Codeunit.al", "codeunit 1 X {}") is None
    covered = new_code.replace("    end;\n}", "        DeactivateDaebNonDaebIntegration();\n    end;\n}")
    assert _fixture_archaeology(tmp_path, "ext/Tests/New.Codeunit.al", covered) is None


def test_write_al_file_line_ending_discipline(tmp_path):
    # CRLF input + newline translation produced \r\r\n and a 484-line rewrite
    # for a 20-line change (wi267598 live). The writer must normalize to the
    # file's existing convention and never double-translate.
    from bc_agentic_mcp.tools.implement import _write_al_file
    from bc_agentic_mcp.scope import ScopeEnforcer

    scope = ScopeEnforcer(["ext/new.al", "ext/old.al"], tmp_path,
                          allowed_extensions=["ext"], scope_mode="permissive")

    # New file with CRLF content -> written as LF, no \r anywhere.
    out = _write_al_file(tmp_path, scope, "ext/new.al", "codeunit 1 X\r\n{\r\n}\r\n")
    assert b"\r" not in out.read_bytes()

    # Existing CRLF file -> convention preserved, no \r\r\n ever.
    crlf = tmp_path / "ext" / "old.al"
    crlf.write_bytes(b"codeunit 2 Y\r\n{\r\n}\r\n")
    out = _write_al_file(tmp_path, scope, "ext/old.al", "codeunit 2 Y\n{\n// new\n}\n")
    data = out.read_bytes()
    assert b"\r\r\n" not in data and b"\r\n" in data
