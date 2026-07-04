"""tool_health — the self-improvement loop's evidence half.

Anthropic's tool-testing finding: rewriting a failing tool's description cut
task completion time 40%. This module supplies the DETERMINISTIC evidence for
that loop: it mines the audit log for per-tool reliability and emits ranked
improvement candidates. The MODEL then proposes docstring/description edits —
and prompt CI (tests/evals) gates every such change deliberately.

Read-only over .specs/.audit/log.jsonl; writes policy/tool_health.{json,md}.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from bc_agentic_mcp.workspace import specs_root

FAILURE_RATE_THRESHOLD = 0.10
MIN_CALLS = 5


def _load_audit(specs_dir: Path, limit: int = 20_000) -> List[Dict[str, Any]]:
    path = specs_dir / ".audit" / "log.jsonl"
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    entries.append(rec)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return entries


def aggregate(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    per_tool: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        tool = str(e.get("tool") or "")
        if not tool:
            continue
        row = per_tool.setdefault(tool, {
            "calls": 0, "failures": 0, "total_ms": 0, "last_failure": ""})
        row["calls"] += 1
        row["total_ms"] += int(e.get("duration_ms") or 0)
        if not e.get("success", True):
            row["failures"] += 1
            row["last_failure"] = str(e.get("timestamp") or "")
    for tool, row in per_tool.items():
        row["failure_rate"] = round(row["failures"] / row["calls"], 3) if row["calls"] else 0.0
        row["avg_ms"] = int(row["total_ms"] / row["calls"]) if row["calls"] else 0
        del row["total_ms"]
    return per_tool


def improvement_candidates(per_tool: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for tool, row in per_tool.items():
        if row["calls"] >= MIN_CALLS and row["failure_rate"] >= FAILURE_RATE_THRESHOLD:
            out.append({
                "tool": tool,
                **row,
                "hypothesis": (
                    "Repeated failures usually mean the tool's description/params let the "
                    "agent call it wrongly (wrong stage, missing input, misread contract). "
                    "Review the audit entries for this tool, then propose a docstring "
                    "clarification — prompt CI will gate the change."),
            })
    return sorted(out, key=lambda r: (r["failure_rate"], r["calls"]), reverse=True)


def handle_tool_health(project_root: str, spec_name: str = "") -> Dict[str, Any]:
    """Per-tool reliability from the audit log + ranked improvement candidates."""
    root = Path(project_root).resolve()
    specs_dir = specs_root(root)
    entries = _load_audit(specs_dir)
    if not entries:
        return {"status": "no_data", "reason": "audit log empty — nothing to analyze yet."}
    per_tool = aggregate(entries)
    candidates = improvement_candidates(per_tool)
    report = {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries_analyzed": len(entries),
        "tools": dict(sorted(per_tool.items())),
        "improvement_candidates": candidates,
    }
    policy_dir = specs_dir / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "tool_health.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Tool health report", "",
        f"_Generated {report['generated_at']} from {len(entries)} audit entries._", "",
        "| tool | calls | failures | rate | avg ms |", "|---|---|---|---|---|",
    ]
    for tool, row in sorted(per_tool.items(), key=lambda kv: kv[1]["failure_rate"], reverse=True):
        lines.append(f"| {tool} | {row['calls']} | {row['failures']} | "
                     f"{row['failure_rate']:.0%} | {row['avg_ms']} |")
    lines += ["", "## Improvement candidates (propose docstring fixes; prompt CI gates them)"]
    lines += [f"- **{c['tool']}** — {c['failure_rate']:.0%} of {c['calls']} calls fail "
              f"(last: {c['last_failure'] or 'n/a'})" for c in candidates] or ["- none 🎉"]
    (policy_dir / "tool_health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
