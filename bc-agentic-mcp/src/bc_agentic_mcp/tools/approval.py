"""bc_request_approval + bc_submit_decision. See spec Sections 3.7, 3.8."""
from datetime import datetime, timezone
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any

from bc_agentic_mcp.validation import validate_phase, validate_decision, validate_idempotency_key
from bc_agentic_mcp import verification
from bc_agentic_mcp import authorization
from bc_agentic_mcp import checkpoints as memory
from bc_agentic_mcp.spec_loader import load_spec, validate_spec_contract


# C1: TWO canonical internal gates. `plan` covers spec+design+tasks in one review;
# `code` is normally satisfied BY the ADO PR approval (pr.record_code_gate_from_pr) —
# an internal `code` approval is the no-PR fallback. Legacy per-phase names are
# accepted as aliases for one release and map onto the canonical gates.
CANONICAL_GATES = {"plan", "code"}
_LEGACY_TO_GATE = {"spec": "plan", "design": "plan", "tasks": "plan",
                   "implement": "code", "complete": "code"}
VALID_PHASES = CANONICAL_GATES | set(_LEGACY_TO_GATE)
VALID_DECISIONS = {"approve", "reject", "request_changes"}


def _data_model_gate(root: Path, specs_dir: Path) -> "str | None":
    """Blocker text when a schema-affecting spec lacks a GRANTED data-model approval."""
    import json as _json

    spec_path = specs_dir / "spec.json"
    if not spec_path.exists():
        return None
    try:
        spec = _json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None
    from bc_agentic_mcp.guidelines_policy import _requires_data_model_approval
    if not _requires_data_model_approval(spec):
        return None
    approval_path = specs_dir / "data_model_approval.json"
    if not approval_path.exists():
        return ("data-model approval MISSING (schema change with no sign-off artifact "
                "data_model_approval.json)")
    try:
        data = _json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        return f"data_model_approval.json unreadable: {exc}"
    if not bool(data.get("approved", False)):
        return (f"data-model approval RECORDED BUT NOT GRANTED "
                f"(approver: {data.get('approver', 'unknown')})")
    return None


async def handle_approve_data_model(
    project_root: str,
    spec_name: str,
    approved: bool,
    approver: str,
    schema_changes: str,
    notes: str = "",
) -> Dict[str, Any]:
    """Record the HUMAN data-model approval for schema-affecting changes.

    ERP guideline: any table/field/enum change needs an explicit data-model sign-off.
    This writes ``data_model_approval.json`` — the artifact the GL-DM001 quality rule
    checks — with WHO approved WHAT, so the decision is auditable, not implicit.
    """
    import json as _json
    from datetime import datetime, timezone

    root = Path(project_root).resolve()
    sdir = specs_root(root) / spec_name
    sdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "approved": bool(approved),
        "approver": approver,
        "schema_changes": schema_changes,
        "notes": notes,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = sdir / "data_model_approval.json"
    path.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
    try:
        memory.append_checkpoint(
            root, spec_name, kind="decision",
            summary=f"Data-model {'approval' if approved else 'REJECTION'} by {approver}: {schema_changes[:160]}",
            details=payload,
        )
    except Exception:
        pass
    return {
        "recorded": True,
        "approved": bool(approved),
        "approval_path": str(path),
        "clears": "GL-DM001" if approved else None,
    }


