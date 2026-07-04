"""capabilities — least-privilege guard over every subprocess this process spawns.

One choke point instead of twenty call-site wrappers: a ``sys.addaudithook``
listener on the ``subprocess.Popen`` audit event. Every exec is checked against
an explicit executable allowlist and written to an append-only audit trail;
anything not on the list is refused with a PermissionError BEFORE it starts.

This is capability scoping, not a sandbox: it constrains WHICH programs the
server may launch (git, docker, pwsh, the AL compiler...), it does not jail
their file access. OS-level isolation remains future work — documented, not
pretended.

Escape hatch for emergencies: set ``BC_AGENTIC_EXEC_GUARD=off`` (audited too).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

# Programs the SERVER legitimately launches (verified against every call site):
#   git (gate/repo_state/views), docker (env_preflight), pwsh/powershell (al_runner,
#   BcContainerHelper), dotnet + alc/altool (AL compiler), python (stdio bridge),
#   robocopy (al_runner sync step mirrors the app folder to the container share).
DEFAULT_ALLOWLIST = frozenset({
    "git", "docker", "pwsh", "powershell", "dotnet", "alc", "altool", "py", "node",
    "robocopy",
})

_lock = threading.Lock()
_state: dict = {"installed": False, "allow": set(DEFAULT_ALLOWLIST), "audit_path": None}


def _basename(executable: Any) -> str:
    name = Path(str(executable or "")).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _first_token(argv: Any) -> str:
    """Executable token from the audit event's args (Windows passes ONE joined
    string via list2cmdline; POSIX passes a list)."""
    if isinstance(argv, str):
        s = argv.strip()
        if s.startswith('"'):
            end = s.find('"', 1)
            return s[1:end] if end > 0 else s[1:]
        return s.split(" ", 1)[0]
    if isinstance(argv, (list, tuple)) and argv:
        return str(argv[0])
    return ""


def check_exec(executable: Any, argv: Any = None) -> Tuple[bool, str]:
    """Pure decision: is this exec allowed? Returns (allowed, resolved-name)."""
    name = _basename(executable)
    if not name:
        name = _basename(_first_token(argv))
    if name.startswith("python"):  # python / python3 / python3.13 — the stdio bridge
        return True, name
    return name in _state["allow"], name


def _audit(record: dict) -> None:
    path = _state.get("audit_path")
    if not path:
        return
    try:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        with _lock:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
    except OSError:
        pass  # the guard must never crash the host over a log line


def _hook(event: str, args: tuple) -> None:
    if event != "subprocess.Popen":
        return
    executable, argv = args[0], args[1]
    if os.environ.get("BC_AGENTIC_EXEC_GUARD", "").lower() == "off":
        _audit({"event": "exec", "name": _basename(executable) or _basename(_first_token(argv)),
                "allowed": True, "guard": "disabled-by-env"})
        return
    allowed, name = check_exec(executable, argv)
    _audit({"event": "exec", "name": name, "allowed": allowed,
            "argv0": (_first_token(argv) or str(executable))[:200]})
    if not allowed:
        raise PermissionError(
            f"capability guard: subprocess exec of '{name or executable}' is not in the "
            f"allowlist ({', '.join(sorted(_state['allow']))}). If this program is "
            "legitimately needed, add it via capabilities.install(extra={...})."
        )


def install(audit_dir: Optional[Path] = None, extra: Iterable[str] = ()) -> None:
    """Install the process-wide exec guard (idempotent; hooks cannot be removed).

    ``extra`` extends the allowlist (e.g. the Mission Control dispatcher adds
    the agent CLIs: opencode / copilot / gh).
    """
    _state["allow"] |= {str(e).lower() for e in extra}
    if audit_dir is not None:
        _state["audit_path"] = str(Path(audit_dir) / "subprocess.jsonl")
    if not _state["installed"]:
        sys.addaudithook(_hook)
        _state["installed"] = True
    _audit({"event": "guard-installed", "allow": sorted(_state["allow"])})
