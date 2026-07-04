"""bc_guard_pr_thread_resolution - enforce local-vs-remote sequencing for PR thread closure.

This tool is intentionally read-only: it inspects git state and blocks resolving PR review
threads as "Fixed" unless the local branch contains unpushed commits that cover the touched
file set for the thread.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from bc_agentic_mcp import gate


def _run_git(repo_root: Path, args: List[str]) -> Optional[subprocess.CompletedProcess[str]]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _normalize_repo_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("./")


def _upstream_branch(repo_root: Path) -> Optional[str]:
    proc = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not proc or proc.returncode != 0:
        return None
    upstream = (proc.stdout or "").strip()
    return upstream or None


def _unpushed_commit_count(repo_root: Path, upstream: str) -> Optional[int]:
    proc = _run_git(repo_root, ["rev-list", "--count", f"{upstream}..HEAD"])
    if not proc or proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def _unpushed_files(repo_root: Path, upstream: str) -> Optional[List[str]]:
    proc = _run_git(repo_root, ["diff", "--name-only", f"{upstream}..HEAD"])
    if not proc or proc.returncode != 0:
        return None
    files = []
    seen = set()
    for raw in (proc.stdout or "").splitlines():
        path = _normalize_repo_path(raw)
        if path and path not in seen:
            files.append(path)
            seen.add(path)
    return files


async def handle_guard_pr_thread_resolution(
    project_root: str,
    touched_files: List[str],
    require_branch_hygiene: bool = True,
    require_tracking_upstream: bool = True,
) -> Dict[str, Any]:
    """Block PR-thread resolution unless matching unpushed commits are present.

    Returns `allowed=False` with a machine-readable reason when any prerequisite is missing.
    """
    root = Path(project_root).resolve()
    touched = []
    seen = set()
    for raw in touched_files or []:
        path = _normalize_repo_path(raw)
        if path and path not in seen:
            touched.append(path)
            seen.add(path)

    if not touched:
        return {
            "allowed": False,
            "reason": "No touched_files provided; cannot validate PR-thread resolution readiness.",
            "blocked": [],
        }

    branch = gate._current_branch(root)
    if not branch:
        return {
            "allowed": False,
            "reason": "Unable to resolve current git branch; cannot verify local-vs-remote state.",
            "blocked": touched,
            "touched_files": touched,
        }

    if require_branch_hygiene and branch.lower() in gate.PROTECTED_BRANCHES:
        return {
            "allowed": False,
            "reason": (
                f"Branch hygiene violation: current branch '{branch}' is protected. "
                "Use a dedicated PR branch before resolving review threads."
            ),
            "branch": branch,
            "blocked": touched,
            "touched_files": touched,
        }

    upstream = _upstream_branch(root)
    if not upstream and require_tracking_upstream:
        return {
            "allowed": False,
            "reason": (
                "No tracking upstream branch configured; cannot prove commits are unpushed. "
                "Set upstream (git push -u ...) before marking PR threads as fixed."
            ),
            "branch": branch,
            "blocked": touched,
            "touched_files": touched,
        }

    if not upstream:
        return {
            "allowed": False,
            "reason": "No tracking upstream branch configured.",
            "branch": branch,
            "blocked": touched,
            "touched_files": touched,
        }

    commit_count = _unpushed_commit_count(root, upstream)
    files = _unpushed_files(root, upstream)
    if commit_count is None or files is None:
        return {
            "allowed": False,
            "reason": "Unable to inspect unpushed commits/files from git.",
            "branch": branch,
            "upstream": upstream,
            "blocked": touched,
            "touched_files": touched,
        }

    if commit_count <= 0:
        return {
            "allowed": False,
            "reason": "No unpushed commits found; push-ready evidence is missing for PR thread resolution.",
            "branch": branch,
            "upstream": upstream,
            "unpushed_commits": commit_count,
            "unpushed_files": files,
            "blocked": touched,
            "touched_files": touched,
        }

    unpushed_lut = {p.lower(): p for p in files}
    covered = [p for p in touched if p.lower() in unpushed_lut]
    missing = [p for p in touched if p.lower() not in unpushed_lut]
    if missing:
        return {
            "allowed": False,
            "reason": (
                "Unpushed commits do not cover all touched files for this PR thread. "
                "Do not mark as fixed yet."
            ),
            "branch": branch,
            "upstream": upstream,
            "unpushed_commits": commit_count,
            "unpushed_files": files,
            "covered_files": covered,
            "missing_files": missing,
            "blocked": missing,
            "touched_files": touched,
        }

    return {
        "allowed": True,
        "reason": "Ready to resolve thread: unpushed commits exist and cover all touched files.",
        "branch": branch,
        "upstream": upstream,
        "unpushed_commits": commit_count,
        "unpushed_files": files,
        "covered_files": covered,
        "missing_files": [],
        "blocked": [],
        "touched_files": touched,
    }
