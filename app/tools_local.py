"""本地校园工具：课表、成绩、请假、报修、场地预约、知识库检索、人工兜底、备用网页搜索。

每个工具带 role 权限：工具只对满足角色的用户可见，执行时再次校验。
"""
import json
import re
from datetime import datetime
from urllib.parse import quote

import httpx

from . import campus_map, config, knowledge, notify
from .database import get_conn


WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DOC_TYPE_CN = {"text": "文档", "pdf": "PDF", "table": "表格"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def _valid_time(s: str) -> bool:
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except Exception:
        return False


def _week_contains(weeks: str, week: int) -> bool:
    for part in weeks.replace("周", "").split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m and int(m.group(1)) <= week <= int(m.group(2)):
            return True
        if part.isdigit() and int(part) == week:
            return True
    return False


async def _search_knowledge(user: dict, args: dict) -> str:
    query = str(args.get("query", "")).strip()
    top_k = min(int(args.get("top_k", 4)), 6)
    if not query:
        return "请提供要查询的问题。"
    results = knowledge.search(query, top_k=top_k)
    if not results:
        return "知识库中没有找到相关内容。请换一种问法，或使用 ask_human 转人工。"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] 标题：{r['title']}（分类：{r['category']}，"
            f"类型：{DOC_TYPE_CN.get(r.get('doc_type', 'text'), '文档')}，"
            f"来源：{r.get('source') or '校内资料'}）"
        )
        lines.append(f"内容：{r['chunk']}")
    return "\n".join(lines)


async def _query_course(user: dict, args: dict) -> str:
    role = user["role"]
    conn = get_conn()
    try:
        if role == "student":
            rows = conn.execute(
                "SELECT * FROM courses WHERE class_name=? ORDER BY day_of_week,start_section",
                (user.get("class_name"),),
            ).fetchall()
            who = f"{user['name']}（{user.get('class_name')}）"
        elif role in ("teacher", "admin"):
            rows = conn.execute(
                "SELECT * FROM courses WHERE teacher_name=? ORDER BY day_of_week,start_section",
                (user["name"],),
            ).fetchall()
            who = f"教师 {user['name']}"
        else:
            return "访客账号无法查询课表，请使用学生或教师账号登录。"
        if not rows:
            return f"{who} 没有查询到课程安排。"
        week = args.get("week")
        lines = [f"{who} 的课表："]
        for r in rows:
            if week and not _week_contains(r["weeks"], int(week)):
                continue
            lines.append(
                f"· {r['name']}｜{WEEKDAYS[r['day_of_week'] - 1]} 第{r['start_section']}-{r['end_section']}节｜{r['weeks']}｜{r['location']}（{r['teacher_name']}）"
            )
        return "\n".join(lines)
    finally:
        conn.close()


async def _query_score(user: dict, args: dict) -> str:
    if user["role"] != "student":
        return "只有学生账号可以查询成绩。"
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT course_name,score,credit,semester FROM grades WHERE student_id=? ORDER BY semester,id",
            (user["id"],),
        ).fetchall()
        if not rows:
            return "暂无成绩记录。"
        lines = [f"{user['name']} 的成绩单："]
        total = 0.0
        creds = 0.0
        for r in rows:
            lines.append(f"· {r['course_name']}：{r['score']} 分（{r['credit']} 学分，{r['semester']}）")
            total += r["score"] * r["credit"]
            creds += r["credit"]
        if creds:
            lines.append(f"加权平均分：{total / creds:.1f}")
        return "\n".join(lines)
    finally:
        conn.close()


