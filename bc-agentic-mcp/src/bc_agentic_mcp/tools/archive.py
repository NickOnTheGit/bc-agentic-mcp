"""bc_archive — close out a spec. See spec Section 3.16."""
from pathlib import Path
from bc_agentic_mcp.workspace import specs_root
from typing import Dict, Any, List

from bc_agentic_mcp.state import StateManager


def _incomplete_tasks(specs_dir: Path, spec_name: str) -> List[str]:
    """Return list of unchecked task IDs from TASKS.md, if it exists."""
    tasks_path = specs_dir / spec_name / "TASKS.md"
    if not tasks_path.exists():
        return []
    incomplete = []
    import re
    pattern = re.compile(r"^-\s+\[\s+\]\s+(T-\d+)", re.MULTILINE)
    for m in pattern.finditer(tasks_path.read_text(encoding="utf-8")):
        incomplete.append(m.group(1))
    return incomplete


def _verification_state(specs_dir: Path, spec_name: str) -> Dict[str, Any]:
    """Return {ran, coverage_pct, fully_validated} from VERIFICATION.md / verification data."""
    import json as _json
    from bc_agentic_mcp import checkpoints as memory, verification as _v
    root = specs_dir.parent
    charter_path = specs_dir / spec_name / "charter.json"
    if not charter_path.exists():
        return {"ran": False, "coverage_pct": 0.0, "fully_validated": False}
    tests = [c for c in memory.load_checkpoints(root, spec_name) if c.get("kind") == "test"]
    if not tests:
        return {"ran": False, "coverage_pct": 0.0, "fully_validated": False}
    try:
        digest = _v.build_verification(root, spec_name)
        return {
            "ran": True,
            "coverage_pct": digest.get("coverage_pct", 0.0),
            "fully_validated": digest.get("fully_validated", False),
        }
    except Exception:
        return {"ran": True, "coverage_pct": 0.0, "fully_validated": False}


async def handle_archive(
    project_root: str,
    spec_name: str,
    outcome: str = "merged",
    force: bool = False,
) -> Dict[str, Any]:
    """Mark a spec as closed with an outcome.

    Blocked when:
      - TASKS.md contains unchecked tasks (use bc_generate_tests + bc_run_tests first)
      - No test checkpoints exist (bc_verify requires at least one recorded test)

    Set force=true ONLY with explicit human confirmation that tests were run outside
    the MCP (e.g. manual container run) and evidence has been captured separately.
    """
    root = Path(project_root).resolve()
    specs_dir = specs_root(root)

    if not force:
        blockers: List[str] = []

        incomplete = _incomplete_tasks(specs_dir, spec_name)
        if incomplete:
            blockers.append(
                f"TASKS.md has {len(incomplete)} unchecked task(s): {incomplete}. "
                "Run bc_generate_tests and record results with bc_record_test before archiving."
            )

        ver = _verification_state(specs_dir, spec_name)
        if not ver["ran"]:
            blockers.append(
                "No test evidence recorded. Call bc_generate_tests, run the tests, "
                "then record results via bc_record_test and bc_verify before archiving."
            )

        if blockers:
            return {
                "archived": False,
                "blocked": True,
                "blockers": blockers,
                "next_action": {
                    "tool": "bc_generate_tests",
                    "reason": "Generate the test scaffold, run tests, record results, then re-call bc_archive.",
                    "params_hint": {"spec_name": spec_name, "project_root": project_root},
                },
                "override": "Set force=true (with human confirmation) to archive without test evidence.",
            }

    sm = StateManager(specs_dir)
    sm.archive_spec(spec_name, outcome)

    result: Dict[str, Any] = {
        "spec_name": spec_name,
        "status": "closed",
        "outcome": outcome,
        "forced": force,
    }

    # Bugfix learning loop: every archived bug feeds its VERIFIED root cause into the
    # lessons store. A recurring pattern is auto-promoted to a confirmed lesson that
    # analyzers surface proactively — the MCP gets better with every bug it closes.
    rc_path = specs_dir / spec_name / "root_cause.json"
    if rc_path.exists():
        try:
            import json as _json
            from bc_agentic_mcp import lessons as _lessons
            rc = _json.loads(rc_path.read_text(encoding="utf-8"))
            objects = [e.get("object") or e.get("ref", "") for e in rc.get("evidence", [])]
            keyword = (objects[0].split(" ")[-1] if objects and objects[0] else spec_name)
            lesson = _lessons.record_observation(
                root,
                code="BUG-PATTERN",
                message=(f"Bug pattern from {spec_name}: {rc.get('symptom', '')} — root cause: "
                         f"{rc.get('root_cause', '')} — fix: {rc.get('fix_approach', '')}")[:500],
                severity="warning",
                match={"keyword": keyword},
                spec_name=spec_name,
            )
            result["bug_lesson"] = {"id": lesson.get("id"), "status": lesson.get("status"),
                                    "hits": lesson.get("hits")}
            if lesson.get("status") == "confirmed":
                result["next_action"] = {
                    "tool": "bc_promote_lesson",
                    "reason": "This bug pattern has recurred — promote it to the cross-project store.",
                    "params_hint": {"lesson_id": lesson.get("id")},
                }
        except Exception:
            pass  # the learning loop must never block closeout

    return result
