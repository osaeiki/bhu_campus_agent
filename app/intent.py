"""显式意图分析层。

策略：关键词/正则规则优先（快、零成本、可解释）；规则未命中时调用 DeepSeek
做一次轻量分类（max_tokens 很小）。返回 (意图, 置信度, 来源)。

意图确定后，Agent 只暴露与该意图相关的工具，降低 40+ 工具对模型的干扰。
"""
import json
import re

import httpx

from . import config

INTENT_LABEL = {
    "small_talk": "闲聊/问候",
    "kb_qa": "校园知识问答",
    "query_course": "查课表",
    "query_score": "查成绩",
    "leave": "请假",
    "repair": "报修",
    "venue_booking": "场地预约",
    "campus_route": "校内路线",
    "maps_navigation": "真实导航/公交",
    "weather": "天气",
    "poi_search": "地点/周边搜索",
    "web_search": "联网搜索",
    "teacher_manage": "教师管理",
    "human_service": "转人工",
    "broad": "综合",
}

INTENT_DESC = {
    "small_talk": "闲聊、问候、自我介绍、询问能力",
    "kb_qa": "询问北华大学北校区校园信息（地点、设施、制度、流程等）",
    "query_course": "查询课表、今天上什么课",
    "query_score": "查询成绩、绩点、平均分",
    "leave": "请假申请、查询请假状态、审批请假",
    "repair": "报修、查询维修进度、处理报修",
    "venue_booking": "场地预约、查看场地、审批预约",
    "campus_route": "北校区校内从A地到B地的步行路线",
    "maps_navigation": "真实地图出行：公交/地铁/打车/驾车/骑行，或去校外地点/其他校区",
    "weather": "查询天气、气温、是否下雨",
    "poi_search": "查找附近/周边的地点、店铺、设施",
    "web_search": "需要联网搜索最新或网上公开信息（公告、新闻、报名时间等）",
    "teacher_manage": "教师查询学生请假、查看待办审批清单",
    "human_service": "要求转人工服务",
    "broad": "包含多个意图或无法确定，允许使用全部工具",
}

# 校外/交通词出现时优先“真实导航”，校内地点词+走法才是“校内路线”
_OUTDOOR = ["吉林站", "火车站", "高铁", "机场", "汽车站", "万达", "雾凇", "江城广场", "财富", "商场", "超市", "医院", "东校区", "南校区", "师范分院", "公交", "地铁", "打车", "驾车", "开车", "骑行", "自行车"]
_CAMPUS = ["三教", "二教", "一教", "教学楼", "图书馆", "食堂", "宿舍", "公寓", "田径场", "操场", "体育馆", "校医院", "风华碑", "风华广场", "综合楼", "办公", "北府", "浴池", "大学生服务中心", "服务中心", "校门", "正门"]
_ROUTE_VERB = ["怎么走", "怎么去", "多远", "路线", "指路", "咋走", "路线图", "路怎么"]


def _has(t: str, kws: list[str]) -> bool:
    return any(k in t for k in kws)


def classify_rule(text: str):
    """规则优先：命中返回 (意图, 置信度)，未命中返回 (None, 0)。"""
    t = (text or "").strip()
    if not t:
        return "small_talk", 0.95

    # 1. 转人工
    if _has(t, ["转人工", "人工服务", "找人工", "人工客服", "投诉", "联系老师", "找老师", "工单", "人工回复", "有没有回复", "回复了吗", "人工处理"]):
        return "human_service", 0.96

    # 2. 教师管理（查学生/请假名单/待办）——先于普通请假判断
    if _has(t, ["请假名单", "谁请假", "谁没来", "出勤", "请假情况", "待办", "待审批"]) or re.search(
        r"查(?!我)[^，。]{0,12}请假", t
    ):
        if _has(t, ["学生", "同学", "李明", "张雪", "名单", "待办", "待审批"]) or "我的" not in t[:6]:
            return "teacher_manage", 0.9

    # 3. 多意图检测：同一句出现 2 类以上业务词 → 综合（放行全部工具）
    groups = {
        "query_course": ["课", "上课", "节次"],
        "query_score": ["成绩", "分数", "绩点"],
        "leave": ["请假", "事假", "病假"],
        "repair": ["报修", "维修", "坏了"],
        "venue_booking": ["预约", "场地", "订"],
        "weather": ["天气", "气温", "温度"],
        "maps_navigation": _OUTDOOR,
        "web_search": ["搜索", "最新", "官网"],
        "poi_search": ["附近", "周边"],
    }
    hit = [k for k, kws in groups.items() if _has(t, kws)]
    # 校内路线只有当句子里确实有校内地点时才计为一路意图（避免“去吉林站怎么走”被算成两个意图）
    if _has(t, _CAMPUS) and _has(t, _ROUTE_VERB):
        hit.append("campus_route")
    if len(set(hit)) >= 2:
        return "broad", 0.7

    # 4. 高优意图（先于知识问答等兜底）
    if _has(t, ["课表", "课程表", "上什么课", "今天上什么", "明天上什么", "这节课", "下节课"]) or re.search(
        r"查[^。]{0,8}(课|课程|节)", t
    ):
        return "query_course", 0.92
    if _has(t, ["成绩", "绩点", "平均分", "分数", "考了", "考得", "挂科"]):
        return "query_score", 0.92
    if re.search(r"请假|请.{0,10}假|事假|病假|公假|销假|休学申请", t):
        return "leave", 0.92
    if _has(t, ["报修", "维修", "坏了", "漏水", "门锁", "插座", "灯管", "灯坏", "水管", "报修单"]):
        return "repair", 0.92
    if _has(t, ["预约", "订场", "借用", "订个", "场地", "研讨室", "报告厅", "羽毛球", "足球场", "篮球场"]):
        return "venue_booking", 0.9
    if _has(t, ["天气", "气温", "温度", "下雨", "下雪", "降雨", "降温", "天气预报", "几度", "风力"]):
        return "weather", 0.94
    if _has(t, _OUTDOOR) and (_has(t, ["去", "到", "走", "路线", "导航", "怎么", "坐"]) or len(t) <= 12):
        return "maps_navigation", 0.9
    if _has(t, _CAMPUS) and _has(t, _ROUTE_VERB):
        return "campus_route", 0.93
    if _has(t, ["附近", "周边", "哪里有", "哪里能", "去哪吃", "哪有", "找一下", "查一下附近"]):
        return "poi_search", 0.9
    if _has(t, ["最新", "搜索", "网上", "官网", "新闻", "公告", "2026", "分数线", "报到时间", "报名时间", "录取", "通知"]):
        return "web_search", 0.86
    if _has(t, ["你好", "您好", "你是谁", "介绍一下你", "你能干什么", "会什么", "有什么功能", "帮助", "hi", "hello", "在吗"]):
        return "small_talk", 0.92

    # 4. 校内知识问答（含地点/设施/时间/流程类问句）
    if _has(t, _CAMPUS) or _has(t, ["北校区", "北华大学", "几点", "几点开门", "开放时间", "地址", "邮编", "流程", "规定", "校历", "学费", "社团", "快递", "宽带", "校园卡", "什么时候", "在哪", "在哪里", "哪里"]):
        return "kb_qa", 0.78

    return None, 0.0


