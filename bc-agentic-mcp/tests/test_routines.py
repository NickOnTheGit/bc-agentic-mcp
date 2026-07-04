"""Routine scheduler tests — pure scheduling logic + executor integration (no web server)."""
import asyncio
from datetime import datetime

import pytest

from bc_agentic_mcp.mission_control import routines


def _routine(**over):
    base = {"id": "r1", "name": "morning sweep", "action": "pr_sweep",
            "time": "07:30", "days": "weekdays", "enabled": True, "last_run": ""}
    base.update(over)
    return base


def test_validate_normalizes_and_rejects():
    ok = routines.validate_routine({"action": "pr_sweep", "time": "7:30", "days": "DAILY"})
    assert ok["time"] == "07:30" and ok["days"] == "daily" and ok["enabled"] is True
    with pytest.raises(ValueError):
        routines.validate_routine({"action": "rm_rf_everything", "time": "07:30"})
    with pytest.raises(ValueError):
        routines.validate_routine({"action": "pr_sweep", "time": "25:99"})
    with pytest.raises(ValueError):
        routines.validate_routine({"action": "pr_sweep", "time": "07:30", "days": "someday"})


def test_is_due_time_day_and_once_per_day():
    monday_0729 = datetime(2026, 7, 6, 7, 29)
    monday_0731 = datetime(2026, 7, 6, 7, 31)
    saturday_0800 = datetime(2026, 7, 4, 8, 0)  # 2026-07-04 is a Saturday
    r = _routine()
    assert routines.is_due(r, monday_0729) is False          # not yet time
    assert routines.is_due(r, monday_0731) is True           # due
    assert routines.is_due(r, saturday_0800) is False        # weekdays only
    assert routines.is_due(_routine(enabled=False), monday_0731) is False
    already = _routine(last_run=monday_0731.strftime("%Y-%m-%d 07:30"))
    assert routines.is_due(already, monday_0731) is False    # once per day


def test_scheduler_tick_runs_due_and_records(tmp_path):
    calls = []

    async def executor(action, presets):
        calls.append((action, dict(presets)))
        return {"summary": f"{action} done", "details": []}

    routines.save_routines(tmp_path, [_routine()])
    routines.save_server_presets(tmp_path, {"test_container_name": "acctest",
                                            "BC_TEST_PASSWORD": "must-not-persist"})
    sched = routines.RoutineScheduler(tmp_path, executor)
    ran = asyncio.run(sched.tick(datetime(2026, 7, 6, 7, 45)))
    assert ran == ["r1"]
    assert calls and calls[0][0] == "pr_sweep"
    # Secret-looking keys are never persisted server-side.
    assert "BC_TEST_PASSWORD" not in calls[0][1]
    assert calls[0][1]["test_container_name"] == "acctest"
    # last_run stamped -> a second tick the same day does nothing.
    assert asyncio.run(sched.tick(datetime(2026, 7, 6, 8, 0))) == []
    runs = routines.recent_runs(tmp_path)
    assert runs and runs[0]["ok"] is True and "pr_sweep done" in runs[0]["summary"]


def test_run_routine_records_failures(tmp_path):
    async def broken(action, presets):
        raise RuntimeError("bridge exploded")

    sched = routines.RoutineScheduler(tmp_path, broken)
    record = asyncio.run(sched.run_routine(_routine()))
    assert record["ok"] is False and "bridge exploded" in record["summary"]
    assert routines.recent_runs(tmp_path)[0]["ok"] is False
