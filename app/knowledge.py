"""校园知识库：文件切分 + 混合检索（BM25 稀疏 + 向量稠密）+ 管理端 CRUD。

检索架构（RAG 的 R 部分）：

    用户问题 ──┬─ jieba 分词 ─→ BM25 打分 ──┐
               └─ jieba 分词 ─→ 向量余弦 ──┴─ 归一化加权融合 ─→ Top-K 知识块 ─→ 交给 LLM 生成

为什么是混合检索：BM25 擅长专有名词、编号、表格字段这类“词面必须对上”的查询
（如“63041110”“第六小节”）；向量检索擅长同义表达（如“生病”“就医”“校医院”）。
两路各召回 20 条，做 min-max 归一化后按权重加权，并给“两路都命中”的块一点加成。

全部离线可跑：BM25 是纯 Python，向量用 LSA（TF-IDF+SVD）或符号哈希，
不需要下载模型、不需要 GPU，符合“本地部署、数据不出校”的要求。
"""
import math
import pathlib
import re
import threading
from datetime import datetime

import jieba

from . import config, embedding
from .database import get_conn

jieba.setLogLevel(20)

_CHUNK_SIZE = 400
_CHUNK_OVERLAP = 60
_PDF_CHUNK_SIZE = 520
_K1 = 1.5
_B = 0.75
# 命中「标题」时额外加分：标题是人工/文件结构给出的强信号（BM25F 的轻量做法）
_TITLE_BOOST = 1.5
# 融合后的标题命中加成（标题里出现查询词，说明这一块大概率就是用户要的那一节）
_FUSED_TITLE_BONUS = 0.35
# 同一个文档最多保留几个块进入最终结果：
# PDF/长文会被切成多块，同一页可能产生 2~3 个块，重复占位会挤掉别的文档，
# 也会给大模型灌重复上下文，所以默认一个文档只留最高分的那一块。
_MAX_CHUNKS_PER_DOC = 1

# 停用词 + 疑问词 / 语气词 / 指代词。
# 为什么要专门处理：BM25 的 IDF 是「越少见越重要」，而「怎么」「哪里」这类词
# 在整个语料里只出现在少数几个块里，IDF 反而很高，会盖过「东校区」这种真正的
# 关键词（实测「怎么去东校区？」会被一个泛泛的问答页压过交通章节）。
_STOPWORDS = set(
    """的 了 是 在 有 和 与 及 或 并 而 也 都 还 又 就 才 很 太 更 最 挺 非常 比较 一下 一些 一个 这个 那个 这些 那些
    我 你 他 她 它 我们 你们 他们 咱们 自己 大家 请问 请 麻烦 帮我 给 我 你 咱 想 要 能 能够 可以 可不可以 能不能
    怎么 怎样 怎么样 如何 咋 为什么 为啥 什么 啥 哪些 哪个 哪里 哪儿 哪种 多少 几 几点 多久 多长 多远 谁 何时
    吗 呢 吧 啊 呀 哦 嗯 哇 唉 哈 嘛 啦 哟 诶 请问一下
    吗 吧 呢 之 其 该 应该 应当 需要 得 会 可能 大概 也许 就是 只是 还有 以及 而且 但是 不过 因为 所以 如果 然后
    这 那 这样 那样 时候 情况 问题 方面 相关 关于 告诉 知道 说说 介绍 一下 现在 今天 明天 昨天 最近
    去 到 从 对 把 被 让 使 于 向 往 给 跟 同 比 等 等等 之类 什么的
    一 二 三 四 五 六 七 八 九 十 个 些 点 条 次 位 名 件 张 本
    几个 干什么 干啥 干嘛 咋办 帮 我查 上 之 哦 诶 请问一下 查一下 说下 说说""".split()
)


def _is_punct(token: str) -> bool:
    """纯标点/符号/空白：不携带任何检索信息，直接丢掉。"""
    return all(not ch.isalnum() for ch in token)

