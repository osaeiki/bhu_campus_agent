"""主动推送消息中心。

场景：教师/管理员批准请假、回复人工工单、更新报修或审批场地后，
系统主动给相关用户写一条站内通知；前端每 5 秒轮询一次，
用横幅 + 右上角气泡的方式主动弹出，不需要用户再问一遍。

设计要点：
- 通知只写数据库，不依赖任何长连接，进程重启后未读消息依然存在；
- 同时被 API 路由（后台按钮）和本地工具（对话里审批）调用，
  保证“从哪儿审批都会推送”；
- 与旧的人工工单未读标记（tickets.read_by_user）并存，互不影响。
"""
from .database import get_conn

# 通知类型 -> 中文标签（前端弹窗标题用）
TYPE_LABEL = {
    "leave_result": "请假审批结果",
    "ticket_reply": "人工服务回复",
    "repair_update": "报修处理进度",
    "booking_result": "场地预约结果",
    "todo_leave": "新的待审批请假",
    "todo_repair": "新的待处理报修",
    "todo_booking": "新的待审批预约",
    "todo_ticket": "新的人工工单",
    "system": "系统通知",
}


def push(user_id, ntype: str, title: str, content: str = "", link: str = "") -> int:
    """写入一条通知，返回通知 id；user_id 为空时静默跳过。"""
    if not user_id:
        return 0
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO notifications(user_id,type,title,content,link) VALUES(?,?,?,?,?)",
            (int(user_id), ntype, title, content or "", link or ""),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def push_many(user_ids, ntype: str, title: str, content: str = "", link: str = "") -> int:
    """批量推送（如给全体教师推待办），返回写入条数。"""
    n = 0
    for uid in user_ids or []:
        if push(uid, ntype, title, content, link):
            n += 1
    return n


def notify_role(roles, ntype: str, title: str, content: str = "", link: str = "",
                exclude_user_id: int = 0) -> int:
    """给指定角色的所有启用用户推送通知。"""
    if isinstance(roles, str):
        roles = [roles]
    placeholders = ",".join("?" for _ in roles)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT id FROM users WHERE enabled=1 AND role IN ({placeholders})",
            tuple(roles),
        ).fetchall()
    finally:
        conn.close()
    uids = [r["id"] for r in rows if r["id"] != exclude_user_id]
    return push_many(uids, ntype, title, content, link)


def list_for_user(user_id: int, limit: int = 30, unread_only: bool = False) -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM notifications WHERE user_id=?"
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(sql, (user_id, max(1, min(limit, 100)))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def unread_count(user_id: int) -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND read_at IS NULL",
            (user_id,),
        ).fetchone()["c"]
    finally:
        conn.close()


def mark_read(user_id: int, notice_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE notifications SET read_at=datetime('now','localtime') "
            "WHERE id=? AND user_id=? AND read_at IS NULL",
            (notice_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_all_read(user_id: int) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE notifications SET read_at=datetime('now','localtime') "
            "WHERE user_id=? AND read_at IS NULL",
            (user_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
