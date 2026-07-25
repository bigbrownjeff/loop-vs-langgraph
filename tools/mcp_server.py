"""The shared MCP tool layer. One tool layer, two loop implementations.

Both the hand-rolled loop and the LangGraph loop talk to THIS server over MCP.
Neither one imports `world.py` directly. That is the point of the artifact: the
tool contract is framework-portable, so swapping the orchestrator does not
touch the tools.

The contract style is copied deliberately from a client-engagement MCP server
the author built earlier: tool names, descriptions and both
JSON Schemas are loaded verbatim from a single frozen tool-defs.json, and the
low-level MCP Server does the enforcement on every call. It validates incoming
arguments against inputSchema before any handler runs, and validates the
structured result against outputSchema before it goes back on the wire. Change
the contract and the server changes with it; there is no second copy to drift.

Run:
  python tools/mcp_server.py                   # stdio (what the loops use)
  python tools/mcp_server.py --transport http  # Streamable HTTP on 127.0.0.1:8850/mcp
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import types
from mcp.server.lowlevel import Server

import world

SERVER_NAME = "loop-lab-tools"
SERVER_VERSION = world.CONTRACT_VERSION


def build_server() -> Server:
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=defn["name"],
                description=defn["description"],
                inputSchema=world.tool_input_schema(defn["name"]),
                outputSchema=world.tool_output_schema(defn["name"]),
            )
            for defn in world.TOOL_LIST
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> tuple[list[types.ContentBlock], dict[str, Any]]:
        handler = world.DISPATCH.get(name)
        if handler is None:
            world.log_call(name, arguments, "unknown-tool")
            raise ValueError(f"unknown tool: {name}")
        try:
            result = handler(arguments)
        except Exception as exc:
            world.log_call(name, arguments, f"handler-exception:{exc}")
            raise
        world.log_call(name, arguments, "ok")
        return [types.TextContent(type="text", text=world.summarize(name, result))], result

    return server


async def _run_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _run_http(server: Server, host: str, port: int) -> None:
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(app=server, json_response=False)

    async def handle_mcp(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="loop-lab shared MCP tool layer")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8850)
    args = parser.parse_args()

    server = build_server()
    if args.transport == "stdio":
        import anyio

        anyio.run(_run_stdio, server)
    else:
        _run_http(server, args.host, args.port)


if __name__ == "__main__":
    main()