async def _submit_leave(user: dict, args: dict) -> str:
    if user["role"] not in ("student", "teacher"):
        return "当前账号不能提交请假申请。"
    leave_type = str(args.get("leave_type", "")).strip() or "事假"
    reason = str(args.get("reason", "")).strip()
    start = str(args.get("start_date", "")).strip()
    end = str(args.get("end_date", "")).strip()
    if not reason or not _valid_date(start) or not _valid_date(end):
        return "参数不完整：需要 reason（事由）、start_date、end_date（格式 YYYY-MM-DD）。"
    if start > end:
        return "开始日期不能晚于结束日期。"
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO leaves(user_id,leave_type,reason,start_date,end_date,status) VALUES(?,?,?,?,?,?)",
            (user["id"], leave_type, reason, start, end, "pending"),
        )
        conn.commit()
        lid = cur.lastrowid
        # 主动推送：给学生本人确认，同时推给教师/管理员作为待办
        notify.push(
            user["id"],
            "system",
            "请假申请已提交",
            f"你的{leave_type}申请（#{lid}，{start} 至 {end}）已提交，状态：待审批。审批结果会主动通知你。",
            link=f"leave:{lid}",
        )
        notify.notify_role(
            ["teacher", "admin"],
            "todo_leave",
            "新的请假申请待审批",
            f"{user['name']}（{user.get('class_name') or '—'}）提交了{leave_type}申请 #{lid}，"
            f"时间 {start} 至 {end}，事由：{reason}。",
            link=f"leave:{lid}",
            exclude_user_id=user["id"],
        )
        return f"请假申请已提交，申请编号 #{lid}。类型：{leave_type}，时间：{start} 至 {end}，事由：{reason}。当前状态：待审批。可让辅导员/教师在审批列表处理。"
    finally:
        conn.close()


async def _my_leave_status(user: dict, args: dict) -> str:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM leaves WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)
        ).fetchall()
        if not rows:
            return "你还没有请假记录。"
        lines = [f"{user['name']} 的请假记录："]
        for r in rows:
            note = f"，备注：{r['note']}" if r["note"] else ""
            lines.append(
                f"· #{r['id']} {r['leave_type']} {r['start_date']}~{r['end_date']}｜{r['reason']}｜状态：{r['status']}{note}"
            )
        return "\n".join(lines)
    finally:
        conn.close()


async def _approve_leave(user: dict, args: dict) -> str:
    if user["role"] not in ("teacher", "admin"):
        return "只有教师或管理员可以审批请假。"
    leave_id = int(args.get("leave_id", 0))
    approve = bool(args.get("approve", True))
    note = str(args.get("note", "")).strip()
    status = "approved" if approve else "rejected"
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM leaves WHERE id=?", (leave_id,)).fetchone()
        if not row:
            return f"请假申请 #{leave_id} 不存在。"
        conn.execute(
            "UPDATE leaves SET status=?, approver_id=?, note=?, updated_at=? WHERE id=?",
            (status, user["id"], note or None, _now(), leave_id),
        )
        conn.commit()
        # 主动推送：审批结果通知请假人
        notify.push(
            row["user_id"],
            "leave_result",
            f"请假申请已{'批准' if approve else '驳回'}",
            f"你的{row['leave_type']}申请（#{leave_id}，{row['start_date']} 至 {row['end_date']}）"
            f"审批结果：已{'批准' if approve else '驳回'}。审批人：{user['name']}。"
            + (f"审批备注：{note}" if note else ""),
            link=f"leave:{leave_id}",
        )
        return f"请假申请 #{leave_id} 已{'批准' if approve else '驳回'}。"
    finally:
        conn.close()


async def _submit_repair(user: dict, args: dict) -> str:
    if user["role"] not in ("student", "teacher", "admin"):
        return "当前账号不能提交报修。"
    location = str(args.get("location", "")).strip()
    category = str(args.get("category", "")).strip() or "其他"
    desc = str(args.get("description", "")).strip()
    if not location or not desc:
        return "参数不完整：需要 location（报修地点）和 description（问题描述）。"
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO repairs(user_id,location,category,description,status) VALUES(?,?,?,?,?)",
            (user["id"], location, category, desc, "pending"),
        )
        conn.commit()
        rid = cur.lastrowid
        notify.push(
            user["id"],
            "system",
            "报修单已提交",
            f"你的报修单 #{rid}（{location}·{category}）已提交，状态：待处理。处理进度会主动通知你。",
            link=f"repair:{rid}",
        )
        notify.notify_role(
            ["teacher", "admin"],
            "todo_repair",
            "新的报修单待处理",
            f"{user['name']} 提交了报修单 #{rid}：{location}｜{category}｜{desc}",
            link=f"repair:{rid}",
            exclude_user_id=user["id"],
        )
        return f"报修单已提交，编号 #{rid}。地点：{location}，类别：{category}，描述：{desc}。状态：待处理。"
    finally:
        conn.close()


