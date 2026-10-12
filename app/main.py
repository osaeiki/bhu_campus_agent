"""应用入口：初始化数据库/知识库/外部MCP，挂载静态页面。"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, knowledge
from .api import router as api_router
from .database import (
    get_conn,
    init_db,
    seed_if_empty,
    seed_knowledge_if_empty,
    seed_mcp_config,
)
from .mcp_manager import MCPManager

mcp_manager = MCPManager()


async def _start_mcps_in_background(rows: list[dict]) -> None:
    """在后台拉起外部 MCP。

    为什么要放后台：高德是远程服务、浏览器 MCP 要拉起 Chromium，两者加起来
    可能十几秒；如果放在 lifespan 里同步等待，这段时间整个网站都是打不开的
    （uvicorn 要等 startup 完成才开始接收请求）。放到后台之后，网页秒开，
    MCP 就绪后状态自动变成 online —— 工具列表本来就是每次请求时动态取的，
    所以晚几秒上线完全不影响使用。
    """
    try:
        await mcp_manager.start_all(rows)
        for s in mcp_manager.status():
            print(f"[init] 外部MCP [{s['name']}] -> {s['status']}，工具：{s['tools']}")
    except Exception as e:  # 后台任务不能把异常抛给事件循环
        print(f"[init] 外部MCP 启动异常：{e}")


def _ensure_amap_mcp():
    """若 .env 配置了高德 Key，注册高德官方远程 MCP（streamable HTTP）。
    Key 只存在 .env / 本地数据库，不写入种子文件。"""
    if not config.AMAP_API_KEY:
        return
    conn = get_conn()
    try:
        if conn.execute("SELECT id FROM mcp_servers WHERE name='amap'").fetchone():
            return
        conn.execute(
            "INSERT INTO mcp_servers(name,command,args,env,transport,enabled,status) VALUES(?,?,?,?,?,?,?)",
            (
                "amap",
                f"https://mcp.amap.com/mcp?key={config.AMAP_API_KEY}",
                "[]",
                "{}",
                "http",
                1,
                "unknown",
            ),
        )
        conn.commit()
        print("[init] 已注册高德地图 MCP")
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    seed_if_empty()
    seed_knowledge_if_empty()
    seed_mcp_config()
    _ensure_amap_mcp()
    # 扫描知识文件目录（文档 / PDF / 表格），按内容哈希增量导入后再建索引
    try:
        stats = knowledge.sync_all()
        if stats["chunks"] or stats["removed"]:
            print(
                f"[init] 知识文件导入：新增 {stats['added']} / 更新 {stats['updated']} / "
                f"删除 {stats['removed']} 个文件，入库知识块 {stats['chunks']}"
            )
    except Exception as e:
        print(f"[init] 知识文件导入失败：{e}")
    n = knowledge.rebuild_index()
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM mcp_servers").fetchall()]
    finally:
        conn.close()
    print(f"[init] 知识库索引分块数：{n}")
    info = knowledge.index_info()
    print(
        f"[init] 检索索引：BM25 + 向量（后端 {info.get('backend')}，维度 {info.get('dim')}，"
        f"向量数 {info.get('vectors')}，文档 {info.get('docs')} 篇）"
    )
    mcp_task = asyncio.create_task(_start_mcps_in_background(rows)) if rows else None
    yield
    if mcp_task and not mcp_task.done():
        # 关服时给后台启动任务一点时间收尾，超时就取消，避免退出卡住
        try:
            await asyncio.wait_for(asyncio.shield(mcp_task), timeout=5)
        except Exception:
            mcp_task.cancel()
    await mcp_manager.stop_all()


app = FastAPI(title="北华校园助手", description="北华大学北校区本地校园 Agent", lifespan=lifespan)
app.include_router(api_router)

STATIC_DIR = config.BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/admin")
async def admin():
    return FileResponse(str(STATIC_DIR / "admin.html"))