# 口语 -> 书面语 / 俗称 -> 正式名 的查询改写表。
# 用户说“二食堂三楼”，知识库里写的是“第二食堂 / 三层”，纯词面匹配会漏召回，
# 所以在检索前把同义词一起加进查询（不是替换，保留原词，避免改写错误导致漏检）。
_QUERY_ALIAS = {
    "三楼": ["三层"], "二楼": ["二层"], "一楼": ["一层"],
    "四楼": ["四层"], "五楼": ["五层"], "六楼": ["六层"],
    "一食堂": ["第一食堂"], "二食堂": ["第二食堂"], "三食堂": ["第三食堂"],
    "一教": ["第一教学楼"], "二教": ["第二教学楼"], "三教": ["第三教学楼"],
    "寝室": ["宿舍"], "公寓": ["宿舍"], "澡堂": ["浴池", "洗浴"],
    "生病": ["就医", "校医院", "病假"], "发烧": ["病假", "就医", "校医院"],
    "感冒": ["病假", "校医院"], "受伤": ["就医", "校医院"],
    "开学": ["报到", "校历", "上课"], "放假": ["假期", "校历"],
    "多少钱": ["价格", "费用", "收费"], "收费": ["费用", "价格"],
    "号码": ["电话"], "联系方式": ["电话"], "怎么走": ["路线"], "在哪里": ["位置", "地点"],
}

_index_lock = threading.Lock()
_chunks: list[dict] = []          # [{doc_id,title,category,source,source_file,doc_type,chunk,tokens}]
_df: dict[str, int] = {}          # token -> 出现该词的块数
_avgdl = 1.0
_vector_index = None
_meta: dict = {"backend": "-", "dim": 0, "vectors": 0, "chunks": 0}


# ------------------------------------------------------------------ 切分
def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[。！？!?；;])", text)
    return [p.strip() for p in parts if p.strip()]


def _chunk_text(content: str, doc_type: str = "text") -> list[str]:
    """按文档类型切分：表格块原样使用；PDF 用稍大的窗口；文本按标点切。"""
    content = (content or "").strip()
    if not content:
        return []
    # 表格在 ingest.py 里已经按“表头 + N 行”切好了，直接作为独立知识块，避免拆散行
    if doc_type == "table":
        return [content]

    size = _PDF_CHUNK_SIZE if doc_type == "pdf" else _CHUNK_SIZE
    sentences = _split_sentences(content)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) > size and buf:
            chunks.append(buf.strip())
            buf = buf[-_CHUNK_OVERLAP:] + s  # 重叠，保证跨句上下文不被切断
        else:
            buf += s
    if buf.strip():
        chunks.append(buf.strip())
    if not chunks:
        chunks = [content]
    return chunks


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    for t in jieba.lcut((text or "").lower()):
        t = t.strip()
        if not t or _is_punct(t) or t in _STOPWORDS:
            continue
        out.append(t)
    return out


def expand_query_tokens(query: str, tokens: list[str]) -> list[str]:
    """查询改写：把俗称/口语的同义词追加进检索词，提升口语提问的召回率。"""
    extra: list[str] = []
    for key, values in _QUERY_ALIAS.items():
        if key in query:
            extra.extend(values)
    out = list(tokens)
    for t in tokens:
        for v in _QUERY_ALIAS.get(t, ()):  # token 级别的精确同义词
            extra.append(v)
    seen = set()
    for t in extra:
        for piece in _tokenize(t):
            if piece not in seen and piece not in out:
                seen.add(piece)
                out.append(piece)
    return out


