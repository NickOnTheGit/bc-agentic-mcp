from pathlib import Path

from bc_agentic_mcp.workflow_policy import check_tool_call


def test_role_policy_blocks_disallowed_tool_for_planner(tmp_path: Path):
    allowed, meta = check_tool_call(
        tool_name="bc_implement",
        agent_role="planner",
        project_root=str(tmp_path),
        spec_name="sample",
    )
    assert allowed is False
    assert meta["policy"] == "role"


def test_stage_policy_blocks_verify_tool_in_plan_stage(tmp_path: Path):
    allowed, meta = check_tool_call(
        tool_name="bc_archive",
        agent_role="orchestrator",
        project_root=str(tmp_path),
        spec_name="sample",
    )
    assert allowed is False
    assert meta["policy"] == "stage"
    assert meta["stage"] == "plan"


def test_orchestrator_can_plan_tool(tmp_path: Path):
    allowed, meta = check_tool_call(
        tool_name="bc_write_spec",
        agent_role="orchestrator",
        project_root=str(tmp_path),
        spec_name="sample",
    )
    assert allowed is True
    assert meta["role"] == "orchestrator"


def test_evidence_phase_does_not_regress_stage(tmp_path: Path):
    """item_refined AFTER reviewed is evidence, not a lifecycle transition:
    the stage must stay 'verify' (observed live: regression to 'plan' blocked
    the prescribed bc_run_tests on wi265204)."""
    from bc_agentic_mcp import timeline

    for phase in ("plan_approved", "implemented", "reviewed", "item_refined"):
        timeline.record_phase(tmp_path, "sample", phase)

    allowed, _ = check_tool_call(
        tool_name="bc_run_tests",
        agent_role="orchestrator",
        project_root=str(tmp_path),
        spec_name="sample",
    )
    assert allowed is True

    from bc_agentic_mcp.workflow_policy import infer_stage
    assert infer_stage(str(tmp_path), "sample") == "verify"


def test_feature_review_packet_regenerable_in_every_stage(tmp_path: Path):
    """The packet is a read-only report — implement/verify must not fence it."""
    from bc_agentic_mcp import timeline

    for phase in ("plan_approved", "implemented"):
        timeline.record_phase(tmp_path, "sample", phase)

    allowed, meta = check_tool_call(
        tool_name="bc_prepare_feature_review",
        agent_role="orchestrator",
        project_root=str(tmp_path),
        spec_name="sample",
    )
    assert allowed is True, meta


def test_feature_stage_is_floor_of_children(tmp_path: Path):
    """A verified feature (all children verified) must reach 'verify' so the
    one-PR-per-feature tools open up — the feature spec itself only ever
    records planning phases (14th stage contradiction)."""
    import json as _json
    from bc_agentic_mcp import timeline
    from bc_agentic_mcp.workflow_policy import infer_stage

    fd = tmp_path / ".specs" / "feature-77001-demo"
    (fd / "context").mkdir(parents=True)
    (fd / "feature_plan.json").write_text("{}", encoding="utf-8")
    (fd / "context" / "feature.json").write_text(_json.dumps({
        "children": [{"id": "1", "title": "A", "state": "Active"},
                     {"id": "2", "title": "B", "state": "Active"}]
    }), encoding="utf-8")
    for spec, cid in (("c-a", "1"), ("c-b", "2")):
        d = tmp_path / ".specs" / spec / "context"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(_json.dumps({"item_id": cid}), encoding="utf-8")

    timeline.record_phase(tmp_path, "feature-77001-demo", "plan_approved")  # feature stays planning-side

    # one child verified, one still implementing -> floor is implement
    timeline.record_phase(tmp_path, "c-a", "verified")
    timeline.record_phase(tmp_path, "c-b", "implemented")
    assert infer_stage(str(tmp_path), "feature-77001-demo") == "implement"

    # all children verified -> the feature reaches verify and PR tools open
    timeline.record_phase(tmp_path, "c-b", "verified")
    assert infer_stage(str(tmp_path), "feature-77001-demo") == "verify"
    allowed, meta = check_tool_call(
        tool_name="bc_prepare_pr",
        agent_role="orchestrator",
        project_root=str(tmp_path),
        spec_name="feature-77001-demo",
    )
    assert allowed is True, meta


def test_post_verify_evidence_does_not_regress_stage(tmp_path: Path):
    """Supplementary evidence runs AFTER verification (a regression slice, an
    API contract) record tests_run/implemented — the spec must stay in verify;
    only the explicit rework trigger (review_comments_open) regresses it
    (observed live: post-verify slices re-fenced the feature PR tools)."""
    from bc_agentic_mcp import timeline
    from bc_agentic_mcp.workflow_policy import infer_stage

    for phase in ("plan_approved", "implemented", "verified", "tests_run", "implemented"):
        timeline.record_phase(tmp_path, "sample", phase)
    assert infer_stage(str(tmp_path), "sample") == "verify"

    # the explicit rework trigger DOES pull it back to implement
    timeline.record_phase(tmp_path, "sample", "review_comments_open")
    assert infer_stage(str(tmp_path), "sample") == "implement"

    # and a fresh verification after the rework lands back in verify
    timeline.record_phase(tmp_path, "sample", "verified")
    timeline.record_phase(tmp_path, "sample", "tests_run")
    assert infer_stage(str(tmp_path), "sample") == "verify"
