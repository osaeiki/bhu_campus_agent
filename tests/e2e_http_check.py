# -*- coding: utf-8 -*-
"""端到端 HTTP 验证：主动推送 + RAG/BM25 混合检索。

前提：本机已启动服务（默认 http://127.0.0.1:8000）。

用法：
    .\.venv\Scripts\python.exe tests\e2e_http_check.py
    .\.venv\Scripts\python.exe tests\e2e_http_check.py http://127.0.0.1:8000

脚本不会调用大模型，只走 HTTP 接口 + 直接写库造数据，
因此几秒内就能跑完，适合改动代码或部署之后做一次回归自检。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
PASSED = 0
FAILED = 0


def check(name, ok, extra=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


def call(method, path, token=None, body=None, raw=False):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, (text if raw else json.loads(text))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, {"raw": text}


def login(username, password):
    status, data = call("POST", "/api/login", body={"username": username, "password": password})
    if status != 200:
        raise SystemExit(f"登录失败 {username}: {status} {data}")
    return data.get("token") or data.get("access_token")


def main():
    print(f"== 目标服务：{BASE} ==")

    print("\n== 1. 登录三种角色 ==")
    stu = login("student", "student123")
    tea = login("teacher", "teacher123")
    adm = login("admin", "admin123")
    check("学生/教师/管理员登录成功", all([stu, tea, adm]))

    print("\n== 2. 造一条待审批请假（模拟学生在对话里发起） ==")
    sys.path.insert(0, ".")
    from app import database
    conn = database.get_conn()
    try:
        conn.execute(
            "INSERT INTO leaves(user_id, leave_type, start_date, end_date, reason, status) "
            "VALUES(3, '病假', '2026-09-28', '2026-09-29', '发烧，需要休息', 'pending')"
        )
        conn.commit()
        leave_id = conn.execute("SELECT MAX(id) AS i FROM leaves").fetchone()["i"]
    finally:
        conn.close()
    check("请假单已写入", bool(leave_id), f"#{leave_id}")

    status, data = call("GET", "/api/notifications?unread=1", token=stu)
    before = len(data.get("items", [])) if status == 200 else -1
    check("学生未读通知接口可用", status == 200, f"未读 {before} 条")

    status, data = call("GET", "/api/admin/applications", token=tea)
    check("教师能看到请假待办", status == 200 and any(
        l.get("id") == leave_id for l in data.get("leaves", [])), f"HTTP {status}")

    print("\n== 3. 教师批假 -> 学生收到主动推送 ==")
    status, data = call("POST", f"/api/admin/leaves/{leave_id}/decision", token=tea,
                        body={"approve": True, "note": "好好休息，注意身体"})
    check("批假接口返回成功", status == 200, f"HTTP {status} {data}")
    status, data = call("GET", "/api/notifications", token=stu)
    items = data.get("items", []) if status == 200 else []
    hit = [n for n in items if n.get("type") == "leave_result"]
    check("学生收到 leave_result 推送", bool(hit), (hit[0]["title"] + "｜" + hit[0]["content"][:40]) if hit else "无")
    status, data = call("GET", "/api/notifications?unread=1", token=stu)
    after = len(data.get("items", [])) if status == 200 else -1
    check("未读数增加", after > before, f"{before} -> {after}")

    print("\n== 4. 学生转人工 -> 管理员回复 -> 学生收到推送 ==")
    conn = database.get_conn()
    try:
        conn.execute(
            "INSERT INTO tickets(user_id, question, status) "
            "VALUES(3, '北校区食堂有哪些好吃的店？', 'open')"
        )
        conn.commit()
        ticket_id = conn.execute("SELECT MAX(id) AS i FROM tickets").fetchone()["i"]
    finally:
        conn.close()
    check("人工工单已写入", bool(ticket_id), f"#{ticket_id}")

    status, data = call("GET", "/api/admin/tickets", token=adm)
    check("管理员能看到工单", status == 200, f"HTTP {status}")

    status, data = call("POST", f"/api/admin/tickets/{ticket_id}/reply", token=adm,
                        body={"reply": "二食堂三楼的烤盘饭和麻辣香锅都挺受欢迎，人均 12-18 元。"})
    check("管理员回复工单成功", status == 200, f"HTTP {status} {data}")
    status, data = call("GET", "/api/notifications", token=stu)
    items = data.get("items", []) if status == 200 else []
    hit = [n for n in items if n.get("type") == "ticket_reply"]
    check("学生收到 ticket_reply 推送", bool(hit), hit[0]["content"][:40] if hit else "无")

    print("\n== 5. 已读流程 ==")
    if hit:
        status, _ = call("POST", f"/api/notifications/{hit[0]['id']}/read", token=stu)
        check("单条已读成功", status == 200, f"HTTP {status}")
    status, data = call("POST", "/api/notifications/read_all", token=stu)
    check("全部已读成功", status == 200, f"HTTP {status}")
    status, data = call("GET", "/api/notifications?unread=1", token=stu)
    check("未读数归零", status == 200 and len(data.get("items", [])) == 0,
          f"剩余 {len(data.get('items', []))}")

    print("\n== 6. 知识库索引与三路检索对比 ==")
    status, idx = call("GET", "/api/admin/knowledge/index", token=adm)
    check("索引信息接口可用", status == 200, json.dumps(idx, ensure_ascii=False)[:120])
    status, lst = call("GET", "/api/admin/knowledge", token=adm)
    by_type = lst.get("by_type", {}) if status == 200 else {}
    check("文档/PDF/表格三类都有", all(by_type.get(k, 0) > 0 for k in ("text", "pdf", "table")),
          json.dumps(by_type, ensure_ascii=False))

    cases = [
        ("北校区水暖班维修电话是多少？", "63041132"),
        ("第二食堂三楼有什么好吃的？", "食堂"),
        ("校历上第一学期什么时候放寒假？", "寒假"),
        ("我生病了怎么请假？", "请假"),
    ]
    for q, want in cases:
        qs = urllib.parse.quote(q)
        per_mode = {}
        for mode in ("bm25", "vector", "hybrid"):
            status, res = call("GET", f"/api/admin/knowledge/search?q={qs}&mode={mode}", token=adm)
            # 接口返回 {query, tokens, bm25:[...], vector:[...], fused:[...]}
            items = res.get("fused" if mode == "hybrid" else mode, []) if status == 200 else []
            per_mode[mode] = items[0]["title"] if items else "无"
        check(f"检索「{q}」三路都有结果",
              all(v != "无" for v in per_mode.values()),
              f"BM25={per_mode['bm25']}｜向量={per_mode['vector']}｜融合={per_mode['hybrid']}")

    print(f"\n===== 端到端：{PASSED} 通过，{FAILED} 失败 =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
