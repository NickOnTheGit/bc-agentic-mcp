"""bc_request_approval + bc_submit_decision. See spec Sections 3.7, 3.8."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

from bc_agentic_mcp.validation import validate_phase, validate_decision, validate_idempotency_key


VALID_PHASES = {"spec", "design", "tasks", "implement", "complete"}
VALID_DECISIONS = {"approve", "reject", "request_changes"}


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
    approval_dir = root / ".specs" / spec_name / "approvals"
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
    }


async def handle_submit_decision(
    project_root: str,
    spec_name: str,
    phase: str,
    decision: str,
    feedback: str = "",
) -> Dict[str, Any]:
    """Record the human's decision on a pending approval."""
    validate_phase(phase, VALID_PHASES)
    validate_decision(decision, VALID_DECISIONS)

    root = Path(project_root).resolve()
    approval_path = root / ".specs" / spec_name / "approvals" / f"{phase}.md"

    if not approval_path.exists():
        return {
            "status": "error",
            "message": f"No pending approval for phase '{phase}'. Run bc_request_approval first.",
        }

    content = approval_path.read_text()
    content = content.replace("**Status:** pending", f"**Status:** {decision}")
    content = content.replace(f"- [ ] {decision}", f"- [x] {decision}")
    if feedback:
        content = content.replace("(to be filled by reviewer)", feedback)
    approval_path.write_text(content)

    next_actions = {
        "spec": "proceed_to_bc_plan_design",
        "design": "proceed_to_bc_breakdown_tasks",
        "tasks": "proceed_to_bc_implement",
        "implement": "proceed_to_bc_converge",
        "complete": "proceed_to_bc_archive",
    }

    return {
        "status": decision,
        "next_action": next_actions.get(phase, "unknown")
        if decision == "approve"
        else f"revisit_bc_{phase}",
        "audit_entry": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "decision": decision,
            "feedback": feedback,
        },
    }
