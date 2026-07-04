"""workspace — resolve where governance artifacts (``.specs``) are stored.

By default artifacts are colocated inside the code repo (``<project_root>/.specs``)
for backward compatibility. When an external base is configured — via the
``BC_AGENTIC_SPECS_ROOT`` env var, which the server sets from its ``--specs-root``
flag — artifacts live *outside* the code repo, keyed per-repo so multiple
checkouts never collide and the code tree stays pristine.

Every module resolves its spec directory through :func:`specs_root` so the
storage location is decided in exactly one place.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional, Union

ENV_VAR = "BC_AGENTIC_SPECS_ROOT"

PathLike = Union[str, "os.PathLike[str]", Path]


def _repo_key(project_root: PathLike) -> str:
    """Stable, filesystem-safe, human-navigable per-repo folder name.

    Combines the repo folder name with a short hash of its absolute path so
    two checkouts of the same repo (or same-named repos) never collide. The
    path is lower-cased before hashing so callers that pass resolved vs.
    unresolved paths land on the same key.

    A git WORKTREE is the same logical repo as its main checkout (same object
    store, same specs, same approvals) — key it by the MAIN repo path, else a
    per-item worktree resolves to an empty workspace and every gate sees "no
    approved charter" for work that IS approved (observed live on PBI 240435).
    """
    resolved = Path(project_root).resolve()
    git_marker = resolved / ".git"
    if git_marker.is_file():  # worktrees have a .git FILE pointing at the main store
        try:
            content = git_marker.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                if line.startswith("gitdir:"):
                    gitdir = Path(line.split(":", 1)[1].strip())
                    # <main>/.git/worktrees/<name> -> main repo root
                    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                        resolved = gitdir.parent.parent.parent.resolve()
                    break
        except OSError:
            pass
    digest = hashlib.sha1(str(resolved).lower().encode("utf-8")).hexdigest()[:8]
    name = resolved.name or "repo"
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name)
    return f"{safe}-{digest}"


def external_base() -> Optional[Path]:
    """The configured external base directory, or ``None`` for colocated mode."""
    base = os.environ.get(ENV_VAR)
    return Path(base).expanduser() if base else None


def specs_root(project_root: PathLike) -> Path:
    """Directory holding the per-spec governance folders for ``project_root``.

    External (``<base>/<repo-key>``) when ``BC_AGENTIC_SPECS_ROOT`` is set,
    otherwise colocated (``<project_root>/.specs``).
    """
    base = external_base()
    if base is not None:
        return base / _repo_key(project_root)
    return Path(project_root) / ".specs"