# ------------------------------------------------------------------ 建索引
def rebuild_index() -> int:
    global _chunks, _df, _avgdl, _vector_index, _meta
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,title,category,content,source,doc_type,source_file "
            "FROM knowledge_docs WHERE enabled=1 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    chunks: list[dict] = []
    for row in rows:
        doc_type = (row["doc_type"] or "text") if "doc_type" in row.keys() else "text"
        for ci, c in enumerate(_chunk_text(row["content"], doc_type)):
            tokens = _tokenize(c)
            if not tokens:
                continue
            chunks.append(
                {
                    "doc_id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "source": row["source"],
                    "source_file": row["source_file"] if "source_file" in row.keys() else "",
                    "doc_type": doc_type,
                    "chunk_index": ci,
                    "chunk": c,
                    "tokens": tokens,
                    "title_tokens": set(_tokenize(row["title"] or "")),
                }
            )
    df: dict[str, int] = {}
    for ch in chunks:
        for t in set(ch["tokens"]):
            df[t] = df.get(t, 0) + 1
    avgdl = (sum(len(ch["tokens"]) for ch in chunks) / len(chunks)) if chunks else 1.0

    try:
        vidx = embedding.VectorIndex(dim=config.EMBEDDING_DIM, backend=config.EMBEDDING_BACKEND)
        vidx.build([ch["tokens"] for ch in chunks])
    except Exception as e:  # 向量侧失败不能让整个知识库不可用
        print(f"[knowledge] 向量索引构建失败，降级为纯 BM25：{e}")
        vidx = None

    with _index_lock:
        _chunks = chunks
        _df = df
        _avgdl = avgdl
        _vector_index = vidx
        info = vidx.info() if vidx else {"backend": "off", "dim": 0, "vectors": 0}
        _meta = {
            "backend": info.get("backend", "-"),
            "dim": info.get("dim", 0),
            "vectors": info.get("vectors", 0),
            "has_numpy": info.get("has_numpy", False),
            "chunks": len(chunks),
            "docs": len(rows),
        }
    return len(chunks)


def index_info() -> dict:
    with _index_lock:
        return dict(_meta)


# ------------------------------------------------------------------ 检索
def _bm25_rank(q_tokens: list[str], chunks, df, avgdl, top_n: int) -> list[tuple[int, float]]:
    n = len(chunks)
    if n == 0:
        return []
    scores: list[tuple[float, int]] = []
    uniq = set(q_tokens)
    for i, ch in enumerate(chunks):
        tf: dict[str, int] = {}
        for t in ch["tokens"]:
            tf[t] = tf.get(t, 0) + 1
        dl = len(ch["tokens"])
        title_tokens = ch.get("title_tokens") or set()
        score = 0.0
        for t in uniq:
            f = tf.get(t, 0)
            in_title = t in title_tokens
            if f == 0 and not in_title:
                continue
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            if f:
                score += idf * (f * (_K1 + 1)) / (f + _K1 * (1 - _B + _B * dl / avgdl))
            if in_title:
                score += _TITLE_BOOST * idf
        if score > 0:
            scores.append((score, i))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [(i, s) for s, i in scores[:top_n]]


def _minmax(pairs: list[tuple[int, float]]) -> dict[int, float]:
    if not pairs:
        return {}
    vals = [s for _, s in pairs]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return {i: 1.0 for i, _ in pairs}
    return {i: (s - lo) / (hi - lo) for i, s in pairs}