async def _my_repair_status(user: dict, args: dict) -> str:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM repairs WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)
        ).fetchall()
        if not rows:
            return "你还没有报修记录。"
        lines = [f"{user['name']} 的报修记录："]
        for r in rows:
            note = f"，处理说明：{r['handler_note']}" if r["handler_note"] else ""
            lines.append(f"· #{r['id']} {r['location']}｜{r['category']}｜{r['description']}｜状态：{r['status']}{note}")
        return "\n".join(lines)
    finally:
        conn.close()


async def _update_repair(user: dict, args: dict) -> str:
    if user["role"] not in ("teacher", "admin"):
        return "只有教师或管理员可以处理报修。"
    rid = int(args.get("repair_id", 0))
    status = str(args.get("status", "")).strip()
    note = str(args.get("note", "")).strip()
    if status not in ("pending", "processing", "done", "closed"):
        return "状态只能是 pending / processing / done / closed。"
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM repairs WHERE id=?", (rid,)).fetchone()
        if not row:
            return f"报修单 #{rid} 不存在。"
        conn.execute(
            "UPDATE repairs SET status=?, handler_note=?, updated_at=? WHERE id=?",
            (status, note or None, _now(), rid),
        )
        conn.commit()
        status_cn = {"pending": "待处理", "processing": "处理中", "done": "已完成", "closed": "已关闭"}
        notify.push(
            row["user_id"],
            "repair_update",
            f"报修单状态更新：{status_cn.get(status, status)}",
            f"你的报修单 #{rid}（{row['location']}）状态变为「{status_cn.get(status, status)}」"
            + (f"，处理说明：{note}" if note else "") + f"。处理人：{user['name']}",
            link=f"repair:{rid}",
        )
        return f"报修单 #{rid} 状态已更新为 {status}。"
    finally:
        conn.close()


async def _list_venues(user: dict, args: dict) -> str:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM venues ORDER BY id").fetchall()
        if not rows:
            return "暂无可预约场地。"
        lines = ["可预约场地："]
        for r in rows:
            lines.append(
                f"· #{r['id']} {r['name']}｜{r['location']}｜容量 {r['capacity']} 人｜开放 {r['open_time']}-{r['close_time']}"
            )
        return "\n".join(lines)
    finally:
        conn.close()


