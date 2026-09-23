"""
mcp_client_manager.py
=======================
Thin, reusable wrapper around the `mcp` client SDK that lets the LangGraph
agent hold open sessions to MULTIPLE MCP servers at once (filesystem +
skills-db, per the Part B bonus) and call tools on any of them by name,
without every LangGraph node re-implementing stdio plumbing.

Usage:
    manager = MCPClientManager({
        "filesystem": ["python3", "filesystem_mcp_server.py"],
        "skills_db":  ["python3", "skills_db_mcp_server.py"],
    })
    async with manager:
        result = await manager.call("filesystem", "read_file", {"path": "..."})
        skills = await manager.call("skills_db", "get_skill_demand", {"skill": "python"})
"""

import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPToolError(RuntimeError):
    """Raised when an MCP tool call comes back with isError=True, i.e. a
    protocol-level failure the agent should treat as a hard stop for that
    step (as opposed to a normal {"success": False} business-logic result,
    which the agent handles itself without raising)."""


class MCPClientManager:
    """Manages one stdio-connected MCP session per configured server."""

    def __init__(self, server_commands: Dict[str, List[str]],
                 env_overrides: Optional[Dict[str, Dict[str, str]]] = None,
                 cwd: Optional[str] = None):
        """
        Args:
            server_commands: {server_name: [command, arg1, arg2, ...]}
            env_overrides:  {server_name: {ENV_VAR: value}} merged over os.environ
            cwd: working directory to launch servers from (defaults to this file's dir)
        """
        self._server_commands = server_commands
        self._env_overrides = env_overrides or {}
        self._cwd = cwd or str(Path(__file__).parent)
        self._stack: Optional[AsyncExitStack] = None
        self.sessions: Dict[str, ClientSession] = {}

    async def __aenter__(self) -> "MCPClientManager":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for name, command in self._server_commands.items():
            env = dict(os.environ)
            env.update(self._env_overrides.get(name, {}))
            params = StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=self._cwd)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
        return self

    async def __aexit__(self, *exc_info):
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)
        self.sessions.clear()

    async def list_tools(self, server: str) -> List[str]:
        result = await self.sessions[server].list_tools()
        return [t.name for t in result.tools]

    async def call(self, server: str, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call `tool` on `server` and return the parsed JSON payload.

        Raises MCPToolError for protocol-level failures (isError=True).
        Returns the tool's own {"success": bool, "data": ..., "error": ...}
        dict unchanged for logical/business-level outcomes, so callers keep
        using the same success/error contract as the raw Milestone 1 tools.
        """
        if server not in self.sessions:
            raise MCPToolError(f"No active session for server '{server}' (available: {list(self.sessions)})")

        result = await self.sessions[server].call_tool(tool, arguments)
        text = result.content[0].text if result.content else ""

        if result.isError:
            raise MCPToolError(f"[{server}.{tool}] {text}")

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Some tools (rare) might return plain text; wrap it consistently.
            return {"success": True, "data": text, "error": None}

    async def read_resource(self, server: str, uri: str) -> Any:
        result = await self.sessions[server].read_resource(uri)
        text = result.contents[0].text if result.contents else ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