async def _llm_classify(text: str, history: str = ""):
    """DeepSeek 轻量分类兜底。"""
    lines = [f"{k}:{v}" for k, v in INTENT_DESC.items()]
    sys_prompt = (
        "你是意图分类器。只从下面列表里选一个最匹配的意图，输出严格 JSON："
        '{"intent":"意图名","confidence":0到1的数字}。不要输出其他内容。\n' + "\n".join(lines)
    )
    user_prompt = (f"最近对话：\n{history[-400:]}\n\n" if history else "") + f"用户最新问题：{text}"
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": 40,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{config.DEEPSEEK_BASE_URL}/chat/completions", json=payload, headers=headers
            )
        if resp.status_code != 200:
            return "broad", 0.5
        content = resp.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return "broad", 0.5
        data = json.loads(m.group(0))
        intent = data.get("intent", "broad")
        if intent not in INTENT_LABEL:
            intent = "broad"
        return intent, float(data.get("confidence", 0.6))
    except Exception:
        return "broad", 0.5


async def classify_intent(text: str, history: str = ""):
    """入口：规则优先，未命中走 LLM。返回 (intent, confidence, source)。"""
    rule, conf = classify_rule(text)
    if rule:
        return rule, conf, "rule"
    intent, conf = await _llm_classify(text, history)
    return intent, conf, "llm"


def filter_tools(intent: str, tools: list[dict]) -> list[dict]:
    """按意图过滤 OpenAI 函数格式的工具列表；保留人工兜底。"""
    if intent == "broad":
        return tools

    def name_of(t: dict) -> str:
        return t.get("function", {}).get("name", "")

    allow: dict[str, set] = {
        "small_talk": set(),
        "kb_qa": {"search_knowledge"},
        "query_course": {"query_course"},
        "query_score": {"query_score"},
        "leave": {"submit_leave", "my_leave_status", "approve_leave"},
        "repair": {"submit_repair", "my_repair_status", "update_repair"},
        "venue_booking": {"list_venues", "book_venue", "my_bookings", "approve_booking"},
        "campus_route": {"campus_route", "search_knowledge"},
        "maps_navigation": {
            "amap_maps_geo",
            "amap_maps_regeocode",
            "amap_maps_direction_walking",
            "amap_maps_direction_driving",
            "amap_maps_direction_transit_integrated",
            "amap_maps_direction_bicycling",
            "amap_maps_distance",
            "amap_maps_schema_navi",
            "amap_maps_schema_take_taxi",
        },
        "weather": {"amap_maps_weather"},
        "poi_search": {"amap_maps_text_search", "amap_maps_around_search", "amap_maps_search_detail", "amap_maps_geo"},
        "web_search": {"web_search"},
        "teacher_manage": {
            "query_student_leave",
            "list_pending_applications",
            "approve_leave",
            "approve_booking",
            "update_repair",
            "query_course",
        },
        "human_service": {"ask_human", "my_ticket_status", "reply_ticket"},
    }
    exact = allow.get(intent, set())
    result = []
    for t in tools:
        n = name_of(t)
        # search_knowledge / ask_human 永远保留：
        #   - 知识库检索是“有依据回答”的底线，任何意图都可能需要查校内资料；
        #   - 意图规则一旦把检索工具过滤掉，模型就只能凭记忆回答，容易编造电话、时间等信息。
        if n in exact or n in ("ask_human", "search_knowledge"):
            result.append(t)
            continue
        if intent == "web_search" and n.startswith("browser_"):
            result.append(t)
    return result
