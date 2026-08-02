"""E2E lifecycle walk — drive a synthetic item through the REAL server path.

Every prior E2E (e2e_test.py, test_integration.py) called handlers DIRECTLY,
bypassing the walls: policy/stage routing, doom-guard, timeline recording,
recovery persistence, envelope. That is exactly why 13 stage contradictions
were only ever discovered live. This walk invokes the registered server
closures — the same code path an agent hits — and asserts the machine can
route an item from capture to the PR gates BY ITS OWN RULES:

  init -> capture -> (identity) -> clarify -> answer -> write_spec
  -> plan_design [PRECEDENTS WALL fires -> waiver] -> plan_design
  -> breakdown -> prepare_review -> request_approval -> approve
  [stage flips to implement] -> record evidence -> verify [stage verify]
  -> prepare_pr [end gates hold: blocked with a prescribed next_action]

Hermetic: no ADO env, no container, no git remotes. A failure prints the
full transcript — the harness is a live bug-catcher, not just a regression net.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from bc_agentic_mcp import security

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def _call(tools, transcript, _tool_name, **kw):
    # NB: leading-underscore positionals — bc_record_test takes a `name` KWARG, and a
    # positional called `name` collides with it (the exact server bug class of 07-04).
    fn = tools[_tool_name]
    result = asyncio.run(fn(**kw))
    transcript.append((_tool_name, {k: v for k, v in kw.items() if k not in ("code", "description", "context", "human_bullets")},
                       result if isinstance(result, dict) else {"raw": str(result)[:200]}))
    return result


def _fail(transcript, msg):
    lines = [msg, "", "=== transcript ==="]
    for name, kw, res in transcript:
        status = res.get("status", res.get("ok", "?")) if isinstance(res, dict) else "?"
        reason = str(res.get("reason", res.get("message", "")))[:160] if isinstance(res, dict) else ""
        lines.append(f"  {name}({kw}) -> {status} {reason}")
    pytest.fail("\n".join(lines))


@pytest.fixture()
def walk_env(tmp_path, monkeypatch):
    # Hermetic: the machine must not reach ADO or the team store during the walk.
    for var in ("AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_EXT_PAT",
                "BC_MCP_TEAM_LESSONS_URL", "BC_MCP_TEAM_LESSONS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BC_MCP_NO_WARMUP", "1")
    src = tmp_path / "extensions" / "BaseApp" / "src"
    src.mkdir(parents=True)
    for f in GOLDEN.glob("*.al"):
        (src / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    from bc_agentic_mcp.server import create_server
    server = create_server()
    tools = {t.name: t.fn for t in server._tool_manager._tools.values()}
    return tmp_path, tools


def test_full_lifecycle_walk_via_server(walk_env):
    root_path, tools = walk_env
    root = str(root_path)
    spec = "e2e-walk"
    t = []  # transcript

    # --- plan stage -------------------------------------------------------
    out = _call(tools, t, "bc_init", project_root=root, module_name="BaseApp")
    if not out.get("ok", True):
        _fail(t, "bc_init failed")

    src_dir = root_path / "extensions" / "BaseApp" / "src"
    page_file = next((f.name for f in src_dir.glob("*.al")
                      if "facilities" in f.name.lower()), next(src_dir.glob("*.al")).name)
    al_rel = f"extensions/BaseApp/src/{page_file}"
    description = (
        "Add a read-only indicator to the facilities overview. "
        f"Modify page FacilitiesOfRealtyObjectFDN (file {al_rel}) to surface the "
        "existing NoOfAddresses field so housing coordinators can see address counts "
        "without opening each facility. No new tables. No data upgrade."
    )
    out = _call(tools, t, "bc_capture_item_context", project_root=root, spec_name=spec,
                work_item_id="990001", description=description)
    if not out.get("captured", out.get("ok")):
        _fail(t, "capture failed")

    # Simulate an ADO-backed identity (hermetically): this ARMS the precedents wall.
    manifest_path = root_path / ".specs" / spec / "context" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["identity"] = {"id": "990001", "type": "Product Backlog Item",
                            "title": "Surface NoOfAddresses on facilities overview"}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    out = _call(tools, t, "bc_clarify", project_root=root, spec_name=spec,
                context=description)
    question_ids = [q.get("id") for q in out.get("questions", []) if isinstance(q, dict) and q.get("id")]
    if not question_ids:  # fallback: parse ids from clarifications.md
        cpath = root_path / ".specs" / spec / "clarifications.md"
        if cpath.exists():
            question_ids = re.findall(r"\b(Q-\d+)\b", cpath.read_text(encoding="utf-8"))
    if question_ids:
        answers = {qid: (f"Use the existing field on page FacilitiesOfRealtyObjectFDN in "
                         f"{al_rel}; read-only, no permission changes, no upgrade.")
                   for qid in dict.fromkeys(question_ids)}
        _call(tools, t, "bc_answer_clarification", project_root=root, spec_name=spec,
              answers=answers)

    out = _call(tools, t, "bc_write_spec", project_root=root, spec_name=spec,
                human_bullets=description, idempotency_key="e2e-walk-spec-1")
    for _ in range(3):  # follow the machine's own prescriptions, never guess
        status = str(out.get("status", ""))
        if not status.startswith("blocked"):
            break
        na = out.get("next_action") or {}
        tool = na.get("tool")
        if tool == "bc_auto_clarify":
            _call(tools, t, "bc_auto_clarify", project_root=root, spec_name=spec)
        elif tool == "bc_answer_clarification":
            cpath = root_path / ".specs" / spec / "clarifications.md"
            ids = re.findall(r"\b(Q-\d+)\b", cpath.read_text(encoding="utf-8")) if cpath.exists() else []
            _call(tools, t, "bc_answer_clarification", project_root=root, spec_name=spec,
                  answers={qid: f"Read-only surface of existing field; evidence {al_rel}."
                           for qid in dict.fromkeys(ids)})
        else:
            _fail(t, f"bc_write_spec blocked with unfollowable next_action: {na}")
        out = _call(tools, t, "bc_write_spec", project_root=root, spec_name=spec,
                    human_bullets=description, idempotency_key="e2e-walk-spec-1")
    if str(out.get("status", "")).startswith("blocked"):
        _fail(t, "bc_write_spec never unblocked by following its own prescriptions")

    # THE PRECEDENTS WALL must fire on the real path for an ADO-backed spec...
    out = _call(tools, t, "bc_plan_design", project_root=root, spec_name=spec)
    if out.get("status") != "blocked_precedents_due":
        _fail(t, f"precedents wall did NOT fire (got {out.get('status')!r})")
    # ...and the prescribed waiver must open it.
    out = _call(tools, t, "bc_mine_precedents", project_root=root, spec_name=spec,
                skip=True, reason="e2e walk: hermetic environment, no ADO reachable")
    if out.get("status") != "skipped":
        _fail(t, "explicit precedents waiver refused")
    # Follow the machine's prescriptions until design passes (grounding loop is a
    # legitimate wall: read code context, regenerate the spec, retry).
    for _ in range(4):
        out = _call(tools, t, "bc_plan_design", project_root=root, spec_name=spec)
        status = str(out.get("status", ""))
        if not status.startswith("blocked"):
            break
        if status == "blocked_needs_grounding":
            _call(tools, t, "bc_read_code_context", project_root=root, spec_name=spec)
            _call(tools, t, "bc_write_spec", project_root=root, spec_name=spec,
                  human_bullets=description, idempotency_key="e2e-walk-spec-2")
        else:
            _fail(t, f"bc_plan_design blocked with unexpected wall: {status}")
    if str(out.get("status", "")).startswith("blocked"):
        _fail(t, "bc_plan_design never unblocked by following its own prescriptions")

    out = _call(tools, t, "bc_breakdown_tasks", project_root=root, spec_name=spec)
    if str(out.get("status", "")).startswith("blocked"):
        _fail(t, "bc_breakdown_tasks blocked")

    def _answer_open_questions():
        cpath = root_path / ".specs" / spec / "clarifications.md"
        ids = re.findall(r"\b(Q-\d+)\b", cpath.read_text(encoding="utf-8")) if cpath.exists() else []
        if ids:
            answer = (
                f"Read-only display of the existing NoOfAddresses field on page "
                f"FacilitiesOfRealtyObjectFDN; evidence {al_rel}; no permission changes, "
                "no upgrade. "
                "TEST negative: a facility without linked addresses shows 0 and no error. "
                "TEST edge: a facility with the maximum number of linked addresses renders the count unclipped."
            )
            _call(tools, t, "bc_answer_clarification", project_root=root, spec_name=spec,
                  answers={qid: answer for qid in dict.fromkeys(ids)})
        return bool(ids)

    out = _call(tools, t, "bc_prepare_review", project_root=root, spec_name=spec,
                human_bullets=description, idempotency_key="e2e-walk-review-1")
    for attempt in range(3):
        status = str(out.get("status", ""))
        review_path = out.get("review_path") or out.get("artifact_path")
        if review_path and not status.startswith(("blocked", "needs")):
            break
        na = (out.get("next_action") or {})
        if status == "needs_clarification" or na.get("tool") == "bc_answer_clarification":
            if not _answer_open_questions():
                _fail(t, "needs_clarification but no open questions found on disk")
        elif na.get("tool") == "bc_read_code_context":
            _call(tools, t, "bc_read_code_context", project_root=root, spec_name=spec)
        else:
            _fail(t, f"bc_prepare_review unfollowable: status={status} next_action={na}")
        out = _call(tools, t, "bc_prepare_review", project_root=root, spec_name=spec,
                    human_bullets=description, idempotency_key=f"e2e-walk-review-{attempt + 2}")
    review_path = out.get("review_path") or out.get("artifact_path")
    if not review_path or str(out.get("status", "")).startswith(("blocked", "needs")):
        _fail(t, "bc_prepare_review did not produce a review packet")

    out = _call(tools, t, "bc_request_approval", project_root=root, spec_name=spec,
                phase="plan", artifact_path=str(review_path),
                summary="e2e walk plan approval", idempotency_key="e2e-walk-plan-1")
    if str(out.get("status", "")).startswith(("blocked", "error")):
        _fail(t, "bc_request_approval refused")

    out = _call(tools, t, "bc_submit_decision", project_root=root, spec_name=spec,
                phase="plan", decision="approve")
    if str(out.get("status", "")).startswith(("blocked", "error")):
        _fail(t, "plan approval refused")

    # The human GO must flip the stage machine to implement.
    out = _call(tools, t, "bc_status", project_root=root, spec_name=spec)
    if out.get("stage") != "implement":
        _fail(t, f"stage did not flip to implement after plan approval (got {out.get('stage')!r})")
    # Recovery surface present on the resume packet.
    if "on_disk" not in out or not out["on_disk"]["key_files"]:
        _fail(t, "bc_status lost its on_disk recovery map")

    # --- evidence ---------------------------------------------------------
    evidence = "container=e2e-sim passed=1/1 (simulated walk evidence)"
    receipt = security.issue_evidence(
        project_root=root_path, spec_name=spec, producer="bc_run_tests",
        name="FacilitiesOverviewShowsAddressCount", result="pass", covers="all",
        layer="al-unit", evidence=evidence,
    )
    out = _call(tools, t, "bc_record_test", project_root=root, spec_name=spec,
                name="FacilitiesOverviewShowsAddressCount", result="pass", covers="all",
                layer="al-unit", evidence=evidence, evidence_receipt=receipt)
    if not out.get("recorded"):
        _fail(t, "bc_record_test refused simulated evidence")

    out = _call(tools, t, "bc_verify", project_root=root, spec_name=spec)
    if "coverage_pct" not in out:
        _fail(t, "bc_verify returned no coverage digest")

    out = _call(tools, t, "bc_status", project_root=root, spec_name=spec)
    if out.get("stage") != "verify":
        _fail(t, f"stage did not advance to verify (got {out.get('stage')!r})")

    # --- end gates --------------------------------------------------------
    # prepare_pr MUST refuse: no independent review ran, no branches exist. The
    # assertion is that the machine stays PRESCRIPTIVE at the very end — a
    # deterministic blocked_* with a named next_action, never a crash or a pass.
    out = _call(tools, t, "bc_prepare_pr", project_root=root, spec_name=spec)
    status = str(out.get("status", ""))
    if not status.startswith("blocked"):
        _fail(t, f"end gates FAILED OPEN: prepare_pr returned {status!r} with no review/branches")
    if not (out.get("next_action") or {}).get("tool"):
        _fail(t, f"end gate blocked without prescribing a next_action: {status}")

    # The whole story must be reconstructable from disk (context-loss armor).
    from bc_agentic_mcp import timeline
    phases = timeline.phases_in_order(root_path, spec, {
        "item_received", "spec_written", "design_planned", "tasks_broken_down",
        "review_prepared", "approval_requested", "decision_recorded", "plan_approved",
        "tests_run", "verified"})
    for must in ("item_received", "spec_written", "design_planned", "plan_approved", "verified"):
        if must not in phases:
            _fail(t, f"timeline lost the '{must}' beat — recovery story incomplete: {phases}")
