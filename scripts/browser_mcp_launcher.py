"""外部 MCP（Playwright 浏览器自动化）启动器。

兼容层：mcp-playwright 0.1.1 使用 `mcp.lifespan = xxx` 赋值，
但新版 FastMCP 在构造时就读取 settings.lifespan 生成底层 server，
事后赋值不生效，导致服务端初始化（浏览器管理器）不执行。
此启动器通过桥接替换底层 server 的 lifespan 来修复。
"""
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_playwright.server import mcp, server_lifespan  # noqa: E402


@asynccontextmanager
async def _lifespan_bridge(_server):
    async with server_lifespan(mcp) as ctx:
        yield ctx


# 替换底层 MCPServer 的 lifespan（FastMCP 构造时已固化，需直接改底层）
mcp._mcp_server.lifespan = _lifespan_bridge
mcp.run()
