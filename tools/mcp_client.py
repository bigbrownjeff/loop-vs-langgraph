"""Thin shared MCP client. Both loop implementations use this and only this.

Neither loop is allowed to import `world` directly, so "swap the orchestrator,
keep the tools" is enforced by construction rather than by good intentions.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = REPO_ROOT / "tools" / "mcp_server.py"

DEFAULT_TOOL_TIMEOUT_S = float(os.environ.get("LOOPLAB_TOOL_TIMEOUT_S", "10"))


def first_leaf(exc: BaseException) -> BaseException:
    """Unwrap anyio's ExceptionGroup down to the exception you actually raised.

    Measured the hard way. The MCP stdio client runs its transport inside an
    anyio task group, so an exception raised inside `async with open_tools()`
    does NOT come out as itself: it comes out as an ExceptionGroup wrapping it.
    Both implementations originally caught `except (ToolError, ToolTimeout)`,
    both silently failed to catch anything, and both died with a raw traceback
    and the wrong exit code on all three injected faults. This is a property of
    the MCP client, not of either orchestrator, and it hit both equally.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class ToolError(RuntimeError):
    """The MCP layer rejected or failed the call.

    Covers all three failure modes the comparison exercises: an input that
    violates inputSchema, a structured result that violates outputSchema, and
    an exception raised inside the handler. The loop sees one exception type
    and cannot tell them apart without reading the message, which is itself a
    finding (see RESULTS.md).
    """


class ToolTimeout(RuntimeError):
    """The tool did not answer inside the per-call deadline."""


class Tools:
    """An open MCP session, addressed by tool name."""

    def __init__(self, session: ClientSession, timeout_s: float) -> None:
        self._session = session
        self._timeout_s = timeout_s
        self.names: list[str] = []

    async def _load(self) -> None:
        listed = await self._session.list_tools()
        self.names = [t.name for t in listed.tools]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            res: types.CallToolResult = await self._session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=self._timeout_s),
            )
        except Exception as exc:  # McpError on timeout, transport errors otherwise
            if "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower():
                raise ToolTimeout(f"{name}: {exc}") from exc
            raise ToolError(f"{name}: {exc}") from exc

        if res.isError:
            text = " ".join(
                block.text for block in res.content if isinstance(block, types.TextContent)
            )
            raise ToolError(f"{name}: {text}")
        if res.structuredContent is None:
            raise ToolError(f"{name}: no structured content returned")
        return dict(res.structuredContent)


@asynccontextmanager
async def open_tools(timeout_s: float | None = None):
    """Spawn the MCP server over stdio and yield a ready Tools handle."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH), "--transport", "stdio"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = Tools(session, timeout_s or DEFAULT_TOOL_TIMEOUT_S)
            await tools._load()
            yield tools
