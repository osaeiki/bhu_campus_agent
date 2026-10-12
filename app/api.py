"""FastAPI 路由：认证、对话（SSE）、反馈、管理后台。"""
import json
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import config, ingest, knowledge, notify
from .agent import run_agent
from .auth import create_token, get_current_user, login as auth_login, require_roles
from .database import get_conn, get_user_by_id
from .security import hash_password

router = APIRouter(prefix="/api")


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def api_login(req: LoginRequest):
    result = auth_login(req.username.strip(), req.password)
    if not result:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return result


@router.get("/me")
async def api_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "name": user["name"],
        "student_no": user["student_no"],
        "department": user["department"],
        "class_name": user["class_name"],
    }


@router.get("/system/status")
async def api_system_status(_: dict = Depends(get_current_user)):
    from .main import mcp_manager

    return {
        "mcp": [{"name": s["name"], "status": s["status"], "tools": s["tools"]} for s in mcp_manager.status()],
        "server_time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------- 对话 ----------
class ChatRequest(BaseModel):
    session_id: str = Field(default="", description="会话ID，前端生成；留空则新建")
    message: str


@router.get("/history/{session_id}")
async def api_history(session_id: str, user: dict = Depends(get_current_user)):
    """返回当前用户自己的会话历史；会话不属于该用户时返回空。"""
    conn = get_conn()
    try:
        sess = conn.execute("SELECT user_id FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
        if not sess or sess["user_id"] != user["id"]:
            return {"items": [], "session_id": session_id}
        rows = conn.execute(
            "SELECT role,content,tool_calls FROM chat_messages WHERE session_id=? "
            "AND role IN ('user','assistant') ORDER BY id",
            (session_id,),
        ).fetchall()
        return {"items": [dict(r) for r in rows], "session_id": session_id}
    finally:
        conn.close()


@router.post("/chat")
async def api_chat(req: ChatRequest, request: Request, user: dict = Depends(get_current_user)):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = req.session_id.strip() or str(uuid.uuid4())
    if req.session_id.strip():
        # 会话归属校验：他人会话不允许续用，直接开新会话
        conn = get_conn()
        try:
            sess = conn.execute("SELECT user_id FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        if sess and sess["user_id"] != user["id"]:
            session_id = str(uuid.uuid4())

    async def gen():
        import asyncio

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict):
            await queue.put(ev)

        task = asyncio.create_task(
            run_agent(
                user,
                session_id,
                message,
                emit,
                mcp_manager=_get_mcp_manager(),
                ip=request.client.host if request.client else "",
            )
        )
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield _sse(ev)
                except asyncio.TimeoutError:
                    if task.done():
                        while not queue.empty():
                            yield _sse(queue.get_nowait())
                        break
        finally:
            if not task.done():
                task.cancel()
        try:
            answer, tools = task.result()
        except Exception as e:
            yield _sse({"type": "error", "message": f"处理失败：{e}"})
            return
        yield _sse({"type": "done", "answer": answer, "tools": tools, "session_id": session_id})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _get_mcp_manager():
    from .main import mcp_manager

    return mcp_manager


# ---------- 反馈 ----------
class FeedbackRequest(BaseModel):
    session_id: str = ""
    rating: int = Field(ge=1, le=5)
    comment: str = ""


@router.post("/feedback")
async def api_feedback(req: FeedbackRequest, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO feedback(user_id,session_id,rating,comment) VALUES(?,?,?,?)",
            (user["id"], req.session_id, req.rating, req.comment),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------- 管理后台：知识库 ----------
class KnowledgeCreate(BaseModel):
    title: str
    category: str = "通用"
    content: str
    source: str = ""
    doc_type: str = "text"


class KnowledgeUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    enabled: Optional[int] = None


@router.get("/admin/knowledge")
async def admin_knowledge_list(
    keyword: str = "", doc_type: str = "", limit: int = 50, offset: int = 0,
    _: dict = Depends(require_roles("admin")),
):
    rows, total, by_type = knowledge.list_docs(limit=limit, offset=offset, keyword=keyword, doc_type=doc_type)
    return {"items": rows, "total": total, "by_type": by_type, "index": knowledge.index_info()}


@router.post("/admin/knowledge")
async def admin_knowledge_add(req: KnowledgeCreate, _: dict = Depends(require_roles("admin"))):
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(status_code=400, detail="标题和内容不能为空")
    doc_id = knowledge.add_doc(
        req.title.strip(),
        req.category.strip() or "通用",
        req.content.strip(),
        req.source.strip(),
        req.doc_type.strip() or "text",
    )
    return {"ok": True, "id": doc_id}


@router.put("/admin/knowledge/{doc_id}")
async def admin_knowledge_update(doc_id: int, req: KnowledgeUpdate, _: dict = Depends(require_roles("admin"))):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not knowledge.update_doc(doc_id, fields):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True}


@router.delete("/admin/knowledge/{doc_id}")
async def admin_knowledge_delete(doc_id: int, _: dict = Depends(require_roles("admin"))):
    if not knowledge.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True}


@router.post("/admin/knowledge/rebuild")
async def admin_knowledge_rebuild(_: dict = Depends(require_roles("admin"))):
    n = knowledge.rebuild_index()
    return {"ok": True, "chunks": n, "index": knowledge.index_info()}


@router.get("/admin/knowledge/index")
async def admin_knowledge_index(_: dict = Depends(require_roles("admin"))):
    """查看当前检索索引状态：文档数、分块数、向量后端与维度。"""
    return {"index": knowledge.index_info(), "roots": [str(p) for p in knowledge.scan_roots()]}


@router.post("/admin/knowledge/rescan")
async def admin_knowledge_rescan(_: dict = Depends(require_roles("admin"))):
    """重新扫描知识文件目录（文档/PDF/表格），按内容哈希增量导入。"""
    stats = knowledge.sync_all()
    return {"ok": True, "stats": stats, "index": knowledge.index_info()}


@router.post("/admin/knowledge/upload")
async def admin_knowledge_upload(file: UploadFile = File(...), _: dict = Depends(require_roles("admin"))):
    """上传文档 / PDF / 表格并立即入库（支持 md/txt/pdf/docx/xlsx/csv）。"""
    name = Path(file.filename or "").name
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if Path(name).suffix.lower() not in ingest.SUPPORTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail="暂不支持该格式，支持：" + "、".join(sorted(ingest.SUPPORTED_EXTS)),
        )
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    pieces = ingest.load_file(dest)
    if not pieces:
        raise HTTPException(status_code=400, detail="文件已保存，但没有解析出可用内容（可能是扫描版 PDF 或空文件）")
    stats = knowledge.sync_all()
    return {"ok": True, "file": name, "chunks": len(pieces), "stats": stats}