async def _book_venue(user: dict, args: dict) -> str:
    if user["role"] not in ("student", "teacher", "admin"):
        return "访客账号不能预约场地。"
    venue_id = int(args.get("venue_id", 0))
    book_date = str(args.get("book_date", "")).strip()
    start = str(args.get("start_time", "")).strip()
    end = str(args.get("end_time", "")).strip()
    purpose = str(args.get("purpose", "")).strip()
    if not _valid_date(book_date) or not _valid_time(start) or not _valid_time(end) or start >= end:
        return "参数不完整或时间不合法：book_date(YYYY-MM-DD)、start_time、end_time(HH:MM)。"
    conn = get_conn()
    try:
        venue = conn.execute("SELECT * FROM venues WHERE id=?", (venue_id,)).fetchone()
        if not venue:
            return "场地不存在，请先用 list_venues 查看可预约场地。"
        if start < venue["open_time"] or end > venue["close_time"]:
            return f"该场地开放时间为 {venue['open_time']}-{venue['close_time']}，预约时间超出开放范围。"
        clash = conn.execute(
            "SELECT id FROM bookings WHERE venue_id=? AND book_date=? AND status!='rejected' AND start_time < ? AND end_time > ? LIMIT 1",
            (venue_id, book_date, end, start),
        ).fetchone()
        if clash:
            return f"该时间段已被预约（冲突预约 # {clash['id']}），请选择其他时间。"
        cur = conn.execute(
            "INSERT INTO bookings(venue_id,user_id,book_date,start_time,end_time,purpose,status) VALUES(?,?,?,?,?,?,?)",
            (venue_id, user["id"], book_date, start, end, purpose or None, "pending"),
        )
        conn.commit()
        bid = cur.lastrowid
        notify.push(
            user["id"],
            "system",
            "场地预约已提交",
            f"你的场地预约 #{bid}（{venue['name']}，{book_date} {start}-{end}）已提交，状态：待审批。",
            link=f"booking:{bid}",
        )
        notify.notify_role(
            ["teacher", "admin"],
            "todo_booking",
            "新的场地预约待审批",
            f"{user['name']} 提交了场地预约 #{bid}：{venue['name']}，{book_date} {start}-{end}，事由：{purpose or '未填写'}。",
            link=f"booking:{bid}",
            exclude_user_id=user["id"],
        )
        return f"场地预约已提交，预约编号 #{bid}：{venue['name']}，{book_date} {start}-{end}，事由：{purpose or '未填写'}。状态：待审批。"
    finally:
        conn.close()


