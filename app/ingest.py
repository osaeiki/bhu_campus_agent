"""知识库文件导入：把「文档 / PDF / 表格」切分成可检索的知识块。

支持的格式：
- 文档：.md / .markdown / .txt（按标题切段）、.docx（按段落 + 表格）
- PDF ：.pdf（按页抽取文本，再用标点切分）
- 表格：.xlsx / .xlsm（每个工作表转成 Markdown 表格，按行分块）、.csv

切分策略：
1. 文本类按“标题 / 标点”做语义切分，再交给 knowledge.py 做 400 字 + 60 字重叠分块；
2. 表格类**按行分块并重复表头**，保证每一块单独看都是完整可读的表格行，
   不会出现“只检索到半行、不知道列名”的情况；
3. 每个块都带 source_file / doc_type，回答时可以给出处；
4. 导入按文件内容哈希做增量同步：文件没变就跳过，变了就整篇替换。
"""
import csv
import hashlib
import io
import re
from pathlib import Path

SUPPORTED_EXTS = {".md", ".markdown", ".txt", ".pdf", ".xlsx", ".xlsm", ".csv", ".docx"}

# 表格每块包含的数据行数（表头会重复出现在每一块里）
TABLE_ROWS_PER_CHUNK = 6

# 关键词 -> 分类：让导入的知识块带上有意义的分类，便于后台筛选与展示
_CATEGORY_RULES = [
    (("校历", "学期", "开学", "放假"), "校历与作息"),
    (("食堂", "餐厅", "美食", "餐"), "食堂餐饮"),
    (("宿舍", "公寓", "住宿", "寝室"), "宿舍住宿"),
    (("图书", "借阅", "自习"), "图书馆"),
    (("报修", "维修", "后勤", "电话", "通讯录"), "后勤服务"),
    (("规章", "规定", "管理办法", "纪律", "考试", "学籍"), "规章制度"),
    (("入学", "新生", "报到", "指南", "生活"), "新生指南"),
    (("场馆", "预约", "体育馆", "操场", "运动"), "场馆与体育"),
    (("交通", "班车", "公交", "出行", "校车"), "交通出行"),
    (("办事", "流程", "证明", "证件"), "办事流程"),
]


def _guess_category(name: str, fallback: str = "文件导入") -> str:
    for keys, cat in _CATEGORY_RULES:
        if any(k in name for k in keys):
            return cat
    return fallback


def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 表格切分
def _rows_to_chunks(title: str, header: list[str], rows: list[list[str]]) -> list[str]:
    """把表格按固定行数切成若干块，每块都带表头，保证块内语义完整。"""
    header = [str(h).strip() for h in header]
    head_line = "| " + " | ".join(header) + " |"
    sep_line = "| " + " | ".join("---" for _ in header) + " |"
    chunks: list[str] = []
    # 第一块给一个“表结构说明”，方便回答“这张表有哪些字段”类问题
    chunks.append(
        f"《{title}》表格说明：共 {len(rows)} 行数据，字段包括："
        + "、".join(h for h in header if h)
        + "。"
    )
    if not rows:
        return chunks
    for start in range(0, len(rows), TABLE_ROWS_PER_CHUNK):
        part = rows[start : start + TABLE_ROWS_PER_CHUNK]
        lines = [f"《{title}》（第 {start + 1}-{start + len(part)} 行）", head_line, sep_line]
        for r in part:
            cells = [str(c).replace("\n", " ").strip() for c in r]
            cells += [""] * (len(header) - len(cells))
            lines.append("| " + " | ".join(cells[: len(header)]) + " |")
        chunks.append("\n".join(lines))
    return chunks


def _parse_xlsx(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    out: list[dict] = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if not rows:
                continue
            header, data = rows[0], rows[1:]
            title = f"{path.stem}·{ws.title}"
            for i, chunk in enumerate(_rows_to_chunks(title, header, data)):
                out.append(
                    {
                        "title": title if i == 0 else f"{title}（数据 {i}）",
                        "content": chunk,
                        "doc_type": "table",
                    }
                )
    finally:
        wb.close()
    return out


def _parse_csv(path: Path) -> list[dict]:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(str(c).strip() for c in r)]
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    title = path.stem
    return [
        {"title": title if i == 0 else f"{title}（数据 {i}）", "content": chunk, "doc_type": "table"}
        for i, chunk in enumerate(_rows_to_chunks(title, header, data))
    ]


