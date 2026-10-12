"""北校区校内步行路线图：地点图 + 最短路径。

距离为按校园公开布局估算的近似值（本地演示用），真实导航建议后续接入高德/百度地图 MCP。
"""
import re
from heapq import heappop, heappush

# 地点与常见别名
PLACES = {
    "校门": ["正门", "北门", "学校大门", "校门口", "大门"],
    "第一教学楼": ["一教", "1教", "第一教学", "一教学楼"],
    "第二教学楼": ["二教", "2教", "第二教学", "二教学楼"],
    "第三教学楼": ["三教", "3教", "第三教学", "三教学楼", "变形金刚"],
    "图书馆": ["北校区图书馆", "学校图书馆"],
    "风华广场": ["风华碑", "风华", "广场"],
    "综合办公楼": ["综合楼", "办公楼", "综合办公"],
    "校医院": ["医院", "校医", "医务室"],
    "田径场": ["操场", "田径", "足球场"],
    "体育馆": ["体院馆", "球馆", "体育馆内"],
    "大学生服务中心": ["服务中心", "学生服务中心"],
    "一食堂": ["第一食堂", "一食"],
    "二食堂": ["第二食堂", "二食"],
    "三食堂": ["第三食堂", "三食"],
    "北府生活广场": ["北府广场", "北府", "生活广场"],
    "浴池": ["澡堂", "洗浴"],
    "一公寓": ["1公寓", "一舍"],
    "二公寓": ["2公寓", "二舍"],
    "三公寓": ["3公寓", "三舍"],
    "四公寓": ["4公寓", "四舍"],
    "五公寓": ["5公寓", "五舍"],
    "六公寓": ["6公寓", "六舍"],
    "七公寓": ["7公寓", "七舍"],
    "八公寓": ["8公寓", "八舍"],
    "九公寓": ["9公寓", "九舍"],
}

# (地点A, 地点B, 距离米) —— 按北校区公开布局估算
EDGES = [
    ("校门", "第二教学楼", 80),
    ("校门", "风华广场", 200),
    ("校门", "北府生活广场", 350),
    ("风华广场", "第三教学楼", 150),
    ("风华广场", "第一教学楼", 160),
    ("风华广场", "图书馆", 120),
    ("风华广场", "综合办公楼", 200),
    ("第三教学楼", "图书馆", 100),
    ("图书馆", "综合办公楼", 120),
    ("第一教学楼", "田径场", 150),
    ("田径场", "体育馆", 120),
    ("综合办公楼", "校医院", 100),
    ("风华广场", "六公寓", 300),
    ("风华广场", "浴池", 260),
    ("一公寓", "二公寓", 70),
    ("二公寓", "三公寓", 80),
    ("三公寓", "四公寓", 70),
    ("四公寓", "六公寓", 80),
    ("六公寓", "五公寓", 70),
    ("五公寓", "七公寓", 80),
    ("七公寓", "八公寓", 70),
    ("八公寓", "九公寓", 80),
    ("九公寓", "大学生服务中心", 10),
    ("九公寓", "浴池", 60),
    ("六公寓", "一食堂", 80),
    ("六公寓", "二食堂", 90),
    ("六公寓", "三食堂", 70),
    ("一食堂", "二食堂", 50),
    ("二食堂", "三食堂", 40),
    ("北府生活广场", "三食堂", 150),
]

_GRAPH: dict[str, list[tuple[str, int]]] = {}
_ALIAS: dict[str, str] = {}


def _build():
    for a, b, d in EDGES:
        _GRAPH.setdefault(a, []).append((b, d))
        _GRAPH.setdefault(b, []).append((a, d))
    for name, aliases in PLACES.items():
        _ALIAS[name] = name
        for al in aliases:
            _ALIAS[al] = name


_build()


def find_place(text: str) -> str | None:
    """从用户话里识别地点（含别名与模糊匹配）。"""
    t = text.strip().lower()
    t = t.replace("北校区", "").replace("北校", "").replace("校区", "")
    if t in _ALIAS:
        return _ALIAS[t]
    for key in _ALIAS:
        if key and key in t:
            return _ALIAS[key]
    # 反向：地点名包含输入（如 “公寓” 泛指）
    if "公寓" in t and len(t) <= 3:
        return "六公寓"
    return None


def route(from_text: str, to_text: str) -> str:
    a = find_place(from_text)
    b = find_place(to_text)
    if not a or not b:
        known = "、".join(PLACES.keys())
        return f"没认出地点。校内可识别地点：{known}。"
    if a == b:
        return f"出发地和目的地都是「{a}」，不需要路线。"
    # Dijkstra
    import math

    dist = {n: math.inf for n in _GRAPH}
    prev: dict[str, str] = {}
    dist[a] = 0
    pq = [(0, a)]
    while pq:
        d, n = heappop(pq)
        if d > dist[n]:
            continue
        if n == b:
            break
        for m, w in _GRAPH.get(n, []):
            nd = d + w
            if nd < dist[m]:
                dist[m] = nd
                prev[m] = n
                heappush(pq, (nd, m))
    if math.isinf(dist.get(b, math.inf)):
        return f"暂时无法规划从「{a}」到「{b}」的路线（图数据不完整）。"
    path = [b]
    cur = b
    while cur != a:
        cur = prev[cur]
        path.append(cur)
    path.reverse()
    minutes = max(1, round(dist[b] / 80))  # 按约 80 米/分钟
    lines = [
        f"从「{a}」到「{b}」约 {dist[b]} 米，步行约 {minutes} 分钟：",
        f"1. 从 {a} 出发",
    ]
    for i in range(1, len(path)):
        lines.append(f"{i + 1}. 沿校园道路步行约 {_seg_len(path[i - 1], path[i])} 米，到达 {path[i]}")
    lines.append("提示：校内各建筑间步行即可，以上距离为估算值。")
    return "\n".join(lines)


def _seg_len(x: str, y: str) -> int:
    for a, b, d in EDGES:
        if {a, b} == {x, y}:
            return d
    return 0
