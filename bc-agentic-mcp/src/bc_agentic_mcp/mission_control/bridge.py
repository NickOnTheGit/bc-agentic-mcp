"""bridge — one persistent MCP stdio client session to the bc-agentic server.

The Mission Control web app is a *client* of the real MCP server: every action
travels the identical tool pipeline an agent uses (policy checks, doom-loop
guard, timeline, audit). Nothing here bypasses a gate.

Concurrency contract: anyio cancel scopes inside ``stdio_client`` /
``ClientSession`` must be entered and exited by the SAME task. A dedicated
runner task therefore owns the whole context lifetime; requests only borrow
the session object. Calls are serialized with a lock (single duplex pipe).
If the server process dies, the next call restarts the runner once.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpBridge:
    """Owns the subprocess + session; exposes ``call(tool, args) -> dict``."""

    def __init__(self, project_root: str, specs_root: Optional[str] = None):
        self.project_root = project_root
        self.specs_root = specs_root
        self._lock = asyncio.Lock()
        self._runner: Optional[asyncio.Task] = None
        self._session: Optional[ClientSession] = None
        self._ready: Optional[asyncio.Event] = None
        self._shutdown: Optional[asyncio.Event] = None
        self._boot_error: Optional[BaseException] = None

    def _params(self) -> StdioServerParameters:
        args = ["-m", "bc_agentic_mcp.server", "--project-root", self.project_root]
        if self.specs_root:
            args += ["--specs-root", self.specs_root]
        return StdioServerParameters(command=sys.executable, args=args, env={**os.environ})

    async def _run(self) -> None:
        """Dedicated owner task: enters AND exits every context manager itself."""
        try:
            async with stdio_client(self._params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()  # type: ignore[union-attr]
                    await self._shutdown.wait()  # type: ignore[union-attr]
        except BaseException as exc:  # noqa: BLE001 — surface boot failures to start()
            self._boot_error = exc
            if self._ready is not None:
                self._ready.set()
        finally:
            self._session = None

    async def start(self) -> None:
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._boot_error = None
        self._runner = asyncio.create_task(self._run(), name="mcp-bridge-runner")
        await self._ready.wait()
        if self._session is None:
            error = self._boot_error
            self._runner = None
            raise RuntimeError(f"MCP server failed to start: {error!r}")

    async def stop(self) -> None:
        if self._runner is not None:
            if self._shutdown is not None:
                self._shutdown.set()
            try:
                await asyncio.wait_for(self._runner, timeout=10)
            except (asyncio.TimeoutError, Exception):
                self._runner.cancel()
            self._runner = None
            self._session = None

    async def _restart(self) -> None:
        await self.stop()
        await self.start()

    async def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool; return its decoded JSON payload (or raw text fallback)."""
        async with self._lock:
            if self._session is None:
                if self._runner is not None:
                    await self._restart()
                else:
                    await self.start()
            try:
                result = await self._session.call_tool(tool, args)  # type: ignore[union-attr]
            except Exception:
                await self._restart()
                result = await self._session.call_tool(tool, args)  # type: ignore[union-attr]

        text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
        payload: Dict[str, Any]
        try:
            decoded = json.loads(text) if text else {}
            payload = decoded if isinstance(decoded, dict) else {"result": decoded}
        except (json.JSONDecodeError, ValueError):
            payload = {"raw": text}
        if getattr(result, "isError", False):
            payload.setdefault("isError", True)
        return payload
