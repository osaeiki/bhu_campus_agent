"""自检测试：混合检索（BM25 + 向量）与主动推送消息。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tests\\test_rag_and_notify.py

测试使用临时数据库，不会影响 data/campus.db。
"""
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

_tmp = tempfile.mkdtemp(prefix="bhu_test_")
os.environ["DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ.setdefault("KB_FILES_DIR", str(BASE_DIR / "seed" / "kb_files"))

from app import knowledge, notify  # noqa: E402
from app.database import get_conn, init_db, seed_if_empty, seed_knowledge_if_empty  # noqa: E402
from app.tools_local import execute_local_tool  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  -> {detail}" if detail else ""))


def test_ingest_and_hybrid_search():
    print("\n== 1. 知识文件导入 + 混合检索 ==")
    stats = knowledge.sync_all()
    print("  导入统计:", stats)
    chunks = knowledge.rebuild_index()
    info = knowledge.index_info()
    print("  索引信息:", info)

    check("导入文件数 > 0", stats["files"] > 0, str(stats["files"]))
    check("存在文档/PDF/表格三种知识块", True)

    conn = get_conn()
    try:
        rows = conn.execute("SELECT doc_type, COUNT(*) AS c FROM knowledge_docs GROUP BY doc_type").fetchall()
        by_type = {(r["doc_type"] or "text"): r["c"] for r in rows}
    finally:
        conn.close()
    print("  知识库类型分布:", by_type)
    check("文档类型 text 有数据", by_type.get("text", 0) > 0, str(by_type.get("text", 0)))
    check("文档类型 pdf 有数据", by_type.get("pdf", 0) > 0, str(by_type.get("pdf", 0)))
    check("文档类型 table 有数据", by_type.get("table", 0) > 0, str(by_type.get("table", 0)))
    check("向量索引已建立", info["vectors"] == chunks and chunks > 0, f"{info['backend']} dim={info['dim']}")

    # 覆盖三种文件类型的检索用例：(提问, 期望出现在 Top-1 里的关键词们（命中任一即可）, 期望类型)
    # 说明：关键词写成"或"的关系，因为同一个问题在知识库里可能有多份同样正确的来源，
    # 例如维修电话既在《后勤维修与报修流程》正文里，也在《服务设施一览表·服务电话》表格里。
    cases = [
        ("第一学期什么时候开学，寒假怎么放", "校历", "pdf"),
        ("第二食堂三楼窗口的参考价格是多少", "食堂窗口", "table"),
        ("羽毛球场地开放时间和预约方式", "场馆开放时间", "table"),
        ("北校区水暖班维修电话是多少", ("水暖班", "服务电话"), None),
        ("我今天发烧了下午有课怎么办", ("请假", "校医院", "就医"), None),
        ("图书馆本科生一次能借几本书", "借", None),
        ("宿舍能不能用热得快和电饭锅", "违规电器", None),
        ("体育馆羽毛球场地怎么预约", "羽毛球", None),
        ("从吉林站火车站怎么到北校区", "104", None),
    ]
    for q, expect, want_type in cases:
        res = knowledge.search(q, top_k=3)
        top = res[0] if res else {}
        hay = top.get("title", "") + top.get("chunk", "")
        expects = (expect,) if isinstance(expect, str) else tuple(expect)
        hit = any(e in hay for e in expects)
        detail = f"Top1={top.get('title', '-')} [{top.get('doc_type')}] bm25={top.get('bm25')} vec={top.get('vector')}"
        if want_type:
            hit = hit and top.get("doc_type") == want_type
        check(f"检索「{q}」", bool(hit), detail)

    # 口语提问：允许最相关的“文本块 + 表格块”同时出现在 Top-3（表格块命中即可）
    res = knowledge.search("第二食堂三楼有什么好吃的", top_k=3)
    check(
        "口语提问能同时召回表格块",
        any(r.get("doc_type") == "table" for r in res),
        " | ".join(f"[{r.get('doc_type')}]{r.get('title', '')[:18]}" for r in res),
    )


def test_bm25_vs_vector():
    print("\n== 2. 两路召回对比（BM25 / 向量 / 融合） ==")
    q = "生病了怎么请假"
    hybrid = knowledge.search_detail(q, top_k=3, mode="hybrid")
    bm25 = knowledge.search_detail(q, top_k=3, mode="bm25")
    vector = knowledge.search_detail(q, top_k=3, mode="vector")
    check("混合检索有结果", len(hybrid["fused"]) > 0, str(len(hybrid["fused"])))
    check("BM25 单路有结果", len(bm25["fused"]) > 0, str(len(bm25["fused"])))
    check("向量单路有结果", len(vector["fused"]) > 0, str(len(vector["fused"])))
    print("  查询改写:", hybrid["expanded_tokens"][:12])
    for r in hybrid["fused"]:
        print(f"    {r['score']:.3f} [{r['doc_type']}] {r['title'][:36]} (bm25={r['bm25']}, vec={r['vector']})")


def test_push_notifications():
    print("\n== 3. 主动推送：请假审批 + 人工工单 ==")

    def user(uid):
        conn = get_conn()
        try:
            return dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        finally:
            conn.close()

    student = user(3)   # seed: student 李明
    teacher = user(2)   # seed: teacher 王明
    admin = user(1)

    # 1) 学生提交请假 -> 应给教师/管理员推送待办
    import asyncio

    msg = asyncio.run(execute_local_tool(
        student, "submit_leave",
        {"leave_type": "病假", "reason": "发烧", "start_date": "2026-09-28", "end_date": "2026-09-29"},
    ))
    print("  学生提交:", msg)
    conn = get_conn()
    try:
        leave_id = conn.execute("SELECT MAX(id) AS m FROM leaves WHERE user_id=?", (student["id"],)).fetchone()["m"]
    finally:
        conn.close()
    check("教师收到待审批推送", notify.unread_count(teacher["id"]) > 0, f"teacher 未读 {notify.unread_count(teacher['id'])}")
    check("学生收到已提交确认", notify.unread_count(student["id"]) > 0)

    # 2) 教师批假 -> 应给学生主动推送审批结果
    before = notify.unread_count(student["id"])
    out = asyncio.run(execute_local_tool(teacher, "approve_leave", {"leave_id": leave_id, "approve": True, "note": "好好休息"}))
    print("  教师审批:", out)
    items = notify.list_for_user(student["id"], limit=5)
    after = notify.unread_count(student["id"])
    check("批假后学生未读消息增加", after > before, f"{before} -> {after}")
    check(
        "学生收到「请假审批结果」推送",
        any(i["type"] == "leave_result" and "批准" in i["title"] for i in items),
        items[0]["title"] if items else "-",
    )

    # 3) 学生转人工 -> 管理员收到推送；管理员回复 -> 学生收到推送
    asyncio.run(execute_local_tool(student, "ask_human", {"question": "北校区食堂有哪些好吃的店"}))
    conn = get_conn()
    try:
        tid = conn.execute("SELECT MAX(id) AS m FROM tickets").fetchone()["m"]
    finally:
        conn.close()
    check("管理员收到新工单推送", notify.unread_count(admin["id"]) > 0, f"admin 未读 {notify.unread_count(admin['id'])}")

    before = notify.unread_count(student["id"])
    asyncio.run(execute_local_tool(admin, "reply_ticket", {"ticket_id": tid, "reply": "推荐第二食堂三层的烤盘饭"}))
    items = notify.list_for_user(student["id"], limit=5)
    check("工单回复后学生收到推送", notify.unread_count(student["id"]) > before)
    check(
        "学生收到「人工服务回复」推送",
        any(i["type"] == "ticket_reply" and "烤盘饭" in (i["content"] or "") for i in items),
    )

    # 4) 已读流程
    nid = items[0]["id"]
    notify.mark_read(student["id"], nid)
    read_row = [i for i in notify.list_for_user(student["id"], limit=5) if i["id"] == nid][0]
    check("标记已读生效", bool(read_row["read_at"]))
    n = notify.mark_all_read(student["id"])
    check("全部已读生效", notify.unread_count(student["id"]) == 0, f"本次已读 {n} 条")


def main() -> int:
    init_db()
    seed_if_empty()
    seed_knowledge_if_empty()
    test_ingest_and_hybrid_search()
    test_bm25_vs_vector()
    test_push_notifications()
    print(f"\n===== 结果：{len(PASS)} 通过，{len(FAIL)} 失败 =====")
    if FAIL:
        print("失败项：", "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
