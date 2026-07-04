"""repo_state — ensure code search runs against the LATEST, CLEAN source.

Deterministic git status via an injectable runner (subprocess seam). Freshness + cleanliness is
a PRECONDITION for code-context search: stale or dirty source yields misleading precedents.

Safety: cleaning here is REVERSIBLE only — `git pull --ff-only` and `git stash` (restorable).
Destructive operations (`reset --hard`, `clean -fd`) are NEVER performed automatically; they would
discard in-progress work and must be a deliberate human action.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# runner(args, cwd) -> (returncode, stdout, stderr)
Runner = Callable[[Sequence[str], str], Tuple[int, str, str]]

_GIT_TIMEOUT_S = 20  # a hung git must never stall the planning cycle


def _default_runner(args: Sequence[str], cwd: str) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                           timeout=_GIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {_GIT_TIMEOUT_S}s"


def status(root: str, runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Return branch / dirty files / ahead / behind for the repo at ``root``."""
    run = runner or _default_runner
    r = str(root)

    def git(*a: str) -> Tuple[int, str, str]:
        return run(["git", *a], r)

    _, branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    # Tracked-only: skip the (expensive) untracked-file tree walk on large repos. Untracked build
    # artifacts / symbol packages must never block or slow the code-context precondition.
    _, porcelain, _ = git("status", "--porcelain", "--untracked-files=no")
    dirty_files = [ln[3:].strip() for ln in porcelain.splitlines() if ln.strip()]

    rc_u, upstream, _ = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    has_upstream = rc_u == 0 and upstream.strip() != ""
    ahead = behind = 0
    if has_upstream:
        rc_c, counts, _ = git("rev-list", "--left-right", "--count", "HEAD...@{u}")
        if rc_c == 0 and counts.strip():
            parts = counts.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    return {
        "branch": branch.strip(),
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "has_upstream": has_upstream,
        "upstream": upstream.strip() if has_upstream else None,
        "ahead": ahead,
        "behind": behind,
    }


def is_clean_latest(
    root: str, runner: Optional[Runner] = None, ignore_prefixes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Is the repo clean AND up to date? ``.specs/`` (our own artifacts) is never 'dirty'."""
    st = status(root, runner)
    ignore = tuple(ignore_prefixes or []) + (".specs/", ".specs\\")
    blocking = [f for f in st["dirty_files"] if not f.startswith(ignore)]
    reasons: List[str] = []
    if blocking:
        reasons.append(f"working tree has uncommitted changes ({len(blocking)} file(s))")
    if st["behind"]:
        reasons.append(f"branch is {st['behind']} commit(s) behind {st['upstream']}")
    return {"ok": not reasons, "reasons": reasons, "status": st, "blocking_dirty": blocking}


def make_latest(root: str, runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Reversible: fast-forward pull only (fails cleanly if a merge would be required)."""
    run = runner or _default_runner
    rc, out, err = run(["git", "pull", "--ff-only"], str(root))
    return {"pulled": rc == 0, "output": (out + err).strip()}


def stash(root: str, runner: Optional[Runner] = None,
          message: str = "bc-agentic code-context autostash") -> Dict[str, Any]:
    """Reversible cleaning: stash dirty changes (restore later with `git stash pop`)."""
    run = runner or _default_runner
    rc, out, err = run(["git", "stash", "push", "-u", "-m", message], str(root))
    return {"stashed": rc == 0, "output": (out + err).strip()}