async def _my_bookings(user: dict, args: dict) -> str:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT b.*, v.name AS venue_name FROM bookings b
               JOIN venues v ON v.id=b.venue_id
               WHERE b.user_id=? ORDER BY b.id DESC LIMIT 10""",
            (user["id"],),
        ).fetchall()
        if not rows:
            return "你还没有场地预约记录。"
        lines = [f"{user['name']} 的场地预约："]
        for r in rows:
            lines.append(
                f"· #{r['id']} {r['venue_name']} {r['book_date']} {r['start_time']}-{r['end_time']}｜{r['purpose'] or '无'}｜状态：{r['status']}"
            )
        return "\n".join(lines)
    finally:
        conn.close()


async def _approve_booking(user: dict, args: dict) -> str:
    if user["role"] not in ("teacher", "admin"):
        return "只有教师或管理员可以审批场地预约。"
    bid = int(args.get("booking_id", 0))
    approve = bool(args.get("approve", True))
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
        if not row:
            return f"预约 #{bid} 不存在。"
        conn.execute(
            "UPDATE bookings SET status=? WHERE id=?", ("approved" if approve else "rejected", bid)
        )
        conn.commit()
        notify.push(
            row["user_id"],
            "booking_result",
            f"场地预约已{'通过' if approve else '驳回'}",
            f"你的场地预约 #{bid}（{row['book_date']} {row['start_time']}-{row['end_time']}）"
            f"已{'通过' if approve else '被驳回'}。审批人：{user['name']}",
            link=f"booking:{bid}",
        )
        return f"场地预约 #{bid} 已{'批准' if approve else '驳回'}。"
    finally:
        conn.close()


async def _ask_human(user: dict, args: dict) -> str:
    question = str(args.get("question", "")).strip()
    if not question:
        return "请描述你要人工协助的问题。"
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tickets(user_id,question,agent_context,status) VALUES(?,?,?,?)",
            (user["id"], question, json.dumps(args.get("context", ""), ensure_ascii=False), "open"),
        )
        conn.commit()
        tid = cur.lastrowid
        # 主动推送：管理员端待办提醒（学生端由轮询在收到回复后再推送）
        notify.notify_role(
            ["admin"],
            "todo_ticket",
            "新的人工工单待回复",
            f"{user['name']} 提交了人工工单 #{tid}：{question}",
            link=f"ticket:{tid}",
            exclude_user_id=user["id"],
        )
        return f"已转人工服务，工单编号 #{tid}。管理员会在后台回复，你也可以在后续对话中继续询问。"
    finally:
        conn.close()


async def _web_search_fallback(user: dict, args: dict) -> str:
    """备用联网搜索：外部 MCP 浏览器不可用时的兜底，优先 360 搜索，失败再试必应。"""
    query = str(args.get("query", "")).strip()
    if not query:
        return "请提供搜索关键词。"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    html = ""
    for url in (
        f"https://www.so.com/s?q={quote(query)}",
        f"https://www.bing.com/search?q={quote(query)}&mkt=zh-CN",
    ):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and len(resp.text) > 2000:
                html = resp.text
                break
        except Exception:
            continue
    if not html:
        return "联网搜索失败：搜索引擎均不可用。可稍后再试或使用 ask_human 转人工。"
    results = []
    # 360：li[class*=result] 内 h3 a 标题 + p 摘要
    for m in re.finditer(r'<li[^>]*class="[^"]*(?:res-list|result)[^"]*"[^>]*>.*?<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?(?:<p[^>]*>(.*?)</p>)?', html, re.S):
        href = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snippet = re.sub(r"<[^>]+>", "", m.group(3) or "").strip()
        if title:
            results.append({"title": title, "url": href, "snippet": snippet[:200]})
    # 必应：h2 a
    if not results:
        for m in re.finditer(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', html, re.S):
            results.append({"title": re.sub(r"<[^>]+>", "", m.group(2)).strip(), "url": m.group(1), "snippet": ""})
    if not results:
        return "没有搜到结果，可以换关键词，或使用 ask_human 转人工。"
    lines = [f"搜索结果（关键词：{query}）："]
    for r in results[:5]:
        lines.append(f"· {r['title']}（{r['url']}）" + (f"\n  {r['snippet']}" if r.get("snippet") else ""))
    return "\n".join(lines)


async def _campus_route(user: dict, args: dict) -> str:
    """北校区校内 A→B 步行路线。"""
    frm = str(args.get("from", "")).strip()
    to = str(args.get("to", "")).strip()
    if not frm or not to:
        return "请提供 from（出发地）和 to（目的地），例如：从 三教 到 图书馆。"
    return campus_map.route(frm, to)


async def _query_student_leave(user: dict, args: dict) -> str:
    """教师/管理员查询某学生的请假情况。"""
    if user["role"] not in ("teacher", "admin"):
        return "只有教师或管理员可以查询学生请假。"
    kw = str(args.get("student", "")).strip()
    if not kw:
        return "请提供学生姓名、学号或账号，例如：查一下李明是否请假。"
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT u.id, u.name, u.student_no, u.class_name, u.username FROM users u
               WHERE u.role='student' AND (u.name=? OR u.student_no=? OR u.username=? OR ?='') LIMIT 5""",
            (kw, kw, kw, kw),
        ).fetchall()
        rows = [dict(r) for r in rows]
        if not rows:
            return f"没有找到学生「{kw}」，请确认姓名/学号是否正确。"
        out = []
        for stu in rows:
            leaves = conn.execute(
                "SELECT * FROM leaves WHERE user_id=? ORDER BY id DESC LIMIT 20", (stu["id"],)
            ).fetchall()
            if not leaves:
                out.append(f"· {stu['name']}（{stu.get('class_name') or stu.get('student_no') or stu['username']}）：无请假记录")
                continue
            out.append(f"· {stu['name']}（{stu.get('class_name') or stu.get('student_no') or stu['username']}）请假记录：")
            for lv in leaves:
                note = f"，备注：{lv['note']}" if lv["note"] else ""
                out.append(
                    f"  #{lv['id']} {lv['leave_type']} {lv['start_date']}~{lv['end_date']}｜{lv['reason']}｜状态：{lv['status']}{note}"
                )
        return "\n".join(out)
    finally:
        conn.close()


