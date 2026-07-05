"""context_recovery — survive agent context loss by making the DISK the memory.

Problem (2026-07-05): a long lifecycle produces tool responses far bigger than a
weak agent's usable context. After compaction/truncation the agent has lost the
review packet / code context / analysis it was just reasoning about, and its only
options are re-running expensive tools (container runs, rate-limited ADO REST) or
fabricating from half-memory.

Contract:
- Every spec-scoped tool result whose serialized size exceeds the threshold is
  persisted VERBATIM (as the agent saw it) to ``.specs/<spec>/artifacts/`` and the
  response gains a ``recovery`` pointer {artifact, bytes}.
- The persistence is announced in the item timeline (checkpoint kind="artifact"),
  so checkpoints.jsonl remains the ONE place that reconstructs the story — including
  where the big payloads live.
- ``disk_map()`` gives bc_status a rehydration map: the key lifecycle files plus the
  latest artifacts/logs, with sizes — "re-read these instead of re-running tools".
- Fail-open everywhere: recovery must never break the tool call it protects.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root

# Serialized-size threshold (chars) above which a result is persisted. ~16KB ≈ 4K
# tokens: anything bigger is at real risk of being compacted out of a weak agent.
_DEFAULT_THRESHOLD = 16_000
# Retention per spec: newest N artifact files are kept, older pruned.
_DEFAULT_KEEP = 40

# Read-only projections are never persisted: they are cheap to recompute and
# persisting them would make the recovery surface reference itself.
EXCLUDED_TOOLS = {
    "bc_status", "bc_timeline", "bc_recall", "bc_checkpoint", "bc_lessons",
    "bc_tool_health", "_health", "bc_health",
}


def _threshold() -> int:
    try:
        return int(os.environ.get("BC_MCP_RECOVERY_THRESHOLD", _DEFAULT_THRESHOLD))
    except ValueError:
        return _DEFAULT_THRESHOLD


def _keep() -> int:
    try:
        return int(os.environ.get("BC_MCP_RECOVERY_KEEP", _DEFAULT_KEEP))
    except ValueError:
        return _DEFAULT_KEEP


def artifacts_dir(root: Path, spec_name: str) -> Path:
    return specs_root(Path(root).resolve()) / spec_name / "artifacts"


def persist_result(
    result: Any,
    project_root: Optional[str],
    spec_name: Optional[str],
    tool: str,
) -> Any:
    """Persist an oversized result to disk and attach a ``recovery`` pointer.

    Returns the (possibly annotated) result. Never raises; never truncates the
    in-band response — weak agents keep working, strong ones stop re-running tools.
    """
    if (
        not isinstance(result, dict)
        or not project_root
        or not spec_name
        or tool in EXCLUDED_TOOLS
    ):
        return result
    try:
        payload = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return result
    if len(payload) <= _threshold():
        return result
    try:
        directory = artifacts_dir(Path(project_root), spec_name)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"{stamp}-{tool}.json"
        n = 1
        while path.exists():
            n += 1
            path = directory / f"{stamp}-{tool}-{n}.json"
        path.write_text(payload, encoding="utf-8")
        _prune(directory)
        result["recovery"] = {
            "artifact": str(path),
            "bytes": len(payload),
            "hint": "full result persisted — after context loss re-read this file instead of re-running the tool",
        }
        _announce(Path(project_root), spec_name, tool, path, len(payload))
    except OSError:
        return result
    return result


def _prune(directory: Path) -> None:
    """Keep the newest N artifacts (stamp-prefixed names sort chronologically)."""
    try:
        files = sorted(f for f in directory.glob("*.json") if f.is_file())
        for old in files[: max(0, len(files) - _keep())]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _announce(root: Path, spec_name: str, tool: str, path: Path, size: int) -> None:
    """Record the artifact in the item timeline — checkpoints.jsonl stays the single
    story, including where recoverable payloads live."""
    try:
        from bc_agentic_mcp import checkpoints as memory
        memory.append_checkpoint(
            Path(root).resolve(), spec_name,
            kind="artifact",
            summary=f"{tool} result ({size:,} chars) persisted for recovery",
            details={"tool": tool, "path": str(path), "bytes": size},
        )
    except Exception:
        pass


# Key lifecycle files a recovering agent should know about, in read-priority order.
_KEY_FILES = [
    "spec.json",
    "TDD.md",
    "DESIGN.md",
    "tasks.json",
    "context/manifest.json",
    "context/precedents.json",
    "review/CHARTER.md",
    "review/REVIEW.md",
    "TEST-REPORT.md",
    "pr/PR.md",
    "pr/prepared.json",
    "checkpoints.jsonl",
    "TIMELINE.md",
]


def disk_map(project_root: Path, spec_name: str) -> Dict[str, Any]:
    """The rehydration map for bc_status: what exists on disk for this item.

    Lists the key lifecycle files that exist plus the newest artifacts and logs —
    each with size and mtime — so an agent recovering from context loss reads its
    way back instead of re-running the lifecycle.
    """
    base = specs_root(Path(project_root).resolve()) / spec_name

    def _entry(p: Path) -> Dict[str, Any]:
        st = p.stat()
        return {
            "path": str(p),
            "bytes": st.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
        }

    files: List[Dict[str, Any]] = []
    for rel in _KEY_FILES:
        p = base / rel
        if p.is_file():
            try:
                files.append(_entry(p))
            except OSError:
                continue
    artifacts: List[Dict[str, Any]] = []
    adir = base / "artifacts"
    if adir.is_dir():
        newest = sorted((f for f in adir.glob("*.json") if f.is_file()), reverse=True)[:8]
        for f in newest:
            try:
                artifacts.append(_entry(f))
            except OSError:
                continue
    logs: List[Dict[str, Any]] = []
    ldir = base / "logs"
    if ldir.is_dir():
        newest = sorted((f for f in ldir.glob("*.log") if f.is_file()), reverse=True)[:5]
        for f in newest:
            try:
                logs.append(_entry(f))
            except OSError:
                continue
    return {
        "key_files": files,
        "artifacts": artifacts,
        "logs": logs,
        "hint": ("after context loss: read key_files top-down, then the newest artifacts — "
                 "never re-run container/ADO tools just to re-see their output"),
    }
