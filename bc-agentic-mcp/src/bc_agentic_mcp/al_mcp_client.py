"""Microsoft AL MCP backend adapter."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@dataclass
class ALMcpResult:
    """Result returned by the Microsoft AL MCP backend."""

    summary: Dict[str, Any]
    objects: List[Dict[str, Any]]
    dependencies: Dict[str, Any]
    diagnostics: List[Dict[str, Any]]


def _resolve_altool_command() -> List[str]:
    env_path = os.environ.get("ALTOOL_PATH")
    if env_path:
        return [env_path]
    return ["altool"]


async def analyze_project(project_root: Path, depth: str = "basic") -> ALMcpResult:
    """Ask the Microsoft AL MCP server to analyze the workspace.

    Uses Microsoft's own compiled-symbol backend when available. If the tool
    is missing or the MCP call fails, callers should fall back to local logic.
    """
    server = StdioServerParameters(
        command=_resolve_altool_command()[0],
        args=["launchmcpserver", "--transport", "stdio"],
        cwd=str(project_root),
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            package_result = await session.call_tool(
                "al_packages",
                {
                    "action": "load",
                    "projectPath": str(project_root),
                },
            )

            diagnostics_result = await session.call_tool(
                "al_getdiagnostics",
                {
                    "projectPath": str(project_root),
                },
            )

    package_text = getattr(package_result, "content", [])
    diagnostics_text = getattr(diagnostics_result, "content", [])
    summary = {
        "name": project_root.name,
        "path": str(project_root),
        "source": "microsoft-al-mcp",
        "packages_loaded": True,
        "depth": depth,
    }
    return ALMcpResult(
        summary=summary,
        objects=[],
        dependencies={"package_load": package_text},
        diagnostics=diagnostics_text,
    )