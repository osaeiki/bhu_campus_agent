"""外部 MCP 服务管理：启动/停止 stdio MCP Server、暴露工具给 Agent、状态监控。

外部 MCP 承载复杂功能（如浏览器自动化）；MCP 服务不可用时自动降级，
Agent 仍可使用本地工具继续工作。
"""
import asyncio
import json
import os
import re
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import config


def _safe_prefix(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


class MCPHandle:
    def __init__(self, row: dict):
        self.row = row
        self.name = row["name"]
        self.ctx = None
        self.session: Optional[ClientSession] = None
        self.tools: list[dict] = []  # [{name, description, input_schema}]
        self.status = "unknown"
        self.last_error = ""

    @property
    def prefix(self) -> str:
        return _safe_prefix(self.name)

    def tool_names(self) -> list[str]:
        return [f"{self.prefix}_{_safe_prefix(t['name'])}" for t in self.tools]


class MCPManager:
    def __init__(self):
        self._handles: dict[str, MCPHandle] = {}
        self._lock = asyncio.Lock()

    async def start_all(self, server_rows: list[dict]):
        for row in server_rows:
            if row["enabled"]:
                await self.start_one(row)

    async def start_one(self, row: dict) -> MCPHandle:
        handle = MCPHandle(row)
        async with self._lock:
            old = self._handles.get(handle.name)
            if old:
                await self._close_handle(old)
            self._handles[handle.name] = handle
        try:
            await asyncio.wait_for(self._do_start(handle), timeout=40)
            handle.status = "online"
        except Exception as e:
            handle.status = "offline"
            handle.last_error = str(e)[:500]
        self._persist_status(handle)
        return handle

    async def _do_start(self, handle: MCPHandle):
        cmd = handle.row["command"]
        raw_args = handle.row.get("args") or []
        args = json.loads(raw_args) if isinstance(raw_args, str) else list(raw_args)
        raw_env = handle.row.get("env") or {}
        env = json.loads(raw_env) if isinstance(raw_env, str) else dict(raw_env)
        if handle.row.get("transport") == "http":
            # 远程 streamable HTTP MCP：command 为 URL，env 为请求头（如 {"Authorization":"Bearer xxx"}）
            from mcp.client.streamable_http import streamablehttp_client

            ctx = streamablehttp_client(cmd, headers=(env or None))
            read, write, _get_session_id = await ctx.__aenter__()
            handle.ctx = ctx
            session = await ClientSession(read, write).__aenter__()
            handle.session = session
            await session.initialize()
            result = await session.list_tools()
            handle.tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in result.tools
            ]
            return
        if not os.path.isabs(cmd):
            cmd = os.path.normpath(os.path.join(str(config.BASE_DIR), cmd))
            # 跨平台兼容：Windows 上传的 .venv/Scripts/python.exe 在 Linux/容器里不存在时，
            # 自动改用当前解释器（如容器内的 python）
            if not os.path.exists(cmd) and cmd.lower().endswith(("python.exe", "pythonw.exe")):
                import sys as _sys
                cmd = _sys.executable
        full_env = dict(os.environ)
        full_env.update(env)
        params = StdioServerParameters(command=cmd, args=args, env=full_env)
        ctx = stdio_client(params)
        read, write = await ctx.__aenter__()
        handle.ctx = ctx
        session = await ClientSession(read, write).__aenter__()
        handle.session = session
        await session.initialize()
        result = await session.list_tools()
        handle.tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in result.tools
        ]

    def _persist_status(self, handle: MCPHandle):
        try:
            from .database import get_conn

            conn = get_conn()
            try:
                conn.execute(
                    "UPDATE mcp_servers SET status=?, last_error=? WHERE name=?",
                    (handle.status, handle.last_error or None, handle.name),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    async def _close_handle(self, handle: MCPHandle):
        try:
            if handle.session:
                await handle.session.__aexit__(None, None, None)
            if handle.ctx:
                await handle.ctx.__aexit__(None, None, None)
        except BaseException:
            pass
        handle.session = None
        handle.ctx = None
        handle.tools = []
        handle.status = "offline"

    async def stop_all(self):
        async with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for h in handles:
            await self._close_handle(h)

    def reload(self, server_rows: list[dict]):
        """同步本地 handle 与数据库配置：新增启动、删除关闭、改动重启。"""
        want = {r["name"]: r for r in server_rows if r["enabled"]}
        for name, row in want.items():
            handle = self._handles.get(name)
            if handle is None:
                asyncio.create_task(self.start_one(row))
            elif handle.status != "online":
                asyncio.create_task(self.start_one(row))
        for name in list(self._handles.keys()):
            if name not in want:
                handle = self._handles.pop(name)
                asyncio.create_task(self._close_handle(handle))

    def available_tools(self) -> list[dict]:
        """转换为 OpenAI 函数格式，供 Agent 调用。"""
        out = []
        for handle in self._handles.values():
            if handle.status != "online":
                continue
            for t in handle.tools:
                schema = dict(t["input_schema"])
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{handle.prefix}_{_safe_prefix(t['name'])}",
                            "description": f"[外部MCP:{handle.name}] {t['description']}".strip(),
                            "parameters": schema,
                        },
                    }
                )
        return out

    async def call_tool(self, prefixed_name: str, arguments: dict) -> str:
        for handle in self._handles.values():
            if handle.status != "online":
                continue
            for t in handle.tools:
                if prefixed_name == f"{handle.prefix}_{_safe_prefix(t['name'])}":
                    result = await handle.session.call_tool(t["name"], arguments)
                    texts = []
                    for content in result.content:
                        if getattr(content, "type", None) == "text":
                            texts.append(content.text)
                        else:
                            texts.append(f"[非文本内容:{getattr(content,'type','?')}]")
                    text = "\n".join(texts).strip()
                    return text[: config.TOOL_RESULT_MAX] if text else str(result)
        raise KeyError(f"MCP 工具不存在或服务离线：{prefixed_name}")

    def status(self) -> list[dict]:
        rows = []
        for handle in self._handles.values():
            rows.append(
                {
                    "name": handle.name,
                    "status": handle.status,
                    "last_error": handle.last_error,
                    "tools": [t["name"] for t in handle.tools],
                }
            )
        return rows
