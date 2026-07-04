"""Server configuration and AL MCP Server discovery. See spec Section 5.3, 9.2."""
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


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
    tool_timeout_seconds: int = 60
    analysis_max_files: int = 1000
    analysis_max_sibling_modules: int = 12
    max_compile_attempts: int = 3
    approval_timeout_minutes: int = 60
    analyzers: List[str] = field(default_factory=list)
    agent_role: str = "orchestrator"


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
            ["altool", "--version"], capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
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


def discover_analyzers(root: Path) -> List[str]:
    """Detect which AL code analyzers are configured for the project.

    Looks at analyzer config files and the VS Code `al.codeAnalyzers` setting.
    Covers Microsoft analyzers (CodeCop/AppSourceCop/UICop/PerTenantExtensionCop)
    and community analyzers (LinterCop, ALCops).
    """
    found = set()
    config_files = {
        "AppSourceCop.json": "AppSourceCop",
        "LinterCop.json": "LinterCop",
        "CodeCop.json": "CodeCop",
        "UICop.json": "UICop",
    }
    for filename, label in config_files.items():
        if (root / filename).exists():
            found.add(label)

    settings = root / ".vscode" / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8", errors="replace"))
            for analyzer in data.get("al.codeAnalyzers", []) or []:
                token = str(analyzer).strip().strip("${}")
                if token:
                    found.add(token)
        except (OSError, json.JSONDecodeError):
            pass
    return sorted(found)


def recommended_al_tools() -> List[dict]:
    """Curated list of best-in-class AL/Business Central tooling."""
    return [
        {
            "name": "AL Language (alc) compiler",
            "why": "Authoritative compile + diagnostics for AL.",
            "url": "https://marketplace.visualstudio.com/items?itemName=ms-dynamics-smb.al",
        },
        {
            "name": "AppSourceCop / CodeCop / UICop / PerTenantExtensionCop",
            "why": "Microsoft analyzers: breaking-change, style, UI, and PTE rules.",
            "url": "https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/developer/devenv-using-code-analysis-tool",
        },
        {
            "name": "ALCops",
            "why": "Active community analyzers (successor to LinterCop) for clean AL.",
            "url": "https://alcops.dev/",
        },
        {
            "name": "AL Object ID Ninja",
            "why": "Automatic object/field ID assignment within app.json idRanges.",
            "url": "https://github.com/vjekob/al-objid",
        },
        {
            "name": "BcContainerHelper",
            "why": "Local Business Central build/test containers.",
            "url": "https://github.com/microsoft/navcontainerhelper",
        },
        {
            "name": "AL-Go for GitHub",
            "why": "CI/CD pipelines for AL apps.",
            "url": "https://github.com/microsoft/AL-Go",
        },
        {
            "name": "AL Test Runner",
            "why": "Run and debug AL tests from VS Code.",
            "url": "https://marketplace.visualstudio.com/items?itemName=jamespearson.al-test-runner",
        },
    ]
