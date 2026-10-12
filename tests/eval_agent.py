"""北华校园助手评测脚本。

用法：
  .venv\\Scripts\\python tests\\eval_agent.py          # 知识库检索 + 本地工具离线评测
  .venv\\Scripts\\python tests\\eval_agent.py --server  # 附加 API/对话冒烟测试（需服务已启动）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

KB_QUESTIONS = [
    ("北校区地址在哪？", ["概况"]),
    ("北校区有几个食堂？", ["食堂"]),
    ("第三教学楼有什么特点？", ["教学楼"]),
    ("图书馆在哪里，能干什么？", ["图书馆"]),
    ("寝室几点关门？", ["宿舍"]),
    ("怎么去东校区？", ["交通"]),
    ("校园卡在哪里充值？", ["食堂", "校园卡"]),
    ("校医院在哪里？", ["生活服务"]),
    ("请假流程是什么？", ["请假"]),
    ("校训是什么？", ["校训"]),
]

INTENT_CASES = [
    ("你好，你是谁", "small_talk"),
    ("北校区有几个食堂", "kb_qa"),
    ("帮我查课表", "query_course"),
    ("我的成绩多少", "query_score"),
    ("帮我请9月5号一天假", "leave"),
    ("灯管坏了报修一下", "repair"),
    ("预约研讨室", "venue_booking"),
    ("从三教到图书馆怎么走", "campus_route"),
    ("从北校区去吉林站坐公交", "maps_navigation"),
    ("吉林市天气怎么样", "weather"),
    ("附近有什么好吃的", "poi_search"),
    ("搜一下北华大学2026新生报到时间", "web_search"),
    ("查一下李明有没有请假", "teacher_manage"),
    ("帮我转人工", "human_service"),
    ("帮我查课表顺便看天气", "broad"),
]


def eval_kb() -> dict:
    from app import knowledge

    n = knowledge.rebuild_index()
    hits = 0
    details = []
    for q, cats in KB_QUESTIONS:
        results = knowledge.search(q, top_k=3)
        top_hay = []
        for r in results:
            top_hay.append(r["category"])
            top_hay.append(r["title"])
        ok = any(any(c in h for c in cats) for h in top_hay)
        hits += ok
        details.append((q, ok, [f"{r['category']}/{r['title']}" for r in results[:3]]))
    return {"chunks": n, "hit": hits, "total": len(KB_QUESTIONS), "details": details}


def eval_api_smoke() -> dict:
    import httpx

    base = "http://127.0.0.1:8000"
    out = {}
    r = httpx.post(f"{base}/api/login", json={"username": "admin", "password": "admin123"}, timeout=15)
    out["login_admin"] = r.status_code == 200
    token = r.json().get("token", "") if out["login_admin"] else ""
    out["stats"] = httpx.get(f"{base}/api/admin/stats", headers={"Authorization": f"Bearer {token}"}, timeout=15).status_code == 200
    out["mcp"] = httpx.get(f"{base}/api/admin/mcp", headers={"Authorization": f"Bearer {token}"}, timeout=15).status_code == 200
    return out


def eval_intent() -> dict:
    from app.intent import classify_rule

    ok = 0
    details = []
    for text, want in INTENT_CASES:
        got, conf = classify_rule(text)
        hit = got == want
        ok += hit
        details.append((text, want, got, conf, hit))
    return {"hit": ok, "total": len(INTENT_CASES), "details": details}


def main():
    print("=" * 60)
    print("知识库检索评测（BM25 + jieba）")
    kb = eval_kb()
    print(f"索引分块：{kb['chunks']}，命中：{kb['hit']}/{kb['total']}")
    for q, ok, top in kb["details"]:
        print(("✅" if ok else "❌"), q, "->", top)
    print("=" * 60)
    print("意图分析评测（规则层）")
    it = eval_intent()
    print(f"命中：{it['hit']}/{it['total']}")
    for text, want, got, conf, ok in it["details"]:
        print(("✅" if ok else "❌"), text, "->", got, f"({conf})", "" if ok else f"期望:{want}")
    print("=" * 60)
    if "--server" in sys.argv:
        print("API 冒烟测试（需服务已启动）")
        smoke = eval_api_smoke()
        for k, v in smoke.items():
            print(("✅" if v else "❌"), k)
    sys.exit(0 if kb["hit"] >= kb["total"] * 0.8 else 1)


if __name__ == "__main__":
    main()