def search_detail(query: str, top_k: int = 4, mode: str = "hybrid") -> dict:
    """完整检索过程（含两路原始得分），用于管理后台的检索测试与调参。"""
    raw_tokens = _tokenize(query)
    q_tokens = expand_query_tokens(query, raw_tokens)
    with _index_lock:
        chunks = list(_chunks)
        df = dict(_df)
        avgdl = _avgdl
        vidx = _vector_index
        meta = dict(_meta)
    if not q_tokens or not chunks:
        return {
            "query": query,
            "tokens": raw_tokens,
            "expanded_tokens": q_tokens,
            "bm25": [],
            "vector": [],
            "fused": [],
            "meta": meta,
        }

    cand = max(config.RETRIEVE_CANDIDATES, top_k)
    bm25_pairs = _bm25_rank(q_tokens, chunks, df, avgdl, cand)
    vec_pairs = vidx.query(q_tokens, cand) if (vidx and mode != "bm25") else []

    def pack(pairs, key):
        out = []
        for rank, (i, s) in enumerate(pairs, 1):
            ch = chunks[i]
            out.append(
                {
                    "rank": rank,
                    "doc_id": ch["doc_id"],
                    "title": ch["title"],
                    "doc_type": ch["doc_type"],
                    "source": ch["source"],
                    key: round(s, 4),
                    "chunk": ch["chunk"][:160],
                }
            )
        return out

    if mode == "bm25":
        fused_pairs = [(i, s) for i, s in bm25_pairs]
    elif mode == "vector":
        fused_pairs = [(i, s) for i, s in vec_pairs]
    else:
        nb, nv = _minmax(bm25_pairs), _minmax(vec_pairs)
        wb, wv = config.HYBRID_BM25_WEIGHT, config.HYBRID_VECTOR_WEIGHT
        merged: dict[int, float] = {}
        for i in set(nb) | set(nv):
            score = wb * nb.get(i, 0.0) + wv * nv.get(i, 0.0)
            if i in nb and i in nv:
                score *= 1.08  # 两路都召回，说明词面与语义都相关，给一点加成
            merged[i] = score
        fused_pairs = sorted(merged.items(), key=lambda x: x[1], reverse=True)

        # 标题命中加成：查询词出现在标题里（如“图书馆”“校医院”“交通”），
        # 说明这一块大概率就是用户想找的那一节，给一点额外权重把它顶上来。
        if _FUSED_TITLE_BONUS:
            boosted: dict[int, float] = {}
            for i, s in fused_pairs:
                title = chunks[i].get("title") or ""
                hit = sum(1 for t in set(q_tokens) if len(t) > 1 and t in title)
                boosted[i] = s * (1.0 + _FUSED_TITLE_BONUS * min(hit, 2)) if hit else s
            fused_pairs = sorted(boosted.items(), key=lambda x: x[1], reverse=True)

    # 同一文档去重：长文/PDF 会被切成多块，重复占位既挤掉别的文档，也给模型灌重复内容
    if _MAX_CHUNKS_PER_DOC > 0:
        kept: list[tuple[int, float]] = []
        counter: dict[int, int] = {}
        for i, s in fused_pairs:
            did = chunks[i]["doc_id"]
            if counter.get(did, 0) >= _MAX_CHUNKS_PER_DOC:
                continue
            counter[did] = counter.get(did, 0) + 1
            kept.append((i, s))
        fused_pairs = kept

    bm25_map = {i: s for i, s in bm25_pairs}
    vec_map = {i: s for i, s in vec_pairs}
    bm25_rank = {i: r for r, (i, _) in enumerate(bm25_pairs, 1)}
    vec_rank = {i: r for r, (i, _) in enumerate(vec_pairs, 1)}
    fused = []
    for i, score in fused_pairs[:top_k]:
        ch = chunks[i]
        fused.append(
            {
                "doc_id": ch["doc_id"],
                "title": ch["title"],
                "category": ch["category"],
                "source": ch["source"],
                "source_file": ch.get("source_file", ""),
                "doc_type": ch["doc_type"],
                "chunk": ch["chunk"],
                "score": round(score, 4),
                "bm25": round(bm25_map.get(i, 0.0), 4),
                "bm25_rank": bm25_rank.get(i),
                "vector": round(vec_map.get(i, 0.0), 4),
                "vector_rank": vec_rank.get(i),
            }
        )
    return {
        "query": query,
        "tokens": raw_tokens[:30],
        "expanded_tokens": q_tokens[:48],
        "bm25": pack(bm25_pairs, "score"),
        "vector": pack(vec_pairs, "score"),
        "fused": fused,
        "meta": meta,
        "mode": mode,
    }


def search(query: str, top_k: int = 4, mode: str = "hybrid") -> list[dict]:
    """混合检索入口，返回 Top-K 知识块（带出处与两支得分）。"""
    return search_detail(query, top_k=top_k, mode=mode)["fused"]


# ------------------------------------------------------------------ 文件导入
def scan_roots() -> list:
    """所有需要扫描的知识文件目录（种子目录 + 后台上传目录）。"""
    roots = []
    for d in (pathlib.Path(config.KB_FILES_DIR), pathlib.Path(config.UPLOAD_DIR)):
        if d.exists():
            roots.append(d)
    return roots


def _exists_in_roots(rel: str) -> bool:
    return any((root / rel).exists() for root in scan_roots())


