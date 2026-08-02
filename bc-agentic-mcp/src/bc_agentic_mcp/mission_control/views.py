"""views — read-only projections over the .specs workspace for the cockpit.

Polling endpoints use ONLY these direct file reads (state.json, checkpoints.jsonl,
clarifications.md, approvals/*.md) so a browser refreshing every few seconds never
spams the MCP audit log. Mutating actions go through bridge.McpBridge instead.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root
from bc_agentic_mcp import timeline, workflow_policy

SPEC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Canonical delivery pipeline (order mirrors timeline.TOOL_PHASE / advance.py).
ITEM_PIPELINE: List[str] = [
    "item_received", "spec_written", "design_planned", "code_context_built",
    "tasks_broken_down", "review_prepared", "approval_requested", "decision_recorded",
    "implemented", "tests_generated", "tests_run", "verified", "reviewed",
    "pr_prepared", "pr_created", "merged", "archived",
]
FEATURE_PIPELINE: List[str] = [
    "feature_captured", "feature_refined", "feature_planned",
    # The C1 plan gate is shared with the item tier — a feature passes it too.
    "approval_requested", "decision_recorded",
    "item_refined",
]
INTAKE_PIPELINE: List[str] = ["intake_started", "intake_analyzed", "intake_graduated"]

_REVIEW_PRIORITY = [
    "ITEM.md", "ROOT-CAUSE.md", "TDD.md", "DESIGN.md", "TASKS.md", "REVIEW.md",
    "ITEM-REFINEMENT.md", "clarifications.md", "charter.md", "TIMELINE.md",
]

_MAX_ARTIFACT_BYTES = 1_000_000


def valid_spec_name(name: str) -> bool:
    return bool(SPEC_NAME_RE.match(name or ""))


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _read_checkpoints(item_dir: Path, limit: int = 400) -> List[Dict[str, Any]]:
    path = item_dir / "checkpoints.jsonl"
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    events.append(rec)
            except (json.JSONDecodeError, ValueError):
                continue
    except OSError:
        return []
    return events


def _phase_history(events: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for e in events:
        if e.get("kind") == "phase":
            phase = (e.get("details") or {}).get("phase")
            if phase and phase not in seen:
                seen.append(phase)
    return seen


def _pipeline_for(history: List[str], lane: str = "") -> List[str]:
    # Only feature_* phases mark a FEATURE folder. item_refined alone does not:
    # ordinary items record it too when refined as part of a feature.
    if any(p.startswith("intake_") for p in history):
        return INTAKE_PIPELINE
    if any(p.startswith("feature_") for p in history):
        return FEATURE_PIPELINE
    if lane == "bugfix" or "root_cause_identified" in history:
        # Bug lane: diagnosis-before-planning — root cause sits right after capture.
        pipeline = list(ITEM_PIPELINE)
        pipeline.insert(1, "root_cause_identified")
        return pipeline
    return ITEM_PIPELINE


def _lane(item_dir: Path) -> str:
    """Delivery lane from the captured identity ('bugfix' for Bug work items)."""
    if (item_dir / "root_cause.json").exists():
        return "bugfix"
    manifest = _read_json(item_dir / "context" / "manifest.json")
    return str((manifest.get("identity") or {}).get("lane") or "pbi").lower()


def _progress(history: List[str], lane: str = "") -> Dict[str, Any]:
    pipeline = _pipeline_for(history, lane)
    current = None
    for p in reversed(pipeline):
        if p in history:
            current = p
            break
    idx = pipeline.index(current) if current else -1
    return {
        "pipeline": pipeline,
        "done": [p for p in pipeline if p in history],
        "current": current,
        "percent": round(100 * (idx + 1) / len(pipeline)) if idx >= 0 else 0,
    }


def parse_clarifications(item_dir: Path) -> List[Dict[str, Any]]:
    """Parse clarifications.md into [{id, question, answer, open}]."""
    path = item_dir / "clarifications.md"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    questions: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in lines:
        m = re.match(r"^##\s+(Q-\d+):\s*(.+)$", line.strip())
        if m:
            current = {"id": m.group(1), "question": m.group(2).strip(), "answer": "", "options": []}
            questions.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("- [") and len(stripped) > 5:
            current["options"].append(stripped[5:].strip())
        elif stripped.startswith("_Answer:_"):
            current["answer"] = stripped[len("_Answer:_"):].strip()
        elif current["answer"] and stripped and not stripped.startswith("#"):
            current["answer"] += " " + stripped  # tolerate multi-line answers
    for q in questions:
        q["open"] = not q["answer"]
    return questions


def parse_approvals(item_dir: Path) -> List[Dict[str, Any]]:
    """List approvals/*.md with their Status field (pending = human gate open)."""
    approvals_dir = item_dir / "approvals"
    if not approvals_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(approvals_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        status = "unknown"
        m = re.search(r"\*\*Status:\*\*\s*(\S+)", text)
        if m:
            status = m.group(1)
        summary = ""
        sm = re.search(r"## Summary\s*\n(.+?)(?:\n##|\Z)", text, re.DOTALL)
        if sm:
            summary = sm.group(1).strip()[:500]
        out.append({"phase": path.stem, "status": status, "summary": summary,
                    "file": f"approvals/{path.name}"})
    return out


def list_artifacts(item_dir: Path) -> List[Dict[str, Any]]:
    """Top-level + approvals/ + context/ files, review candidates first."""
    if not item_dir.is_dir():
        return []
    files: List[Dict[str, Any]] = []
    seen = set()

    def add(path: Path, rel: str) -> None:
        if rel in seen or not path.is_file():
            return
        seen.add(rel)
        try:
            stat = path.stat()
        except OSError:
            return
        files.append({
            "name": rel,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "review": rel in _REVIEW_PRIORITY or rel.startswith("approvals/"),
        })

    for rel in _REVIEW_PRIORITY:
        add(item_dir / rel, rel)
    for path in sorted(item_dir.glob("*")):
        if path.is_file():
            add(path, path.name)
    for sub in ("approvals", "context", "context/related"):
        subdir = item_dir / sub
        if subdir.is_dir():
            for path in sorted(subdir.glob("*")):
                if path.is_file():
                    add(path, f"{sub}/{path.name}")
    return files


def read_artifact(project_root: Path, spec_name: str, rel: str) -> Dict[str, Any]:
    """Guarded read of one artifact inside the item's spec folder only."""
    if not valid_spec_name(spec_name):
        return {"error": "invalid spec name"}
    item_dir = (specs_root(project_root) / spec_name).resolve()
    try:
        target = (item_dir / rel).resolve()
    except (OSError, ValueError):
        return {"error": "invalid path"}
    if not target.is_relative_to(item_dir):
        return {"error": "path escapes item folder"}
    if not target.is_file():
        return {"error": "not found"}
    if target.stat().st_size > _MAX_ARTIFACT_BYTES:
        return {"error": "file too large for viewer"}
    try:
        return {"name": rel, "content": target.read_text(encoding="utf-8", errors="replace")}
    except OSError as exc:
        return {"error": str(exc)}


def overview(project_root: Path) -> Dict[str, Any]:
    """All items with phase + progress (pure file reads).

    Union of state.json registrations and on-disk spec folders — feature folders
    created by bc_capture_feature exist on disk without a state.json entry.
    """
    base = specs_root(project_root)
    state = _read_json(base / "state.json")
    names = dict.fromkeys(state.get("specs") or {})
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if (child.is_dir() and not child.name.startswith(".")
                    and valid_spec_name(child.name)
                    and (child / "checkpoints.jsonl").exists()):
                names.setdefault(child.name)
    items: List[Dict[str, Any]] = []
    for name in names:
        entry = (state.get("specs") or {}).get(name) or {}
        item_dir = base / name
        events = _read_checkpoints(item_dir)
        history = _phase_history(events)
        lane = _lane(item_dir)
        prog = _progress(history, lane)
        last_event_ts = events[-1].get("ts", "") if events else ""
        open_questions = sum(1 for q in parse_clarifications(item_dir) if q["open"])
        pending_approvals = sum(1 for a in parse_approvals(item_dir) if a["status"] == "pending")
        items.append({
            "name": name,
            "phase": prog["current"] or entry.get("phase") or "new",
            "percent": prog["percent"],
            "is_feature": prog["pipeline"] == FEATURE_PIPELINE,
            "is_intake": prog["pipeline"] == INTAKE_PIPELINE,
            "lane": lane,
            "open_questions": open_questions,
            "pending_approvals": pending_approvals,
            "last_activity": entry.get("last_activity") or last_event_ts,
        })
    items.sort(key=lambda i: i.get("last_activity") or "", reverse=True)
    return {
        "project_root": str(project_root),
        "specs_root": str(base),
        "active_spec": state.get("active_spec"),
        "items": items,
    }


def _readiness(project_root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    """Verification gate digest (pure disk reads) — the 'am I approval-ready?' truth."""
    try:
        from bc_agentic_mcp import verification
        g = verification.gate(project_root, spec_name)
        d = g.get("digest") or {}
        return {
            "passed": bool(g.get("passed")),
            "blockers": (g.get("blockers") or [])[:10],
            "coverage_pct": d.get("coverage_pct", 0),
            "criteria_count": d.get("criteria_count", 0),
            "required_strength": d.get("required_strength_label", ""),
            "validation_classes": {
                name: {"required": bool(s.get("required")), "ok": bool(s.get("ok")),
                       "reason": str(s.get("reason") or "")[:160]}
                for name, s in (d.get("validation_classes") or {}).items()
            },
        }
    except Exception:
        return None


def _dossier_summary(item_dir: Path) -> Optional[Dict[str, Any]]:
    """Compact refinement dossier for the cockpit (intake missions only)."""
    d = _read_json(item_dir / "dossier.json")
    if not d:
        return None
    return {
        "lane": d.get("lane") or {},
        "precedents": (d.get("precedents") or [])[:5],
        "code_reality": (d.get("code_reality") or [])[:8],
        "open_questions": (d.get("open_questions") or [])[:8],
        "generated_at": d.get("generated_at", ""),
    }


def git_diff(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Read-only git diff of the item's allowed files (falls back to whole repo stat)."""
    if not valid_spec_name(spec_name):
        return {"error": "invalid spec name"}
    spec = _read_json(specs_root(project_root) / spec_name / "spec.json")
    allowed = [f for f in ((spec.get("scope_boundaries") or {}).get("allowed_files") or [])
               if isinstance(f, str) and not f.startswith("-")]
    import subprocess
    def run(args: List[str]) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=str(project_root), capture_output=True,
                                  text=True, timeout=30, encoding="utf-8", errors="replace")
            return proc.stdout or proc.stderr or ""
        except Exception as exc:
            return f"git failed: {exc}"
    scope = ["--", *allowed] if allowed else []
    stat = run(["diff", "HEAD", "--stat", *scope])
    patch = run(["diff", "HEAD", *scope])
    untracked = run(["status", "--porcelain", *scope])
    return {
        "allowed_files": allowed,
        "stat": stat[:20_000],
        "patch": patch[:200_000],
        "untracked": untracked[:5_000],
        "truncated": len(patch) > 200_000,
    }


def item_pulse(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """Everything the cockpit needs for one item (pure file reads)."""
    if not valid_spec_name(spec_name):
        return {"error": "invalid spec name"}
    base = specs_root(project_root)
    item_dir = base / spec_name
    if not item_dir.is_dir():
        return {"error": f"item '{spec_name}' not found", "exists": False}
    events = _read_checkpoints(item_dir)
    history = _phase_history(events)
    charter = _read_json(item_dir / "charter.json")
    recent = [
        {
            "ts": e.get("ts", ""),
            "kind": e.get("kind", ""),
            "summary": e.get("summary", ""),
            "phase": (e.get("details") or {}).get("phase"),
            "status": (e.get("details") or {}).get("status"),
        }
        for e in events[-60:]
        if e.get("kind") in {"phase", "milestone", "mistake", "correction",
                             "gate", "decision", "reflection", "override"}
    ]
    recent.reverse()
    lane = _lane(item_dir)
    progress = _progress(history, lane)
    # Mirror the server's stage routing so the UI never offers a policy-blocked action.
    stage = workflow_policy._phase_to_stage(progress["current"])
    pr_record = _read_json(item_dir / "pr" / "pr.json")
    worktree = _read_json(item_dir / "worktree.json")
    rc = _read_json(item_dir / "root_cause.json")
    rubric_history = _read_json(item_dir / "review_rubric.json")
    latest_rubric = rubric_history[-1] if isinstance(rubric_history, list) and rubric_history else None
    graduation = _read_json(item_dir / "graduation.json")
    consistency = _read_json(item_dir / "consistency.json")
    root_cause = None
    if rc:
        root_cause = {
            "symptom": str(rc.get("symptom") or "")[:400],
            "root_cause": str(rc.get("root_cause") or "")[:600],
            "fix_approach": str(rc.get("fix_approach") or "")[:400],
            "evidence_count": len(rc.get("evidence") or []),
        }
    return {
        "exists": True,
        "name": spec_name,
        "item_dir": str(item_dir),
        "progress": progress,
        "lane": lane,
        "root_cause": root_cause,
        "worktree": ({"path": worktree.get("path"), "branch": worktree.get("branch"),
                      "exists": Path(str(worktree.get("path", ""))).is_dir()}
                     if worktree else None),
        "stage": stage,
        # bc_clarify / bc_answer_clarification / bc_auto_clarify are plan-stage tools.
        "clarifications_locked": stage != "plan",
        "is_feature": progress["pipeline"] == FEATURE_PIPELINE,
        "is_intake": progress["pipeline"] == INTAKE_PIPELINE,
        "dossier": _dossier_summary(item_dir),
        "graduation": graduation or None,
        "rubric": ({"overall": latest_rubric.get("overall"), "passed": latest_rubric.get("passed"),
                    "scores": latest_rubric.get("scores") or {},
                    "count": len(rubric_history)} if latest_rubric else None),
        "consistency": ({"status": consistency.get("status"), "ok": consistency.get("ok"),
                         "critical": (consistency.get("critical") or [])[:6],
                         "warnings": (consistency.get("warnings") or [])[:6]}
                        if consistency else None),
        "readiness": _readiness(project_root, spec_name),
        "pr": ({"pr_id": pr_record.get("pr_id"), "url": pr_record.get("url", ""),
                "source_branch": pr_record.get("source_branch", ""),
                "target_branch": pr_record.get("target_branch", "")}
               if pr_record else None),
        "charter": {
            "purpose": (charter.get("purpose") or "")[:600],
            "acceptance_criteria": charter.get("acceptance_criteria") or [],
        },
        "clarifications": parse_clarifications(item_dir),
        "approvals": parse_approvals(item_dir),
        "artifacts": list_artifacts(item_dir),
        "events": recent,
    }

ATLAS_LAYER_NODES: List[Dict[str, Any]] = [
    {"id": "layer:cockpit", "title": "Mission Control cockpit", "kind": "layer", "summary": "Human-facing browser control surface for launch, review, and deterministic orchestration.", "files": ["src/bc_agentic_mcp/mission_control/app.py", "src/bc_agentic_mcp/mission_control/static/index.html", "src/bc_agentic_mcp/mission_control/static/app.js"]},
    {"id": "layer:projection", "title": "Read projections", "kind": "layer", "summary": "File-backed projections over .specs used for high-frequency polling without action side-effects.", "files": ["src/bc_agentic_mcp/mission_control/views.py"]},
    {"id": "layer:bridge", "title": "MCP bridge", "kind": "layer", "summary": "Single serialized stdio client to the real MCP server so all gates remain active.", "files": ["src/bc_agentic_mcp/mission_control/bridge.py"]},
    {"id": "layer:runtime", "title": "Server runtime", "kind": "layer", "summary": "Tool registration, audit, retries, and timeout envelope around each MCP call.", "files": ["src/bc_agentic_mcp/server.py", "src/bc_agentic_mcp/tool_defense.py"]},
    {"id": "layer:policy", "title": "Policy + gates", "kind": "layer", "summary": "Role/stage allowlists and deterministic enforcement walls.", "files": ["src/bc_agentic_mcp/workflow_policy.py", "src/bc_agentic_mcp/enforcement.py", "src/bc_agentic_mcp/gate.py"]},
    {"id": "layer:evidence", "title": "Evidence + memory", "kind": "layer", "summary": "Timeline, tests, verification, review artifacts, and lessons for closure.", "files": ["src/bc_agentic_mcp/timeline.py", "src/bc_agentic_mcp/verification.py", "src/bc_agentic_mcp/review.py"]},
]

ATLAS_GATES: List[Dict[str, Any]] = [
    {"id": "gate:clarifications", "title": "Clarification gate", "kind": "gate", "summary": "Ambiguity is resolved with evidence-grounded answers before planning can proceed.", "tools": ["bc_clarify", "bc_answer_clarification", "bc_auto_clarify"]},
    {"id": "gate:plan_packet", "title": "Plan packet gate", "kind": "gate", "summary": "A canonical review packet is required before requesting human approval.", "tools": ["bc_prepare_review", "bc_request_approval"]},
    {"id": "gate:human_decision", "title": "Human decision gate", "kind": "gate", "summary": "The recorded human decision governs whether implementation is authorized.", "tools": ["bc_submit_decision"]},
    {"id": "gate:implementation", "title": "Implementation gate", "kind": "gate", "summary": "Write operations are fenced by approval state and scope boundaries.", "tools": ["bc_implement", "bc_implement_write", "bc_implement_delete"]},
    {"id": "gate:quality", "title": "Quality gate", "kind": "gate", "summary": "Detectors and review must pass on the fresh diff.", "tools": ["bc_quality_check", "bc_detect", "bc_review"]},
    {"id": "gate:verification", "title": "Verification gate", "kind": "gate", "summary": "Runtime evidence and validation classes decide done/not-done.", "tools": ["bc_generate_tests", "bc_run_tests", "bc_verify", "bc_record_test", "bc_api_contract"]},
    {"id": "gate:pr", "title": "PR gate", "kind": "gate", "summary": "PR preparation, comments, and merge status are explicit lifecycle steps.", "tools": ["bc_prepare_pr", "bc_create_pr", "bc_get_review_comments", "bc_resolve_review_comment", "bc_merge_status"]},
    {"id": "gate:archive", "title": "Archive gate", "kind": "gate", "summary": "Closure requires archived evidence plus reflection/lessons.", "tools": ["bc_archive", "bc_feedback", "bc_reflect", "bc_lessons", "bc_promote_lesson"]},
]


def _atlas_tool_names() -> List[str]:
    tools = set(workflow_policy.COMMON_TOOLS)
    tools.update(workflow_policy.PLANNER_TOOLS)
    tools.update(workflow_policy.IMPLEMENTER_TOOLS)
    tools.update(workflow_policy.GATEKEEPER_TOOLS)
    tools.update(timeline.TOOL_PHASE.keys())
    tools.add("_health")
    return sorted(tools)


def _atlas_tool_family(tool: str) -> str:
    if tool.startswith("bc_intake_"):
        return "intake"
    if tool.startswith("bc_capture_feature") or tool.startswith("bc_refine_feature") or tool.startswith("bc_plan_feature"):
        return "feature"
    if tool in {"bc_prepare_pr", "bc_create_pr", "bc_get_review_comments", "bc_resolve_review_comment", "bc_merge_status"}:
        return "pr"
    if tool in {"bc_implement", "bc_implement_context", "bc_implement_write", "bc_implement_delete", "bc_generate_tests", "bc_run_tests", "bc_verify", "bc_record_test"}:
        return "implementation"
    if tool in {"bc_archive", "bc_feedback", "bc_reflect", "bc_lessons", "bc_promote_lesson"}:
        return "closure"
    if tool in {"bc_status", "bc_recall", "bc_checkpoint", "bc_timeline", "bc_env_preflight", "bc_tool_health", "bc_worktree", "_health", "bc_health"}:
        return "infrastructure"
    return "planning"


def _atlas_tool_summary(tool: str) -> str:
    phase = timeline.TOOL_PHASE.get(tool)
    if phase:
        return f"Completes phase: {phase.replace('_', ' ')}."
    if tool in workflow_policy.COMMON_TOOLS:
        return "Cross-stage supporting tool available across lifecycle boundaries."
    return f"{tool} participates in the deterministic BC MCP lifecycle."


def _atlas_tool_nodes() -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    role_order = ["planner", "implementer", "gatekeeper", "orchestrator"]
    for tool in _atlas_tool_names():
        roles = [r for r in role_order if tool in workflow_policy.ROLE_ALLOWLIST.get(r, set())]
        phase = timeline.TOOL_PHASE.get(tool, "")
        stage = workflow_policy._phase_to_stage(phase) if phase else (
            "plan" if tool in workflow_policy.PLANNER_TOOLS else
            "implement" if tool in workflow_policy.IMPLEMENTER_TOOLS else
            "verify" if tool in workflow_policy.GATEKEEPER_TOOLS else
            "plan"
        )
        nodes.append({
            "id": f"tool:{tool}",
            "title": tool,
            "kind": "tool",
            "family": _atlas_tool_family(tool),
            "roles": roles,
            "phase": phase,
            "stage": stage,
            "summary": _atlas_tool_summary(tool),
        })
    return nodes


def _atlas_phase_nodes() -> List[Dict[str, Any]]:
    seen = set()
    nodes: List[Dict[str, Any]] = []
    for sequence in (ITEM_PIPELINE, FEATURE_PIPELINE, INTAKE_PIPELINE):
        for phase in sequence:
            if phase in seen:
                continue
            seen.add(phase)
            nodes.append({
                "id": f"phase:{phase}",
                "title": phase.replace("_", " "),
                "kind": "phase",
                "stage": workflow_policy._phase_to_stage(phase),
                "summary": f"Lifecycle phase: {phase.replace('_', ' ')}.",
                "tools": [tool for tool, mapped in timeline.TOOL_PHASE.items() if mapped == phase],
            })
    # Include lane-specific phases such as bug diagnosis even when they are
    # inserted dynamically into an item's pipeline.
    for phase in timeline.TOOL_PHASE.values():
        if phase in seen:
            continue
        seen.add(phase)
        nodes.append({
            "id": f"phase:{phase}",
            "title": phase.replace("_", " "),
            "kind": "phase",
            "stage": workflow_policy._phase_to_stage(phase),
            "summary": f"Lifecycle phase: {phase.replace('_', ' ')}.",
            "tools": [tool for tool, mapped in timeline.TOOL_PHASE.items() if mapped == phase],
        })
    return nodes


def _atlas_edges() -> List[Dict[str, str]]:
    edges: List[Dict[str, str]] = []
    for tool, phase in timeline.TOOL_PHASE.items():
        edges.append({"from": f"tool:{tool}", "to": f"phase:{phase}", "kind": "completes"})
    for gate in ATLAS_GATES:
        for tool in gate["tools"]:
            edges.append({"from": gate["id"], "to": f"tool:{tool}", "kind": "guards"})
    for layer in ATLAS_LAYER_NODES:
        layer_id = layer["id"]
        if layer_id == "layer:cockpit":
            orbit = ["bc_status", "bc_advance", "bc_request_approval"]
        elif layer_id == "layer:projection":
            orbit = ["bc_timeline", "bc_feature_status", "bc_tool_health"]
        elif layer_id == "layer:bridge":
            orbit = ["bc_run_tests", "bc_env_preflight", "bc_api_contract"]
        elif layer_id == "layer:runtime":
            orbit = ["bc_init", "bc_worktree", "bc_push_items"]
        elif layer_id == "layer:policy":
            orbit = ["bc_answer_clarification", "bc_submit_decision", "bc_detect", "bc_review"]
        else:
            orbit = ["bc_verify", "bc_archive", "bc_reflect", "bc_get_knowledge_article"]
        for tool in orbit:
            edges.append({"from": layer_id, "to": f"tool:{tool}", "kind": "orbits"})
    return edges


def atlas(project_root: Path) -> Dict[str, Any]:
    """Global BC MCP atlas payload for interactive cockpit visualization."""
    over = overview(project_root)
    items = over.get("items") or []
    phase_counts = Counter(item.get("phase") or "new" for item in items)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": over.get("project_root"),
        "live": {
            "items_total": len(items),
            "pending_approvals": sum(int(item.get("pending_approvals") or 0) for item in items),
            "open_questions": sum(int(item.get("open_questions") or 0) for item in items),
            "feature_items": sum(1 for item in items if item.get("is_feature")),
            "intake_items": sum(1 for item in items if item.get("is_intake")),
            "phase_counts": dict(sorted(phase_counts.items())),
        },
        "sections": [
            {"id": "layers", "title": "Layers", "summary": "System architecture layers.", "nodes": ATLAS_LAYER_NODES},
            {"id": "gates", "title": "Gates", "summary": "Human + deterministic gates.", "nodes": ATLAS_GATES},
            {"id": "tools", "title": "Tools", "summary": "Registered tool surface.", "nodes": _atlas_tool_nodes()},
            {"id": "phases", "title": "Transitions", "summary": "Lifecycle transitions.", "nodes": _atlas_phase_nodes()},
        ],
        "edges": _atlas_edges(),
    }
