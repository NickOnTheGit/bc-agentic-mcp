"""routines — user-defined scheduled tasks for the cockpit (manual, not magic).

The HUMAN defines every routine: which action, at what time, on which days.
Nothing runs that wasn't explicitly configured. The scheduler ticks inside the
Mission Control process; each due routine executes a DETERMINISTIC action from
a fixed menu (no freeform commands) through the same MCP bridge every button
uses — so policy, audit and gates apply unchanged.

Storage (governance workspace, survives restarts):
  .routines/routines.json  — the configured routines
  .routines/presets.json   — server-side copy of the ⚙ presets (no secrets)
  .routines/runs.jsonl     — append-only run log (digest per execution)
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

ACTIONS = {
    "pr_sweep": "Check review comments + merge status for every mission in a PR phase",
    "autopilot_sweep": "Run bc_advance once for every active (non-intake) mission",
    "env_check": "Container/license preflight with the saved presets",
    "tool_health": "Refresh the tool reliability report from the audit log",
    "consistency_sweep": "Cross-artifact consistency check for every plan-stage mission",
    "grill_sweep": "Issue a deterministic self-challenge (simpler design? knowledge conflicts?) to every active mission",
}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAY_SETS = {
    "daily": {0, 1, 2, 3, 4, 5, 6},
    "weekdays": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
}


def routines_dir(specs_base: Path) -> Path:
    return Path(specs_base) / ".routines"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def load_routines(specs_base: Path) -> List[Dict[str, Any]]:
    data = _read_json(routines_dir(specs_base) / "routines.json", [])
    return data if isinstance(data, list) else []


def save_routines(specs_base: Path, routines: List[Dict[str, Any]]) -> None:
    d = routines_dir(specs_base)
    d.mkdir(parents=True, exist_ok=True)
    (d / "routines.json").write_text(json.dumps(routines, indent=2), encoding="utf-8")


def validate_routine(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one user-defined routine; raises ValueError with a human reason."""
    action = str(raw.get("action") or "")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(sorted(ACTIONS))}")
    time_str = str(raw.get("time") or "").strip()
    if not _TIME_RE.match(time_str):
        raise ValueError("time must be HH:MM (24h), e.g. 07:30")
    days = str(raw.get("days") or "daily").lower()
    if days not in _DAY_SETS:
        raise ValueError(f"days must be one of: {', '.join(sorted(_DAY_SETS))}")
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "name": str(raw.get("name") or ACTIONS[action])[:80],
        "action": action,
        "time": f"{int(time_str.split(':')[0]):02d}:{time_str.split(':')[1]}",
        "days": days,
        "enabled": bool(raw.get("enabled", True)),
        "last_run": str(raw.get("last_run") or ""),
    }


def is_due(routine: Dict[str, Any], now: datetime) -> bool:
    """Due when: enabled, today matches, wall-clock time reached, not yet run today."""
    if not routine.get("enabled", True):
        return False
    if now.weekday() not in _DAY_SETS.get(str(routine.get("days", "daily")), set()):
        return False
    try:
        hh, mm = str(routine["time"]).split(":")
        due_minutes = int(hh) * 60 + int(mm)
    except (KeyError, ValueError):
        return False
    if now.hour * 60 + now.minute < due_minutes:
        return False
    last = str(routine.get("last_run") or "")
    return not last.startswith(now.strftime("%Y-%m-%d"))


def append_run(specs_base: Path, record: Dict[str, Any]) -> None:
    d = routines_dir(specs_base)
    d.mkdir(parents=True, exist_ok=True)
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with open(d / "runs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")


def recent_runs(specs_base: Path, limit: int = 20) -> List[Dict[str, Any]]:
    path = routines_dir(specs_base) / "runs.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


def load_server_presets(specs_base: Path) -> Dict[str, Any]:
    data = _read_json(routines_dir(specs_base) / "presets.json", {})
    return data if isinstance(data, dict) else {}


def save_server_presets(specs_base: Path, presets: Dict[str, Any]) -> None:
    clean = {k: str(v) for k, v in (presets or {}).items()
             if isinstance(v, (str, int, float)) and str(v).strip()
             and "password" not in k.lower() and "secret" not in k.lower()
             and "token" not in k.lower() and not str(k).startswith("_")}
    d = routines_dir(specs_base)
    d.mkdir(parents=True, exist_ok=True)
    (d / "presets.json").write_text(json.dumps(clean, indent=2), encoding="utf-8")


class RoutineScheduler:
    """Ticks once a minute inside the cockpit; executes due routines sequentially."""

    def __init__(
        self,
        specs_base: Path,
        executor: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
        tick_seconds: int = 30,
    ):
        self.specs_base = Path(specs_base)
        self.executor = executor  # async (action, presets) -> digest dict
        self.tick_seconds = tick_seconds
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="mc-routine-scheduler")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick(datetime.now())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # scheduler must never die silently
                append_run(self.specs_base, {"routine": "(scheduler)", "action": "tick",
                                             "ok": False, "summary": f"tick failed: {exc}"})
            await asyncio.sleep(self.tick_seconds)

    async def tick(self, now: datetime) -> List[str]:
        routines = load_routines(self.specs_base)
        ran: List[str] = []
        changed = False
        for routine in routines:
            if not is_due(routine, now):
                continue
            routine["last_run"] = now.strftime("%Y-%m-%d %H:%M")
            changed = True
            await self.run_routine(routine)
            ran.append(routine["id"])
        if changed:
            save_routines(self.specs_base, routines)
        return ran

    async def run_routine(self, routine: Dict[str, Any]) -> Dict[str, Any]:
        presets = load_server_presets(self.specs_base)
        try:
            digest = await self.executor(routine["action"], presets)
            ok = not digest.get("error")
        except Exception as exc:
            digest, ok = {"error": str(exc)[:300]}, False
        record = {"routine": routine.get("name", ""), "id": routine.get("id", ""),
                  "action": routine.get("action", ""), "ok": ok,
                  "summary": str(digest.get("summary") or digest.get("error") or "")[:400],
                  "details": digest.get("details")}
        append_run(self.specs_base, record)
        return record
