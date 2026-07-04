"""bc_worktree — managed git worktrees: one isolated checkout per item.

Parallel delivery without collisions: every mission can build/test in its own
worktree (the pattern proven manually with wt-240435) while governance state
stays SHARED — the external specs workspace is keyed by the MAIN repo path, so
tools called with project_root=<worktree> still read/write the same item folder.

Actions (all deterministic, all through git itself):
  create — ``git worktree add`` with a per-item branch; records worktree.json
  status — the recorded worktree + whether it still exists on disk
  list   — every worktree of the repo (porcelain parse)
  remove — ``git worktree remove`` (+ prune); deletes the record
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp.workspace import specs_root

_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _git(root: Path, *args: str, timeout: int = 60) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, timeout=timeout, encoding="utf-8", errors="replace")


def _record_path(root: Path, spec_name: str) -> Path:
    return specs_root(root) / spec_name / "worktree.json"


def load_record(root: Path, spec_name: str) -> Optional[Dict[str, Any]]:
    path = _record_path(root, spec_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _parse_worktree_list(porcelain: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            if current:
                out.append(current)
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
    if current:
        out.append(current)
    return out


def handle_worktree(
    project_root: str,
    action: str,
    spec_name: Optional[str] = None,
    branch: Optional[str] = None,
    base_ref: Optional[str] = None,
    worktrees_base: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Manage per-item worktrees. ``project_root`` is always the MAIN repo."""
    root = Path(project_root).resolve()
    if not (root / ".git").exists():
        return {"status": "error", "reason": f"{root} is not a git repository root."}

    if action == "list":
        proc = _git(root, "worktree", "list", "--porcelain")
        if proc.returncode != 0:
            return {"status": "error", "reason": proc.stderr.strip()[:400]}
        return {"status": "ok", "worktrees": _parse_worktree_list(proc.stdout)}

    if not spec_name or not _SPEC_RE.match(spec_name):
        return {"status": "error", "reason": "a valid spec_name is required for this action."}

    if action == "status":
        record = load_record(root, spec_name)
        if not record:
            return {"status": "none", "exists": False,
                    "hint": "No worktree recorded — bc_worktree(action='create') makes one."}
        exists = Path(record.get("path", "")).is_dir()
        return {"status": "ok", "exists": exists, **record}

    if action == "create":
        existing = load_record(root, spec_name)
        if existing and Path(existing.get("path", "")).is_dir():
            return {"status": "already_exists", **existing,
                    "hint": "Reuse it — pass project_root=<path> to build/test tools."}
        base = Path(worktrees_base).resolve() if worktrees_base else root.parent / "bc-worktrees"
        base.mkdir(parents=True, exist_ok=True)
        wt_path = base / f"wt-{spec_name}"
        branch_name = branch or f"agent/{spec_name}"
        branch_probe = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}")
        if branch_probe.returncode == 0:
            proc = _git(root, "worktree", "add", str(wt_path), branch_name, timeout=300)
        else:
            args = ["worktree", "add", "-b", branch_name, str(wt_path)]
            if base_ref:
                args.append(base_ref)
            proc = _git(root, *args, timeout=300)
        if proc.returncode != 0:
            return {"status": "error", "reason": proc.stderr.strip()[:400]}
        record = {
            "spec_name": spec_name,
            "path": str(wt_path),
            "branch": branch_name,
            "main_repo": str(root),
            "created": datetime.now(timezone.utc).isoformat(),
        }
        rpath = _record_path(root, spec_name)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return {"status": "created", **record,
                "hint": ("Pass project_root=<path> to bc_run_tests/bc_advance to work in "
                         "this checkout; governance artifacts stay shared (keyed by the "
                         "main repo).")}

    if action == "remove":
        record = load_record(root, spec_name)
        if not record:
            return {"status": "none", "reason": "no worktree recorded for this item."}
        wt_path = record.get("path", "")
        if Path(wt_path).is_dir():
            dirty = _git(Path(wt_path), "status", "--porcelain")
            if dirty.stdout.strip() and not force:
                return {"status": "blocked_dirty", "path": wt_path,
                        "reason": "worktree has uncommitted changes — commit them or pass force=true.",
                        "changes": dirty.stdout.strip()[:600]}
            args = ["worktree", "remove", wt_path]
            if force:
                args.append("--force")
            proc = _git(root, *args, timeout=120)
            if proc.returncode != 0:
                return {"status": "error", "reason": proc.stderr.strip()[:400]}
        _git(root, "worktree", "prune")
        _record_path(root, spec_name).unlink(missing_ok=True)
        return {"status": "removed", "path": wt_path, "branch": record.get("branch", "")}

    return {"status": "error", "reason": f"unknown action '{action}' — use create|status|list|remove."}