def _feature_plan_gate(root: Path, specs_dir: Path) -> list:
    """Deterministic feature-tier plan-gate checks (FEATURE-PLAN + refinement)."""
    import json as _json

    blockers: list = []
    plan_md = specs_dir / "FEATURE-PLAN.md"
    if not plan_md.exists():
        blockers.append("FEATURE-PLAN.md missing (run bc_plan_feature)")
    refinement = specs_dir / "feature_refinement.json"
    if not refinement.exists():
        blockers.append("feature refinement missing — claims never confronted with code "
                        "reality (run bc_refine_feature)")
        return blockers
    try:
        data = _json.loads(refinement.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        blockers.append(f"feature_refinement.json unreadable: {exc}")
        return blockers
    counts = (data.get("findings") or {}).get("counts") or {}
    problems = int(counts.get("mismatches", 0)) + int(counts.get("conflicts", 0))
    if problems and not str(data.get("critique", "")).strip():
        blockers.append(
            f"{problems} refinement mismatch(es)/conflict(s) with NO recorded judgment — "
            "re-run bc_refine_feature with `critique` addressing each finding")
    # freshness: the plan must postdate the captured tree.
    manifest = specs_dir / "context" / "manifest.json"
    plan_json = specs_dir / "feature_plan.json"
    if manifest.exists() and plan_json.exists():
        try:
            captured_at = str(_json.loads(manifest.read_text(encoding="utf-8")).get("captured_at", ""))
            generated_at = str(_json.loads(plan_json.read_text(encoding="utf-8")).get("generated_at", ""))
            if captured_at and generated_at and generated_at < captured_at:
                blockers.append("feature plan is STALE — the tree was re-captured after it "
                                "was generated (re-run bc_plan_feature)")
        except (OSError, _json.JSONDecodeError):
            pass
    # MEGA-REVIEW gate (one-approval model, human decision 2026-07-04): once ANY child
    # item spec has been authored, the single feature plan approval must cover ALL of
    # them via a fresh FEATURE-REVIEW.md — never approve a partial view. Before item
    # authoring starts (wave-plan-only review), this gate stays silent.
    try:
        from bc_agentic_mcp.tools.feature import feature_children_specs
        children = feature_children_specs(root, specs_dir.name)
        authored = [c for c in children if c["item_spec"]]
        if authored:
            review_md = specs_dir / "FEATURE-REVIEW.md"
            if not review_md.exists():
                blockers.append(
                    f"{len(authored)} item spec(s) authored but NO feature-wide review packet — "
                    "run bc_prepare_feature_review so ONE approval covers every item")
            else:
                review_mtime = review_md.stat().st_mtime
                for c in authored:
                    spec_file = specs_dir.parent / c["item_spec"] / "spec.json"
                    if spec_file.exists() and spec_file.stat().st_mtime > review_mtime:
                        blockers.append(
                            f"FEATURE-REVIEW.md is STALE — item spec '{c['item_spec']}' changed "
                            "after it was generated (re-run bc_prepare_feature_review)")
                        break
    except Exception:
        pass  # child resolution must never crash the gate; item gates still protect writes
    return blockers
# Phases whose approval means "the work is proven done" — these must pass the evidence gate.
_VERIFIED_PHASES = {"implement", "complete", "code"}
_TRACEABILITY_PHASES = {"tasks", "implement", "complete", "plan", "code"}
_TEST_LIFECYCLE_PHASES = {"implement", "complete", "code"}
_LOCAL_TEST_PHASES = {"implement", "complete", "code"}


def _test_lifecycle_status(root: Path, spec_name: str) -> Dict[str, Any]:
    """Return whether test generation and execution phases were recorded for this spec."""
    cps = memory.load_checkpoints(root, spec_name)
    phase_events = [c for c in cps if c.get("kind") == "phase"]
    phases = [
        (c.get("details") or {}).get("phase")
        for c in phase_events
        if isinstance(c.get("details"), dict)
    ]
    generated = "tests_generated" in phases
    executed = "tests_run" in phases
    return {
        "generated": generated,
        "executed": executed,
        "ok": generated and executed,
        "phase_events": len(phase_events),
    }


async def handle_request_approval(
    project_root: str,
    spec_name: str,
    phase: str,
    artifact_path: str,
    summary: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    """Submit a phase artifact for human review."""
    validate_phase(phase, VALID_PHASES)
    validate_idempotency_key(idempotency_key)

    root = Path(project_root).resolve()

    # PRESENTATION WALL (user catch 2026-07-04, wi267598): the human gate is
    # meaningless when the human never sees the artifact they approve. Two walls:
    # (1) fail-closed on a missing/empty artifact — a path string was accepted
    # unverified before; (2) the response CARRIES the artifact content
    # (present_to_human) so the orchestrator cannot paraphrase from memory.
    artifact = Path(artifact_path)
    if not artifact.is_file():
        return {
            "status": "blocked_artifact_missing",
            "blocked": True,
            "reason": (f"Approval refused: artifact '{artifact_path}' does not exist. "
                       "Generate the review packet first (bc_prepare_review) and pass "
                       "its real path — the human must review the actual artifact."),
            "next_action": {"tool": "bc_prepare_review",
                             "params_hint": {"spec_name": spec_name}},
        }
    try:
        artifact_text = artifact.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "status": "blocked_artifact_unreadable",
            "blocked": True,
            "reason": f"Approval refused: artifact unreadable: {exc}",
        }
    if not artifact_text.strip():
        return {
            "status": "blocked_artifact_empty",
            "blocked": True,
            "reason": "Approval refused: the artifact is empty — nothing to review.",
        }

    approval_dir = specs_root(root) / spec_name / "approvals"
    approval_dir.mkdir(parents=True, exist_ok=True)

    approval_path = approval_dir / f"{phase}.md"
    content = f"""# Approval: {spec_name} — {phase.title()} Phase

**Status:** pending
**Submitted:** {datetime.now(timezone.utc).isoformat()}
**Artifact:** {artifact_path}

## Summary
{summary}

## Decision
- [ ] approve
- [ ] reject
- [ ] request_changes

## Feedback
(to be filled by reviewer)

---
Edit this file's 'Decision' section, then call bc_submit_decision.
"""
    approval_path.write_text(content, encoding="utf-8")

    return {
        "approval_path": str(approval_path),
        "status": "pending",
        "instructions": f"Edit {approval_path} to approve/reject, then call bc_submit_decision.",
        # The orchestrator MUST render this to the human verbatim (or link the file in
        # a shared location) — an approval given on a paraphrase is not an approval.
        "present_to_human": {
            "artifact_path": str(artifact),
            "content": artifact_text[:30000],
            "truncated": len(artifact_text) > 30000,
            "instruction": ("Show this review packet to the human BEFORE asking for the "
                             "decision. Do not summarize it from memory."),
        },
    }


async def handle_submit_decision(
    project_root: str,
    spec_name: str,
    phase: str,
    decision: str,
    feedback: str = "",
    override_reason: str = "",
) -> Dict[str, Any]:
    """Record the human's decision on a pending approval.

    Fail-closed enforcement: an ``approve`` on a proven-done phase (implement/complete) is
    BLOCKED unless the verification gate passes (every acceptance criterion covered by a
    passing test AND meeting its evidence bar). A blocked approval is not written. An
    explicit ``override_reason`` allows a deliberate bypass, which is stamped loudly into
    the audit entry — never silent.
    """
    validate_phase(phase, VALID_PHASES)
    validate_decision(decision, VALID_DECISIONS)

    root = Path(project_root).resolve()
    approval_path = specs_root(root) / spec_name / "approvals" / f"{phase}.md"

    if not approval_path.exists():
        return {
            "status": "error",
            "message": f"No pending approval for phase '{phase}'. Run bc_request_approval first.",
        }

    gate_result = None
    local_test_result = None
    overridden = False
    specs_dir = specs_root(root) / spec_name
    contract_issues = []

    # FEATURE-TIER plan gate: a feature folder's review packet is FEATURE-PLAN.md +
    # FEATURE-REFINEMENT (claims x code reality) — not the item-scoped REVIEW.md
    # machinery. Detected by the feature_plan.json artifact bc_plan_feature writes.
    is_feature_folder = (specs_dir / "feature_plan.json").exists()
    if decision == "approve" and phase == "plan" and is_feature_folder:
        feature_blockers = _feature_plan_gate(root, specs_dir)
        if feature_blockers and not override_reason.strip():
            return {
                "status": "blocked",
                "phase": phase,
                "message": (
                    "Feature plan approval blocked: " + "; ".join(feature_blockers)
                    + " Fix via bc_refine_feature / bc_plan_feature, or provide "
                    "override_reason for an explicit bypass."
                ),
                "blockers": feature_blockers,
            }
        if feature_blockers:
            overridden = True
    elif decision == "approve" and phase in _TRACEABILITY_PHASES:
        fresh_review, freshness_reason = authorization.review_is_fresh(root, spec_name)
        if not fresh_review and not override_reason.strip():
            return {
                "status": "blocked",
                "phase": phase,
                "message": (
                    "Approval blocked: review packet is stale or incomplete. "
                    "Regenerate with bc_prepare_review and re-submit approval, "
                    "or provide override_reason for an explicit bypass."
                ),
                "blockers": [freshness_reason],
            }
        if not fresh_review and override_reason.strip():
            overridden = True

        try:
            spec_path = specs_dir / "spec.json"
            if spec_path.exists():
                spec = load_spec(specs_dir)
                contract_issues = validate_spec_contract(spec, strict_schema=True)
            else:
                contract_issues = []
        except Exception as exc:
            contract_issues = [f"spec load failed: {exc}"]
        if contract_issues and not override_reason.strip():
            return {
                "status": "blocked",
                "phase": phase,
                "message": (
                    "Approval blocked: end-to-end traceability/spec contract gate did not pass. "
                    "Fix missing mappings/upgrade contract in spec.json or provide override_reason "
                    "for an explicit bypass."
                ),
                "blockers": contract_issues,
            }
        if contract_issues and override_reason.strip():
            overridden = True
    if decision == "approve" and phase in _VERIFIED_PHASES:
        # DATA-MODEL APPROVAL is a MERGE BLOCKER for schema-affecting changes.
        # Team process: create a Task under the PBI asking ANOTHER developer for the
        # data-model sign-off; the PR cannot merge until it is granted. Recorded via
        # bc_approve_data_model -> data_model_approval.json (checked here + prepare_pr).
        dm = _data_model_gate(root, specs_dir)
        if dm is not None and not override_reason.strip():
            return {
                "status": "blocked",
                "phase": phase,
                "message": (
                    "Approval blocked: this change affects the DATA MODEL (table/field/enum) "
                    "and has no granted data-model approval. Team process: add a Task under "
                    "the PBI and have ANOTHER developer review the schema change; record the "
                    "grant via bc_approve_data_model. The PR cannot merge until granted."
                ),
                "blockers": [dm],
                "next_action": {
                    "tool": "bc_approve_data_model",
                    "reason": "Record the second developer's data-model sign-off",
                    "params_hint": {"spec_name": spec_name, "approved": True,
                                     "approver": "<other developer>",
                                     "schema_changes": "<what changed in the schema>"},
                },
            }
        if dm is not None:
            overridden = True
        gate_result = verification.gate(root, spec_name)
        if not gate_result["passed"]:
            if not override_reason.strip():
                validation_classes = gate_result.get("digest", {}).get("validation_classes") or {}
                missing_classes = [
                    name for name, state in validation_classes.items()
                    if state.get("required") and not state.get("ok")
                ]
                class_summary = (
                    " Missing validation classes: " + ", ".join(missing_classes) + "."
                    if missing_classes else ""
                )
                return {
                    "status": "blocked",
                    "phase": phase,
                    "message": (
                        "Approval blocked: the verification gate did not pass. Record the "
                        "missing evidence (bc_run_tests / bc_api_contract / bc_record_test) or "
                        "pass an explicit override_reason to bypass deliberately."
                        + class_summary
                    ),
                    "blockers": gate_result["blockers"],
                    "coverage_pct": gate_result["digest"]["coverage_pct"],
                    "required_strength": gate_result["digest"]["required_strength_label"],
                    "missing_validation_classes": missing_classes,
                }
            overridden = True

    if decision == "approve" and phase in _TEST_LIFECYCLE_PHASES:
        test_lifecycle = _test_lifecycle_status(root, spec_name)
        if not test_lifecycle["ok"]:
            if not override_reason.strip():
                missing = []
                if not test_lifecycle["generated"]:
                    missing.append("tests not generated (run bc_generate_tests)")
                if not test_lifecycle["executed"]:
                    missing.append("tests not executed (run bc_run_tests)")
                return {
                    "status": "blocked",
                    "phase": phase,
                    "message": (
                        "Approval blocked: testing is mandatory in the live lifecycle. "
                        "Generate tests for the item and execute them before approval."
                    ),
                    "blockers": missing,
                    "next_action": {
                        "tool": "bc_generate_tests" if not test_lifecycle["generated"] else "bc_run_tests",
                        "reason": "Complete the required test lifecycle for this item.",
                        "params_hint": {"spec_name": spec_name},
                    },
                    "test_lifecycle": test_lifecycle,
                }
            overridden = True

    if decision == "approve" and phase in _LOCAL_TEST_PHASES:
        local_test_result = verification.local_container_evidence(root, spec_name)
        if not local_test_result["ok"]:
            if not override_reason.strip():
                return {
                    "status": "blocked",
                    "phase": phase,
                    "message": (
                        "Approval blocked: no passing local-container test evidence recorded for this item. "
                        "Run local container validation and record it (bc_record_test with container evidence). "
                        "For API evidence, include full endpoint URL and an absolute local evidence artifact path "
                        "(or file:/// URI) that points to an existing file; JSON artifacts must include "
                        "the full scenario schema (name, scenarioDescription, validates, method, endpoint, body, "
                        "expected, actual, statusCode, passed, responseMessage, responseBody) for every scenario. "
                        "For non-API evidence, include execution proof (for example passed=X/Y and/or exit=0), "
                        "or pass override_reason for an explicit bypass."
                    ),
                    "blockers": [
                        "Missing acceptable local-container validation checkpoint "
                        "(kind=test, result=pass, evidence must satisfy API or non-API evidence rules)."
                    ],
                    "local_testing": local_test_result,
                }
            overridden = True

    content = approval_path.read_text()
    content = content.replace("**Status:** pending", f"**Status:** {decision}")
    content = content.replace(f"- [ ] {decision}", f"- [x] {decision}")
    if feedback:
        content = content.replace("(to be filled by reviewer)", feedback)
    approval_path.write_text(content)

    # ONE-APPROVAL CASCADE (human decision 2026-07-04): approving the feature-wide
    # plan gate approves the plan of EVERY authored child item — no per-item human
    # gates. Each child gets a real approvals/plan.md (so implementation_authorized
    # passes) plus a plan_approved timeline phase, both stamped with the source.
    cascaded: list = []
    if decision == "approve" and phase == "plan" and is_feature_folder:
        try:
            from bc_agentic_mcp.tools.feature import feature_children_specs
            from bc_agentic_mcp import timeline as _timeline
            for child in feature_children_specs(root, spec_name):
                item_spec = child.get("item_spec")
                if not item_spec:
                    continue
                child_dir = specs_root(root) / item_spec / "approvals"
                child_dir.mkdir(parents=True, exist_ok=True)
                child_path = child_dir / "plan.md"
                child_path.write_text(
                    f"# Approval: {item_spec} — Plan Phase\n\n"
                    f"**Status:** approve\n"
                    f"**Submitted:** {datetime.now(timezone.utc).isoformat()}\n"
                    f"**Artifact:** ../{spec_name}/FEATURE-REVIEW.md\n\n"
                    f"## Summary\nApproved via the feature-wide review gate on '{spec_name}' "
                    f"(one decision covers every item — see FEATURE-REVIEW.md).\n\n"
                    f"## Decision\n- [x] approve\n",
                    encoding="utf-8")
                try:
                    _timeline.record_phase(root, item_spec, "plan_approved",
                                           summary=f"Plan approved via feature gate '{spec_name}'")
                except Exception:
                    pass
                cascaded.append(item_spec)
        except Exception:
            pass  # cascade failure must not lose the recorded feature decision

    next_actions = {
        "plan": "proceed_to_bc_implement",
        "code": "proceed_to_bc_prepare_pr",
        "spec": "proceed_to_bc_plan_design",
        "design": "proceed_to_bc_breakdown_tasks",
        "tasks": "proceed_to_bc_implement",
        "implement": "proceed_to_bc_converge",
        "complete": "proceed_to_bc_archive",
    }

    audit_entry: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "decision": decision,
        "feedback": feedback,
    }
    if gate_result is not None:
        audit_entry["gate_passed"] = gate_result["passed"]
        audit_entry["coverage_pct"] = gate_result["digest"]["coverage_pct"]
    if local_test_result is not None:
        audit_entry["local_testing"] = local_test_result
    if decision == "approve" and phase in _TEST_LIFECYCLE_PHASES:
        audit_entry["test_lifecycle"] = _test_lifecycle_status(root, spec_name)
    if contract_issues:
        audit_entry["spec_contract_issues"] = contract_issues
    if overridden:
        audit_entry["evidence_override"] = True
        audit_entry["override_reason"] = override_reason
        if gate_result is not None and not gate_result["passed"]:
            audit_entry["overridden_blockers"] = gate_result["blockers"]
        elif contract_issues:
            audit_entry["overridden_blockers"] = contract_issues

    result: Dict[str, Any] = {
        "status": decision,
        "next_action": next_actions.get(phase, "unknown")
        if decision == "approve"
        else f"revisit_bc_{phase}",
        "audit_entry": audit_entry,
    }
    # Gate-aware lifecycle phase: an approved PLAN gate opens the implement stage —
    # the generic 'decision_recorded' phase mapped to verify, locking out the very
    # implement tools the decision's own next_action prescribes.
    gate = _LEGACY_TO_GATE.get(phase, phase)
    if decision == "approve" and gate == "plan":
        result["_timeline_phase"] = "plan_approved"
    elif decision != "approve":
        result["_timeline_phase"] = "review_prepared"  # rework re-opens planning
    if cascaded:
        result["cascaded_plan_approvals"] = cascaded
        result["human_cascade"] = (
            f"This ONE approval also approved the plan of {len(cascaded)} item(s): "
            + ", ".join(cascaded))
    if phase in _LEGACY_TO_GATE:
        result["canonical_gate"] = _LEGACY_TO_GATE[phase]
        result["deprecation"] = (
            f"Phase '{phase}' is a legacy alias; use the canonical gate "
            f"'{_LEGACY_TO_GATE[phase]}' (plan | code)."
        )
    if overridden:
        result["evidence_override"] = True
        result["warning"] = "Approved despite an unmet verification gate (override recorded)."
    return result
