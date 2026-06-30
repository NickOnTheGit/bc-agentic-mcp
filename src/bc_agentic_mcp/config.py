"""Server configuration and AL MCP Server discovery. See spec Section 5.3, 9.2."""
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ALToolStatus:
    """Status of the AL MCP Server connection."""

    altool_path: Optional[Path] = None
    available: bool = False
    mode: str = "spec-only"  # "full" or "spec-only"
    version: str = ""
    error: str = ""

    def __post_init__(self):
        if self.altool_path:
            self.available = True
            self.mode = "full"


@dataclass
class ServerConfig:
    """Runtime configuration for bc-agentic-mcp."""

    project_root: Path
    al_tool: ALToolStatus = field(default_factory=ALToolStatus)
    app_json_path: Optional[Path] = None
    per_tool_rate: int = 30
    per_session_rate: int = 120
    max_compile_attempts: int = 3
    approval_timeout_minutes: int = 60


def discover_al_tool() -> ALToolStatus:
    """Discover AL MCP Server (altool). Tries hard before giving up.

    Order: env var -> standard install paths -> PATH -> unavailable.
    See spec Section 9.2.
    """
    # 1. Environment variable
    env_path = os.environ.get("ALTOOL_PATH")
    if env_path and Path(env_path).exists():
        return ALToolStatus(altool_path=Path(env_path))

    # 2. Standard install paths
    candidates = [
        Path("C:/Program Files/Microsoft/AL Language/altool.exe"),
        Path.home() / ".vscode/extensions/ms-dynamics-smb.al-*/bin/altool",
    ]
    if sys.platform != "win32":
        candidates.append(Path("/usr/local/bin/altool"))
        candidates.append(Path.home() / ".local/bin/altool")

    for candidate in candidates:
        # Expand glob patterns
        if "*" in str(candidate):
            parent = candidate.parent
            pattern = candidate.name
            if parent.exists():
                matches = sorted(parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
                if matches:
                    return ALToolStatus(altool_path=matches[0])
        elif candidate.exists():
            return ALToolStatus(altool_path=candidate)

    # 3. PATH
    try:
        result = subprocess.run(
            ["altool", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return ALToolStatus(altool_path=Path("altool"), version=result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 4. Unavailable — spec-only mode
    return ALToolStatus(error="AL MCP Server (altool) not found. Spec-only mode active.")


def discover_app_json(start_dir: Path) -> Optional[Path]:
    """Walk up from start_dir to find app.json.

    Uses absolute() (not resolve()) so the returned path keeps the caller's
    naming and avoids Windows 8.3 short-name expansion surprises.
    """
    current = Path(start_dir).absolute()
    for _ in range(10):  # max 10 levels up
        candidate = current / "app.json"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break  # reached filesystem root
        current = parent
    return None