def sync_files(directory=None) -> dict:
    """扫描目录，按内容哈希增量导入知识库；返回导入统计。"""
    from . import ingest

    directory = directory or config.KB_FILES_DIR
    directory = pathlib.Path(directory)
    files = ingest.scan_directory(directory)
    stats = {"files": len(files), "added": 0, "updated": 0, "removed": 0, "skipped": 0, "chunks": 0}
    conn = get_conn()
    try:
        present = set()
        for path in files:
            try:
                rel = str(path.relative_to(directory))
            except ValueError:
                rel = path.name
            present.add(rel)
            h = ingest.file_hash(path)
            rows = conn.execute(
                "SELECT id,file_hash FROM knowledge_docs WHERE source_file=?", (rel,)
            ).fetchall()
            if rows and all(r["file_hash"] == h for r in rows):
                stats["skipped"] += 1
                continue
            existed = bool(rows)
            if existed:
                conn.execute("DELETE FROM knowledge_docs WHERE source_file=?", (rel,))
            pieces = ingest.load_file(path)
            for p in pieces:
                conn.execute(
                    "INSERT INTO knowledge_docs(title,category,content,source,doc_type,source_file,file_hash) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        p["title"],
                        p.get("category", "文件导入"),
                        p["content"],
                        p.get("source", path.name),
                        p.get("doc_type", "text"),
                        rel,
                        h,
                    ),
                )
                stats["chunks"] += 1
            if existed:
                stats["updated"] += 1
            else:
                stats["added"] += 1
            conn.commit()
        # 磁盘上已删除的文件，同步清理其知识块（在所有扫描根目录里都找不到才算删除）
        if directory == pathlib.Path(config.KB_FILES_DIR):
            for row in conn.execute(
                "SELECT DISTINCT source_file FROM knowledge_docs "
                "WHERE source_file IS NOT NULL AND source_file != ''"
            ).fetchall():
                rel = row["source_file"]
                if rel not in present and not _exists_in_roots(rel):
                    conn.execute("DELETE FROM knowledge_docs WHERE source_file=?", (rel,))
                    stats["removed"] += 1
            conn.commit()
    finally:
        conn.close()
    if stats["chunks"] or stats["removed"]:
        rebuild_index()
    return stats


def sync_all() -> dict:
    """扫描全部根目录（种子知识文件 + 后台上传目录）并合并统计。"""
    total = {"files": 0, "added": 0, "updated": 0, "removed": 0, "skipped": 0, "chunks": 0}
    roots = scan_roots()
    if not roots:
        return total
    # 先扫种子目录（会做删除清理），再扫上传目录
    roots.sort(key=lambda p: 0 if p == pathlib.Path(config.KB_FILES_DIR) else 1)
    for root in roots:
        s = sync_files(root)
        for k in total:
            total[k] += s.get(k, 0)
    return total


# ------------------------------------------------------------------ 管理端 CRUD
def list_docs(limit: int = 100, offset: int = 0, keyword: str = "", doc_type: str = ""):
    conn = get_conn()
    try:
        where, vals = [], []
        if keyword:
            where.append("(title LIKE ? OR content LIKE ?)")
            vals += [f"%{keyword}%", f"%{keyword}%"]
        if doc_type:
            where.append("doc_type=?")
            vals.append(doc_type)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM knowledge_docs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            vals + [limit, offset],
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) AS c FROM knowledge_docs{clause}", vals).fetchone()["c"]
        type_rows = conn.execute(
            "SELECT doc_type, COUNT(*) AS c FROM knowledge_docs GROUP BY doc_type"
        ).fetchall()
        by_type = {(r["doc_type"] or "text"): r["c"] for r in type_rows}
        return [dict(r) for r in rows], total, by_type
    finally:
        conn.close()


def add_doc(title: str, category: str, content: str, source: str, doc_type: str = "text") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO knowledge_docs(title,category,content,source,doc_type) VALUES(?,?,?,?,?)",
            (title, category, content, source, doc_type),
        )
        conn.commit()
        doc_id = cur.lastrowid
    finally:
        conn.close()
    rebuild_index()
    return doc_id


def update_doc(doc_id: int, fields: dict) -> bool:
    allowed = {"title", "category", "content", "source", "enabled", "doc_type"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    vals.append(doc_id)
    conn = get_conn()
    try:
        cur = conn.execute(f"UPDATE knowledge_docs SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    rebuild_index()
    return ok


def delete_doc(doc_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM knowledge_docs WHERE id=?", (doc_id,))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    rebuild_index()
    return ok


def get_doc(doc_id: int):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM knowledge_docs WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