async def _list_pending_applications(user: dict, args: dict) -> str:
    """教师/管理员查看待审批的请假/场地预约/待处理报修。"""
    if user["role"] not in ("teacher", "admin"):
        return "只有教师或管理员可以查看待办。"
    conn = get_conn()
    try:
        leaves = conn.execute(
            """SELECT l.*, u.name AS stu_name FROM leaves l LEFT JOIN users u ON u.id=l.user_id
               WHERE l.status='pending' ORDER BY l.id""",
        ).fetchall()
        bookings = conn.execute(
            """SELECT b.*, v.name AS venue_name, u.name AS stu_name FROM bookings b
               LEFT JOIN venues v ON v.id=b.venue_id LEFT JOIN users u ON u.id=b.user_id
               WHERE b.status='pending' ORDER BY b.id""",
        ).fetchall()
        repairs = conn.execute(
            """SELECT r.*, u.name AS stu_name FROM repairs r LEFT JOIN users u ON u.id=r.user_id
               WHERE r.status IN ('pending','processing') ORDER BY r.id""",
        ).fetchall()
        out = []
        if not leaves and not bookings and not repairs:
            return "当前没有待办：无待审批请假、场地预约或待处理报修。"
        if leaves:
            out.append(f"待审批请假 {len(leaves)} 条：")
            for lv in leaves:
                out.append(f"· #{lv['id']} {lv['stu_name']} {lv['leave_type']} {lv['start_date']}~{lv['end_date']}｜{lv['reason']}")
        if bookings:
            out.append(f"待审批场地预约 {len(bookings)} 条：")
            for bk in bookings:
                out.append(f"· #{bk['id']} {bk['stu_name']} {bk['venue_name']} {bk['book_date']} {bk['start_time']}-{bk['end_time']}｜{bk['purpose'] or ''}")
        if repairs:
            out.append(f"待处理报修 {len(repairs)} 条：")
            for rp in repairs:
                out.append(f"· #{rp['id']} {rp['stu_name']} {rp['location']}｜{rp['description']}")
        out.append("可在管理后台「申请审批」中处理，或让我直接批准/更新。")
        return "\n".join(out)
    finally:
        conn.close()


