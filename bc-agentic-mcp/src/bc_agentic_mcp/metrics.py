"""metrics — E2: cycle-time and reliability metrics from the EXISTING audit log.

Single source: ``.specs/.audit/log.jsonl`` (written by every ``_run_tool`` call).
No second store, no instrumentation — just a deterministic aggregation so "how long
does an item take, where does the time go, what fails" is a tool call, not a guess.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root


def load_entries(project_root: Path) -> List[Dict[str, Any]]:
    path = specs_root(Path(project_root).resolve()) / ".audit" / "log.jsonl"
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn line must not kill the whole report
    return entries


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def summarize(entries: List[Dict[str, Any]], *, spec_name: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate totals, per-tool timings/failure rates, per-item cycle times."""
    if spec_name:
        entries = [e for e in entries if e.get("spec_name") == spec_name]

    per_tool: Dict[str, Dict[str, Any]] = {}
    per_spec: Dict[str, Dict[str, Any]] = {}
    failures = 0

    for e in entries:
        tool = str(e.get("tool", "unknown"))
        ok = bool(e.get("success"))
        duration = int(e.get("duration_ms", 0) or 0)
        if not ok:
            failures += 1

        t = per_tool.setdefault(tool, {"calls": 0, "failures": 0, "total_ms": 0, "max_ms": 0})
        t["calls"] += 1
        t["failures"] += 0 if ok else 1
        t["total_ms"] += duration
        t["max_ms"] = max(t["max_ms"], duration)

        spec = e.get("spec_name")
        ts = _parse_ts(e.get("timestamp"))
        if spec and ts:
            s = per_spec.setdefault(str(spec), {"events": 0, "failures": 0,
                                                 "first": ts, "last": ts})
            s["events"] += 1
            s["failures"] += 0 if ok else 1
            s["first"] = min(s["first"], ts)
            s["last"] = max(s["last"], ts)

    for t in per_tool.values():
        t["avg_ms"] = round(t["total_ms"] / t["calls"]) if t["calls"] else 0
        del t["total_ms"]

    spec_rows: Dict[str, Dict[str, Any]] = {}
    for name, s in per_spec.items():
        spec_rows[name] = {
            "events": s["events"],
            "failures": s["failures"],
            "first_event": s["first"].isoformat(),
            "last_event": s["last"].isoformat(),
            "cycle_seconds": round((s["last"] - s["first"]).total_seconds()),
        }

    return {
        "total_calls": len(entries),
        "total_failures": failures,
        "failure_rate_pct": round(100.0 * failures / len(entries), 1) if entries else 0.0,
        "per_tool": dict(sorted(per_tool.items())),
        "per_spec": dict(sorted(spec_rows.items())),
    }


async def handle_metrics(
    project_root: str,
    spec_name: Optional[str] = None,
) -> Dict[str, Any]:
    """bc_metrics — deterministic cycle-time/reliability report from audit.jsonl."""
    entries = load_entries(Path(project_root))
    report = summarize(entries, spec_name=spec_name)
    report["status"] = "metrics" if entries else "no_audit_data"
    return report