# ---------------------------------------------------------------- 文档切分
def _parse_markdown(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _split_markdown(text, path.stem)


def _split_markdown(text: str, stem: str) -> list[dict]:
    """按 Markdown 标题切段：每个小节一个知识块，标题写进标题字段便于引用。"""
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    cur_title, cur_body = stem, []
    for line in lines:
        m = re.match(r"^(#{1,4})\s+(.*)$", line.strip())
        if m:
            if cur_body and any(x.strip() for x in cur_body):
                sections.append((cur_title, cur_body))
            cur_title, cur_body = m.group(2).strip(), []
        else:
            cur_body.append(line)
    if any(x.strip() for x in cur_body):
        sections.append((cur_title, cur_body))
    if not sections:
        sections = [(stem, lines)]

    out: list[dict] = []
    for title, body in sections:
        content = "\n".join(body).strip()
        if not content:
            continue
        full_title = title if title.startswith(stem) else f"{stem} · {title}"
        out.append({"title": full_title, "content": content, "doc_type": "text"})
    return out


def _docx_table_rows(table) -> tuple[list[str], list[list[str]]]:
    rows = []
    for row in table.rows:
        rows.append([cell.text.strip() for cell in row.cells])
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _parse_docx(path: Path) -> list[dict]:
    try:
        import docx  # python-docx
    except Exception:
        print(f"[ingest] 跳过 {path.name}：未安装 python-docx")
        return []
    d = docx.Document(str(path))
    out: list[dict] = []
    buf_title: str = path.stem
    buf: list[str] = []

    def flush():
        if any(x.strip() for x in buf):
            title = buf_title if buf_title.startswith(path.stem) else f"{path.stem} · {buf_title}"
            out.append({"title": title, "content": "\n".join(buf).strip(), "doc_type": "text"})
        buf.clear()

    for para in d.paragraphs:
        txt = (para.text or "").strip()
        if not txt:
            continue
        style = (para.style.name or "") if para.style is not None else ""
        if style.lower().startswith("heading") or style.startswith("标题"):
            flush()
            buf_title = txt
        else:
            buf.append(txt)
    flush()
    for idx, table in enumerate(d.tables, 1):
        header, data = _docx_table_rows(table)
        title = f"{path.stem}·表格{idx}"
        for i, chunk in enumerate(_rows_to_chunks(title, header, data)):
            out.append(
                {"title": title if i == 0 else f"{title}（数据 {i}）", "content": chunk, "doc_type": "table"}
            )
    return out


# ---------------------------------------------------------------- PDF 切分
def _parse_pdf(path: Path) -> list[dict]:
    try:
        from pypdf import PdfReader
    except Exception:
        print(f"[ingest] 跳过 {path.name}：未安装 pypdf")
        return []
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        print(f"[ingest] PDF 解析失败 {path.name}：{e}")
        return []
    out: list[dict] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) < 20:
            continue
        out.append(
            {
                "title": f"{path.stem}（第 {i} 页）",
                "content": text,
                "doc_type": "pdf",
            }
        )
    return out


_PARSERS = {
    ".md": _parse_markdown,
    ".markdown": _parse_markdown,
    ".txt": _parse_markdown,
    ".pdf": _parse_pdf,
    ".xlsx": _parse_xlsx,
    ".xlsm": _parse_xlsx,
    ".csv": _parse_csv,
    ".docx": _parse_docx,
}


def load_file(path: Path, category: str = "") -> list[dict]:
    """把一个文件解析成若干知识块（title/content/doc_type/source/source_file）。"""
    parser = _PARSERS.get(path.suffix.lower())
    if not parser:
        return []
    try:
        pieces = parser(path)
    except Exception as e:
        print(f"[ingest] 解析失败 {path.name}：{e}")
        return []
    cat = category or _guess_category(path.stem)
    for p in pieces:
        p["source"] = f"{path.name}"
        p["source_file"] = path.name
        p["category"] = cat
    return pieces


def scan_directory(directory: Path) -> list[Path]:
    directory = Path(directory)
    if not directory.exists():
        return []
    files = [
        p
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and not p.name.startswith("~$")
    ]
    return files
