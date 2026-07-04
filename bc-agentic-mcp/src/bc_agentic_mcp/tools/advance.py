"""bc_advance — C3 composite driver: chain deterministic lifecycle steps server-side.

Executes the decisions of :mod:`bc_agentic_mcp.advance` through the server's own
``_run_tool`` pipeline (injected as ``run_tool``), so every chained step still gets
policy routing, the doom-loop guard, timeline recording, audit and output caps.
Idempotent: re-running after a stop resumes from the current phase.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bc_agentic_mcp import advance as decision
from bc_agentic_mcp import enforcement, timeline

MAX_STEPS_DEFAULT = 6


async def handle_advance(
    project_root: str,
    spec_name: str,
    run_tool: Callable[..., Any],
    max_steps: int = MAX_STEPS_DEFAULT,
    # Optional inputs that unlock conditional steps (never guessed, never defaulted):
    test_container_name: Optional[str] = None,
    test_extension_id: Optional[str] = None,
    credential_env: str = "BC_TEST_PASSWORD",
    app_project_folder: Optional[str] = None,
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    repository: Optional[str] = None,
    work_item_id: Optional[int] = None,
    target_branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Advance the item until a human gate, judgment step, block, or completion.

    ``test_container_name`` is deliberately NOT named ``container_name``: the outer
    call must not hold the per-container mutex that its inner bc_run_tests step needs.
    """
    root = Path(project_root).resolve()
    available: Dict[str, Any] = {
        "container_name": test_container_name,
        "test_extension_id": test_extension_id,
        "credential_env": credential_env,
        "app_project_folder": app_project_folder,
        "org_url": org_url,
        "project": project,
        "repository": repository,
        "work_item_id": work_item_id,
        "target_branch": target_branch,
    }
    base = {"project_root": str(root), "spec_name": spec_name}
    trail: List[Dict[str, Any]] = []

    def _finish(stop: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "status": "advanced" if trail else "no_step_taken",
            "stopped": stop.get("stop"),
            "reason": stop.get("reason"),
            "steps_taken": [t["tool"] for t in trail],
            "trail": trail,
            "current_phase": _phase(),
        }
        for key in ("next_action", "needed", "detail"):
            if stop.get(key) is not None:
                out[key] = stop[key]
        return out

    def _phase() -> Optional[str]:
        try:
            return timeline.current_phase(root, spec_name)
        except Exception:
            return None

    phase = _phase()

    # Pre-implementation phases also honour the enforcement engines: a red engine is a
    # judgment stop (its next_action names the fenced tool to use), never auto-answered.
    if phase in decision._PLANNING_PHASES or phase in {"approval_requested", "decision_recorded"}:
        try:
            status = enforcement.engine_status(root, spec_name)
            if status["next_actions"]:
                return _finish({
                    "stop": "waiting_judgment",
                    "reason": "Enforcement engines are blocking: " + "; ".join(status["blocking"][:3]),
                    "next_action": status["next_actions"][0],
                })
        except Exception:
            pass

    step = decision.seed_action(phase, available)
    for _ in range(max(1, int(max_steps))):
        if "action" not in step:
            return _finish(step)
        tool = step["action"]
        params = decision.build_step_params(tool, base, available)
        result = await run_tool(tool, **params)
        trail.append({
            "tool": tool,
            "status": (result or {}).get("status") if isinstance(result, dict) else None,
        })
        step = decision.next_after_result(tool, result, available)
        if step.get("stop") == "step_complete":
            step = decision.seed_action(_phase(), available)
    return _finish({"stop": "max_steps",
                    "reason": f"Stopped after {max_steps} steps — re-run bc_advance to continue."})