async def _my_ticket_status(user: dict, args: dict) -> str:
    """查询当前用户的人工工单及管理员回复。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)
        ).fetchall()
        if not rows:
            return "你还没有人工服务工单。"
        lines = [f"{user['name']} 的人工工单："]
        for t in rows:
            if t["status"] == "open":
                lines.append(f"· #{t['id']}（待处理）问题：{t['question']}")
            else:
                reply = t["reply"] or "（已关闭，无回复内容）"
                lines.append(f"· #{t['id']}（已回复）问题：{t['question']}\n  管理员回复：{reply}")
        return "\n".join(lines)
    finally:
        conn.close()


async def _reply_ticket(user: dict, args: dict) -> str:
    """管理员回复人工工单。"""
    if user["role"] != "admin":
        return "只有管理员可以回复工单。"
    tid = int(args.get("ticket_id", 0))
    reply = str(args.get("reply", "")).strip()
    if not reply:
        return "请提供回复内容。"
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not row:
            return f"工单 #{tid} 不存在。"
        conn.execute(
            "UPDATE tickets SET status='closed', reply=?, read_by_user=0, updated_at=datetime('now','localtime') WHERE id=?",
            (reply, tid),
        )
        conn.commit()
        # 主动推送：学生端立刻收到人工回复
        notify.push(
            row["user_id"],
            "ticket_reply",
            "你的人工服务工单已回复",
            f"工单 #{tid}（{row['question']}）已由 {user['name']} 回复：{reply}",
            link=f"ticket:{tid}",
        )
        return f"已回复工单 #{tid}，学生端会收到提示。回复内容：{reply}"
    finally:
        conn.close()


LOCAL_TOOLS = [
    {
        "name": "search_knowledge",
        "description": "检索北华大学北校区校园知识库（规章制度、办事流程、教学楼、食堂、宿舍、交通等校内信息）。回答校园相关问题前应优先使用本工具，并引用返回的标题与来源。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的问题或关键词"},
                "top_k": {"type": "integer", "description": "返回条数，默认4"},
            },
            "required": ["query"],
        },
        "role": None,
        "handler": _search_knowledge,
    },
    {
        "name": "query_course",
        "description": "查询课表。学生返回本班课表，教师返回本人授课课表。",
        "parameters": {
            "type": "object",
            "properties": {
                "week": {"type": "integer", "description": "第几周（可选，1-20）"},
            },
        },
        "role": None,
        "handler": _query_course,
    },
    {
        "name": "query_score",
        "description": "查询学生本人成绩与加权平均分。",
        "parameters": {"type": "object", "properties": {}},
        "role": "student",
        "handler": _query_score,
    },
    {
        "name": "submit_leave",
        "description": "提交请假申请（学生/教师）。参数：leave_type 请假类型、reason 事由、start_date、end_date（YYYY-MM-DD）。",
        "parameters": {
            "type": "object",
            "properties": {
                "leave_type": {"type": "string", "description": "请假类型，如事假/病假/公假"},
                "reason": {"type": "string", "description": "请假事由"},
                "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            },
            "required": ["reason", "start_date", "end_date"],
        },
        "role": None,
        "handler": _submit_leave,
    },
    {
        "name": "my_leave_status",
        "description": "查询当前用户提交的请假申请及审批状态。",
        "parameters": {"type": "object", "properties": {}},
        "role": None,
        "handler": _my_leave_status,
    },
    {
        "name": "approve_leave",
        "description": "审批请假申请（教师/管理员）。参数：leave_id 申请编号、approve 是否批准、note 备注。",
        "parameters": {
            "type": "object",
            "properties": {
                "leave_id": {"type": "integer", "description": "请假申请编号"},
                "approve": {"type": "boolean", "description": "true批准/false驳回"},
                "note": {"type": "string", "description": "审批备注"},
            },
            "required": ["leave_id"],
        },
        "roles": ["teacher", "admin"],
        "handler": _approve_leave,
    },
    {
        "name": "submit_repair",
        "description": "提交校园设施报修。参数：location 地点、category 类别、description 问题描述。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "报修地点，如 三教305 灯管坏了"},
                "category": {"type": "string", "description": "类别：水电/网络/桌椅/门窗/其他"},
                "description": {"type": "string", "description": "问题描述"},
            },
            "required": ["location", "description"],
        },
        "role": None,
        "handler": _submit_repair,
    },
    {
        "name": "my_repair_status",
        "description": "查询当前用户提交的报修记录及处理状态。",
        "parameters": {"type": "object", "properties": {}},
        "role": None,
        "handler": _my_repair_status,
    },
    {
        "name": "update_repair",
        "description": "更新报修单处理状态（教师/管理员）。状态：pending/processing/done/closed。",
        "parameters": {
            "type": "object",
            "properties": {
                "repair_id": {"type": "integer", "description": "报修单编号"},
                "status": {"type": "string", "description": "新状态"},
                "note": {"type": "string", "description": "处理说明"},
            },
            "required": ["repair_id", "status"],
        },
        "roles": ["teacher", "admin"],
        "handler": _update_repair,
    },
    {
        "name": "list_venues",
        "description": "列出北校区可预约场地（报告厅、研讨室、体育馆、足球场）及开放时间。",
        "parameters": {"type": "object", "properties": {}},
        "role": None,
        "handler": _list_venues,
    },
    {
        "name": "book_venue",
        "description": "预约场地。参数：venue_id 场地编号、book_date 日期 YYYY-MM-DD、start_time/end_time HH:MM、purpose 用途。",
        "parameters": {
            "type": "object",
            "properties": {
                "venue_id": {"type": "integer", "description": "场地编号"},
                "book_date": {"type": "string", "description": "预约日期 YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "开始时间 HH:MM"},
                "end_time": {"type": "string", "description": "结束时间 HH:MM"},
                "purpose": {"type": "string", "description": "预约用途"},
            },
            "required": ["venue_id", "book_date", "start_time", "end_time"],
        },
        "role": None,
        "handler": _book_venue,
    },
    {
        "name": "my_bookings",
        "description": "查询当前用户的场地预约及审批状态。",
        "parameters": {"type": "object", "properties": {}},
        "role": None,
        "handler": _my_bookings,
    },
    {
        "name": "approve_booking",
        "description": "审批场地预约（教师/管理员）。参数：booking_id、approve。",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "integer", "description": "预约编号"},
                "approve": {"type": "boolean", "description": "true批准/false驳回"},
            },
            "required": ["booking_id"],
        },
        "roles": ["teacher", "admin"],
        "handler": _approve_booking,
    },
    {
        "name": "ask_human",
        "description": "当无法回答用户问题或用户要求人工办理时，创建人工服务工单，由管理员后台处理并回复。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户的问题或诉求"},
            },
            "required": ["question"],
        },
        "role": None,
        "handler": _ask_human,
    },
    {
        "name": "web_search",
        "description": "备用联网搜索（内置兜底，使用必应）。当外部 MCP 浏览器工具不可用时，用于搜索公开网页信息。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
        "role": None,
        "handler": _web_search_fallback,
    },
    {
        "name": "campus_route",
        "description": "北校区校内步行路线：给出从校园内 A 地点到 B 地点的步行距离与路线（如：从三教到图书馆怎么走）。参数 from/to 传地点名称或俗称（如 三教、图书馆、六公寓、一食堂、校医院、体育馆、田径场、校门、北府生活广场等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "出发地，如 三教 / 六公寓 / 校门"},
                "to": {"type": "string", "description": "目的地，如 图书馆 / 一食堂 / 体育馆"},
            },
            "required": ["from", "to"],
        },
        "role": None,
        "handler": _campus_route,
    },
    {
        "name": "query_student_leave",
        "description": "教师/管理员查询某个学生的请假情况（有无请假、起止日期、事由、审批状态）。参数 student 传学生姓名、学号或账号。",
        "parameters": {
            "type": "object",
            "properties": {"student": {"type": "string", "description": "学生姓名或学号，如 李明 / 2023010101"}},
            "required": ["student"],
        },
        "roles": ["teacher", "admin"],
        "handler": _query_student_leave,
    },
    {
        "name": "list_pending_applications",
        "description": "教师/管理员查看所有待审批的请假、场地预约和待处理报修清单。",
        "parameters": {"type": "object", "properties": {}},
        "roles": ["teacher", "admin"],
        "handler": _list_pending_applications,
    },
    {
        "name": "my_ticket_status",
        "description": "查询我提交的人工服务工单，以及管理员是否已回复（含回复内容）。适合问：人工有回复了吗/我的工单怎么处理的。",
        "parameters": {"type": "object", "properties": {}},
        "role": None,
        "handler": _my_ticket_status,
    },
    {
        "name": "reply_ticket",
        "description": "管理员在对话中直接回复人工工单。参数：ticket_id 工单编号、reply 回复内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "工单编号"},
                "reply": {"type": "string", "description": "回复内容"},
            },
            "required": ["ticket_id", "reply"],
        },
        "roles": ["admin"],
        "handler": _reply_ticket,
    },
]

LOCAL_TOOL_NAMES = {t["name"] for t in LOCAL_TOOLS}


def get_local_tools(user: dict) -> list[dict]:
    tools = []
    for t in LOCAL_TOOLS:
        roles = t.get("roles") or ([t["role"]] if t.get("role") else None)
        if roles and user["role"] not in roles:
            continue
        tools.append({"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}})
    return tools


async def execute_local_tool(user: dict, name: str, args: dict) -> str:
    for t in LOCAL_TOOLS:
        if t["name"] == name:
            roles = t.get("roles") or ([t["role"]] if t.get("role") else None)
            if roles and user["role"] not in roles:
                return "没有权限调用该工具。"
            return await t["handler"](user, args)
    raise KeyError(f"未找到本地工具 {name}")
