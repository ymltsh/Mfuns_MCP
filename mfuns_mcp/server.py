"""Mfuns MCP Server 入口（stdio 传输）。"""

from __future__ import annotations

import logging

from mcp.server import MCPServer

from .tools import register_tools

logger = logging.getLogger(__name__)


def build_server() -> MCPServer:
    mcp = MCPServer("mfuns")
    register_tools(mcp)
    return mcp


def main() -> None:
    # stdio 模式下日志必须走 stderr，禁止 print
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