@router.get("/admin/knowledge/search")
async def admin_knowledge_search(
    q: str, mode: str = "hybrid", top_k: int = 5, _: dict = Depends(require_roles("admin"))
):
    """检索测试：一次看清 BM25 / 向量 / 融合三路结果，用于演示与调参。"""
    if not q.strip():
        raise HTTPException(status_code=400, detail="请输入检索词")
    return knowledge.search_detail(q.strip(), top_k=max(1, min(top_k, 10)), mode=mode)


# 注意：带路径参数的路由必须放在 /admin/knowledge/index、/admin/knowledge/search 之后注册，
# 否则 "index"/"search" 会被当成 doc_id 解析（FastAPI 按注册顺序匹配）。
@router.get("/admin/knowledge/{doc_id}")
async def admin_knowledge_get(doc_id: int, _: dict = Depends(require_roles("admin"))):
    doc = knowledge.get_doc(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


# ---------- 管理后台：用户 ----------
class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "student"
    name: str = ""
    student_no: str = ""
    department: str = ""
    class_name: str = ""


class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    student_no: Optional[str] = None
    department: Optional[str] = None
    class_name: Optional[str] = None
    enabled: Optional[int] = None


@router.get("/admin/users")
async def admin_users_list(_: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,username,role,name,student_no,department,class_name,enabled,created_at FROM users ORDER BY id"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/admin/users")
async def admin_users_add(req: UserCreate, _: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        if conn.execute("SELECT id FROM users WHERE username=?", (req.username.strip(),)).fetchone():
            raise HTTPException(status_code=400, detail="用户名已存在")
        conn.execute(
            "INSERT INTO users(username,password_hash,role,name,student_no,department,class_name) VALUES(?,?,?,?,?,?,?)",
            (
                req.username.strip(),
                hash_password(req.password),
                req.role,
                req.name.strip() or req.username.strip(),
                req.student_no.strip() or None,
                req.department.strip() or None,
                req.class_name.strip() or None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.put("/admin/users/{user_id}")
async def admin_users_update(user_id: int, req: UserUpdate, _: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        fields = req.model_dump(exclude_none=True)
        if "password" in fields:
            fields["password_hash"] = hash_password(fields.pop("password"))
        for k, v in fields.items():
            conn.execute(f"UPDATE users SET {k}=? WHERE id=?", (v, user_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/admin/users/{user_id}")
async def admin_users_delete(user_id: int, _: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="用户不存在")
    finally:
        conn.close()
    return {"ok": True}


# ---------- 管理后台：外部 MCP ----------
class MCPCreate(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict = {}
    transport: str = "stdio"
    enabled: bool = True


@router.get("/admin/mcp")
async def admin_mcp_list(_: dict = Depends(require_roles("admin"))):
    from .main import mcp_manager

    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM mcp_servers ORDER BY id").fetchall()
        items = [dict(r) for r in rows]
    finally:
        conn.close()
    status = {s["name"]: s for s in mcp_manager.status()}
    for it in items:
        try:
            it["args"] = json.loads(it["args"]) if isinstance(it["args"], str) else (it["args"] or [])
        except Exception:
            it["args"] = []
        try:
            it["env"] = json.loads(it["env"]) if isinstance(it["env"], str) else (it["env"] or {})
        except Exception:
            it["env"] = {}
        st = status.get(it["name"])
        if st:
            it["status"] = st["status"]
            it["last_error"] = st.get("last_error")
            it["tools"] = st.get("tools", [])
    return {"items": items}


@router.post("/admin/mcp")
async def admin_mcp_add(req: MCPCreate, _: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        if conn.execute("SELECT id FROM mcp_servers WHERE name=?", (req.name.strip(),)).fetchone():
            raise HTTPException(status_code=400, detail="MCP 服务名已存在")
        conn.execute(
            "INSERT INTO mcp_servers(name,command,args,env,transport,enabled,status) VALUES(?,?,?,?,?,?,?)",
            (
                req.name.strip(),
                req.command.strip(),
                json.dumps(req.args, ensure_ascii=False),
                json.dumps(req.env, ensure_ascii=False),
                req.transport,
                1 if req.enabled else 0,
                "unknown",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    from .main import mcp_manager

    mcp_manager.reload(_mcp_rows())
    return {"ok": True}


def _mcp_rows():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM mcp_servers").fetchall()]
    finally:
        conn.close()


@router.post("/admin/mcp/{mcp_id}/toggle")
async def admin_mcp_toggle(mcp_id: int, _: dict = Depends(require_roles("admin"))):
    from .main import mcp_manager

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mcp_servers WHERE id=?", (mcp_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP 服务不存在")
        new_enabled = 0 if row["enabled"] else 1
        conn.execute("UPDATE mcp_servers SET enabled=? WHERE id=?", (new_enabled, mcp_id))
        conn.commit()
    finally:
        conn.close()
    mcp_manager.reload(_mcp_rows())
    return {"ok": True, "enabled": bool(new_enabled)}


@router.post("/admin/mcp/{mcp_id}/test")
async def admin_mcp_test(mcp_id: int, _: dict = Depends(require_roles("admin"))):
    from .main import mcp_manager

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mcp_servers WHERE id=?", (mcp_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    handle = await mcp_manager.start_one(dict(row))
    return {"ok": True, "status": handle.status, "last_error": handle.last_error, "tools": [t["name"] for t in handle.tools]}


@router.delete("/admin/mcp/{mcp_id}")
async def admin_mcp_delete(mcp_id: int, _: dict = Depends(require_roles("admin"))):
    from .main import mcp_manager

    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mcp_servers WHERE id=?", (mcp_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP 服务不存在")
        conn.execute("DELETE FROM mcp_servers WHERE id=?", (mcp_id,))
        conn.commit()
    finally:
        conn.close()
    mcp_manager.reload(_mcp_rows())
    return {"ok": True}


# ---------- 管理后台：统计、日志、工单、审批 ----------
@router.get("/admin/stats")
async def admin_stats(_: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        total_queries = conn.execute("SELECT COUNT(*) AS c FROM audit_logs").fetchone()["c"]
        today_queries = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_logs WHERE date(created_at)=date('now','localtime')"
        ).fetchone()["c"]
        user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        doc_count = conn.execute("SELECT COUNT(*) AS c FROM knowledge_docs WHERE enabled=1").fetchone()["c"]
        pending = conn.execute(
            "SELECT (SELECT COUNT(*) FROM leaves WHERE status='pending') + "
            "(SELECT COUNT(*) FROM bookings WHERE status='pending') + "
            "(SELECT COUNT(*) FROM repairs WHERE status='pending') AS c"
        ).fetchone()["c"]
        open_tickets = conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'").fetchone()["c"]
        feedback_rows = conn.execute("SELECT rating, COUNT(*) AS c FROM feedback GROUP BY rating").fetchall()
        feedback = {r["rating"]: r["c"] for r in feedback_rows}
        tool_rows = conn.execute(
            "SELECT tools FROM audit_logs WHERE tools IS NOT NULL AND tools != '' ORDER BY id DESC LIMIT 200"
        ).fetchall()
        tool_counter: dict[str, int] = {}
        for r in tool_rows:
            try:
                for name in json.loads(r["tools"]):
                    tool_counter[name] = tool_counter.get(name, 0) + 1
            except Exception:
                pass
        top_tools = sorted(tool_counter.items(), key=lambda x: x[1], reverse=True)[:10]
        daily = conn.execute(
            "SELECT date(created_at) AS d, COUNT(*) AS c FROM audit_logs GROUP BY date(created_at) ORDER BY d DESC LIMIT 14"
        ).fetchall()
        intent_rows = conn.execute(
            "SELECT intent, COUNT(*) AS c FROM (SELECT intent FROM audit_logs WHERE intent IS NOT NULL AND intent != '' ORDER BY id DESC LIMIT 200) GROUP BY intent ORDER BY c DESC LIMIT 10"
        ).fetchall()
        return {
            "total_queries": total_queries,
            "today_queries": today_queries,
            "user_count": user_count,
            "doc_count": doc_count,
            "pending_applications": pending,
            "open_tickets": open_tickets,
            "feedback": feedback,
            "top_tools": [{"name": n, "count": c} for n, c in top_tools],
            "daily": [{"date": r["d"], "count": r["c"]} for r in daily],
            "top_intents": [{"name": r["intent"], "count": r["c"]} for r in intent_rows],
        }
    finally:
        conn.close()


@router.get("/admin/logs")
async def admin_logs(limit: int = 100, _: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(limit, 500),)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/admin/feedback")
async def admin_feedback(_: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT f.*, u.username FROM feedback f
               LEFT JOIN users u ON u.id=f.user_id ORDER BY f.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/admin/tickets")
async def admin_tickets(_: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT t.*, u.name AS user_name, u.username FROM tickets t
               LEFT JOIN users u ON u.id=t.user_id ORDER BY (t.status='open') DESC, t.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
    finally:
        conn.close()


class TicketReply(BaseModel):
    reply: str


@router.post("/admin/tickets/{ticket_id}/reply")
async def admin_ticket_reply(ticket_id: int, req: TicketReply, user: dict = Depends(require_roles("admin"))):
    conn = get_conn()
    try:
        if not req.reply.strip():
            raise HTTPException(status_code=400, detail="回复内容不能为空")
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="工单不存在")
        cur = conn.execute(
            "UPDATE tickets SET status='closed', reply=?, read_by_user=0, updated_at=datetime('now','localtime') WHERE id=?",
            (req.reply.strip(), ticket_id),
        )
        conn.commit()
    finally:
        conn.close()
    # 主动推送：管理员回复人工工单后，学生端立刻收到通知
    notify.push(
        row["user_id"],
        "ticket_reply",
        "你的人工服务工单已回复",
        f"工单 #{ticket_id}（{row['question']}）已由 {user['name']} 回复：{req.reply.strip()}",
        link=f"ticket:{ticket_id}",
    )
    return {"ok": True}


# ---------- 主动推送：站内通知 ----------
@router.get("/notifications")
async def my_notifications(limit: int = 30, unread: int = 0, user: dict = Depends(get_current_user)):
    """当前用户的通知列表（unread=1 只看未读）。前端每 5 秒轮询，实现主动推送。"""
    return {
        "count": notify.unread_count(user["id"]),
        "items": notify.list_for_user(user["id"], limit=limit, unread_only=bool(unread)),
    }


@router.post("/notifications/{notice_id}/read")
async def read_notification(notice_id: int, user: dict = Depends(get_current_user)):
    ok = notify.mark_read(user["id"], notice_id)
    if not ok:
        # 已读或不属于自己都不报错，保持前端轮询幂等
        return {"ok": True, "already": True}
    _sync_ticket_read(user["id"], notice_id)
    return {"ok": True}


@router.post("/notifications/read_all")
async def read_all_notifications(user: dict = Depends(get_current_user)):
    n = notify.mark_all_read(user["id"])
    return {"ok": True, "read": n}


def _sync_ticket_read(user_id: int, notice_id: int) -> None:
    """通知被读掉时，同步把对应工单标记为已读，避免旧横幅重复弹出。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT type, link FROM notifications WHERE id=? AND user_id=?", (notice_id, user_id)
        ).fetchone()
        if row and row["type"] == "ticket_reply" and row["link"]:
            try:
                tid = int(str(row["link"]).split(":")[-1])
            except ValueError:
                return
            conn.execute("UPDATE tickets SET read_by_user=1 WHERE id=? AND user_id=?", (tid, user_id))
            conn.commit()
    finally:
        conn.close()


# ---------- 用户端：工单回复送达 ----------
@router.get("/tickets/unread")
async def my_unread_tickets(user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, question, reply, created_at, updated_at FROM tickets
               WHERE user_id=? AND status='closed' AND reply IS NOT NULL AND read_by_user=0
               ORDER BY id DESC""",
            (user["id"],),
        ).fetchall()
        return {"count": len(rows), "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/tickets/{ticket_id}/read")
async def mark_ticket_read(ticket_id: int, user: dict = Depends(get_current_user)):
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE tickets SET read_by_user=1 WHERE id=? AND user_id=?",
            (ticket_id, user["id"]),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="工单不存在或不属于当前用户")
    finally:
        conn.close()
    return {"ok": True}


@router.get("/admin/applications")
async def admin_applications(_: dict = Depends(require_roles("teacher", "admin"))):
    conn = get_conn()
    try:
        leaves = conn.execute(
            """SELECT l.*, u.name AS user_name, u.username FROM leaves l
               LEFT JOIN users u ON u.id=l.user_id ORDER BY l.status='pending' DESC, l.id DESC LIMIT 100"""
        ).fetchall()
        repairs = conn.execute(
            """SELECT r.*, u.name AS user_name, u.username FROM repairs r
               LEFT JOIN users u ON u.id=r.user_id ORDER BY r.status='pending' DESC, r.id DESC LIMIT 100"""
        ).fetchall()
        bookings = conn.execute(
            """SELECT b.*, v.name AS venue_name, u.name AS user_name FROM bookings b
               LEFT JOIN venues v ON v.id=b.venue_id
               LEFT JOIN users u ON u.id=b.user_id ORDER BY b.status='pending' DESC, b.id DESC LIMIT 100"""
        ).fetchall()
        return {
            "leaves": [dict(r) for r in leaves],
            "repairs": [dict(r) for r in repairs],
            "bookings": [dict(r) for r in bookings],
        }
    finally:
        conn.close()


class LeaveDecision(BaseModel):
    approve: bool = True
    note: str = ""


@router.post("/admin/leaves/{leave_id}/decision")
async def admin_leave_decision(leave_id: int, req: LeaveDecision, user: dict = Depends(require_roles("teacher", "admin"))):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM leaves WHERE id=?", (leave_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="请假申请不存在")
        conn.execute(
            "UPDATE leaves SET status=?, approver_id=?, note=?, updated_at=datetime('now','localtime') WHERE id=?",
            ("approved" if req.approve else "rejected", user["id"], req.note.strip() or None, leave_id),
        )
        conn.commit()
    finally:
        conn.close()
    # 主动推送：审批结果立刻通知请假人
    word = "已批准" if req.approve else "已驳回"
    note = req.note.strip()
    notify.push(
        row["user_id"],
        "leave_result",
        f"请假申请{word}",
        f"你的{row['leave_type']}申请（#{leave_id}，{row['start_date']} 至 {row['end_date']}）"
        f"审批结果：{word}。审批人：{user['name']}。" + (f"审批备注：{note}" if note else ""),
        link=f"leave:{leave_id}",
    )
    return {"ok": True}


class RepairUpdate(BaseModel):
    status: str
    note: str = ""


@router.post("/admin/repairs/{repair_id}/update")
async def admin_repair_update(repair_id: int, req: RepairUpdate, user: dict = Depends(require_roles("teacher", "admin"))):
    if req.status not in ("pending", "processing", "done", "closed"):
        raise HTTPException(status_code=400, detail="状态不合法")
    status_cn = {"pending": "待处理", "processing": "处理中", "done": "已完成", "closed": "已关闭"}
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM repairs WHERE id=?", (repair_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="报修单不存在")
        cur = conn.execute(
            "UPDATE repairs SET status=?, handler_note=?, updated_at=datetime('now','localtime') WHERE id=?",
            (req.status, req.note.strip() or None, repair_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="报修单不存在")
    finally:
        conn.close()
    notify.push(
        row["user_id"],
        "repair_update",
        f"报修单状态更新：{status_cn.get(req.status, req.status)}",
        f"你的报修单 #{repair_id}（{row['location']}）状态变为「{status_cn.get(req.status, req.status)}」"
        + (f"，处理说明：{req.note.strip()}" if req.note.strip() else "") + f"。处理人：{user['name']}",
        link=f"repair:{repair_id}",
    )
    return {"ok": True}


class BookingDecision(BaseModel):
    approve: bool = True


@router.post("/admin/bookings/{booking_id}/decision")
async def admin_booking_decision(booking_id: int, req: BookingDecision, user: dict = Depends(require_roles("teacher", "admin"))):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="预约不存在")
        cur = conn.execute(
            "UPDATE bookings SET status=? WHERE id=?",
            ("approved" if req.approve else "rejected", booking_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="预约不存在")
    finally:
        conn.close()
    word = "已通过" if req.approve else "被驳回"
    notify.push(
        row["user_id"],
        "booking_result",
        f"场地预约{word}",
        f"你的场地预约 #{booking_id}（{row['book_date']} {row['start_time']}-{row['end_time']}）{word}。审批人：{user['name']}",
        link=f"booking:{booking_id}",
    )
    return {"ok": True}
