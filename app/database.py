"""SQLite 数据层：建表、连接、种子数据。"""
import json
import sqlite3
from contextlib import contextmanager

from . import config
from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'visitor',
  name TEXT NOT NULL,
  student_no TEXT,
  department TEXT,
  class_name TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS knowledge_docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '通用',
  content TEXT NOT NULL,
  source TEXT,
  doc_type TEXT NOT NULL DEFAULT 'text',
  source_file TEXT,
  file_hash TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS courses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  teacher_name TEXT NOT NULL,
  class_name TEXT NOT NULL,
  day_of_week INTEGER NOT NULL,
  start_section INTEGER NOT NULL,
  end_section INTEGER NOT NULL,
  weeks TEXT NOT NULL DEFAULT '1-18',
  location TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER NOT NULL,
  course_name TEXT NOT NULL,
  score REAL NOT NULL,
  credit REAL NOT NULL,
  semester TEXT NOT NULL DEFAULT '2025-2026-1'
);
CREATE TABLE IF NOT EXISTS leaves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  leave_type TEXT NOT NULL,
  reason TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  approver_id INTEGER,
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS repairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  location TEXT NOT NULL,
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  handler_note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS venues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  location TEXT NOT NULL,
  capacity INTEGER NOT NULL DEFAULT 20,
  open_time TEXT NOT NULL DEFAULT '08:00',
  close_time TEXT NOT NULL DEFAULT '21:00',
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  book_date TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  purpose TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  question TEXT NOT NULL,
  agent_context TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  reply TEXT,
  read_by_user INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT,
  role TEXT,
  query TEXT,
  tools TEXT,
  answer TEXT,
  ip TEXT,
  intent TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  session_id TEXT,
  message_id TEXT,
  rating INTEGER,
  comment TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS mcp_servers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  command TEXT NOT NULL,
  args TEXT,
  env TEXT,
  transport TEXT NOT NULL DEFAULT 'stdio',
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'unknown',
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS chat_sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  title TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT,
  tool_calls TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  type TEXT NOT NULL DEFAULT 'system',
  title TEXT NOT NULL,
  content TEXT,
  link TEXT,
  read_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at);
"""


def get_conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        # 旧库迁移：补齐新增列
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(audit_logs)").fetchall()]
        if "intent" not in cols:
            conn.execute("ALTER TABLE audit_logs ADD COLUMN intent TEXT")
        tcols = [r["name"] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()]
        if "read_by_user" not in tcols:
            conn.execute("ALTER TABLE tickets ADD COLUMN read_by_user INTEGER NOT NULL DEFAULT 0")
        kcols = [r["name"] for r in conn.execute("PRAGMA table_info(knowledge_docs)").fetchall()]
        if "doc_type" not in kcols:
            conn.execute("ALTER TABLE knowledge_docs ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'text'")
        if "source_file" not in kcols:
            conn.execute("ALTER TABLE knowledge_docs ADD COLUMN source_file TEXT")
        if "file_hash" not in kcols:
            conn.execute("ALTER TABLE knowledge_docs ADD COLUMN file_hash TEXT")
        conn.commit()
    finally:
        conn.close()


def _users_seed():
    return [
        ("admin", "admin123", "admin", "系统管理员", None, "信息化办公室", None),
        ("teacher", "teacher123", "teacher", "王明", "T1001", "机械工程学院", "机械2301"),
        ("student", "student123", "student", "李明", "2023010101", "机械工程学院", "机械2301"),
        ("student2", "student123", "student", "张雪", "2023020202", "电气与信息工程学院", "电气2301"),
        ("visitor", "visitor123", "visitor", "校外访客", None, None, None),
    ]


def _courses_seed():
    return [
        ("高等数学(一)", "王明", "机械2301", 1, 1, 2, "1-18周", "第三教学楼101"),
        ("大学英语(三)", "陈晨", "机械2301", 2, 3, 4, "1-18周", "第二教学楼302"),
        ("机械制图", "王明", "机械2301", 3, 5, 6, "1-16周", "第一教学楼205"),
        ("大学物理(一)", "刘洋", "机械2301", 4, 1, 2, "1-18周", "第三教学楼305"),
        ("体育(篮球)", "赵强", "机械2301", 5, 7, 8, "1-18周", "田径场"),
        ("高等数学(一)", "王明", "电气2301", 1, 1, 2, "1-18周", "第三教学楼102"),
        ("电路原理", "孙丽", "电气2301", 2, 5, 6, "1-18周", "第三教学楼201"),
        ("大学英语(三)", "陈晨", "电气2301", 4, 3, 4, "1-18周", "第二教学楼304"),
        ("大学物理(一)", "刘洋", "电气2301", 5, 1, 2, "1-18周", "第三教学楼305"),
    ]


def _grades_seed():
    return [
        (3, "高等数学(一)", 85, 4.0, "2025-2026-1"),
        (3, "大学英语(三)", 78, 3.0, "2025-2026-1"),
        (3, "机械制图", 90, 3.5, "2025-2026-1"),
        (3, "大学物理(一)", 82, 4.0, "2025-2026-1"),
        (4, "高等数学(一)", 88, 4.0, "2025-2026-1"),
        (4, "电路原理", 91, 4.5, "2025-2026-1"),
        (4, "大学英语(三)", 75, 3.0, "2025-2026-1"),
    ]


def _venues_seed():
    return [
        ("三教报告厅", "第三教学楼一楼", 120, "08:00", "21:00", "open"),
        ("图书馆研讨室A", "图书馆二楼", 12, "08:00", "20:00", "open"),
        ("体育馆羽毛球场地", "体育馆二楼", 4, "09:00", "20:00", "open"),
        ("田径场足球场", "田径场中央", 22, "06:00", "21:00", "open"),
    ]


def seed_if_empty():
    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if n == 0:
            for u in _users_seed():
                conn.execute(
                    "INSERT INTO users(username,password_hash,role,name,student_no,department,class_name) VALUES(?,?,?,?,?,?,?)",
                    (u[0], hash_password(u[1]), u[2], u[3], u[4], u[5], u[6]),
                )
        if conn.execute("SELECT COUNT(*) AS c FROM courses").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO courses(name,teacher_name,class_name,day_of_week,start_section,end_section,weeks,location) VALUES(?,?,?,?,?,?,?,?)",
                _courses_seed(),
            )
        if conn.execute("SELECT COUNT(*) AS c FROM grades").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO grades(student_id,course_name,score,credit,semester) VALUES(?,?,?,?,?)",
                _grades_seed(),
            )
        if conn.execute("SELECT COUNT(*) AS c FROM venues").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO venues(name,location,capacity,open_time,close_time,status) VALUES(?,?,?,?,?,?)",
                _venues_seed(),
            )
        conn.commit()
    finally:
        conn.close()


def seed_mcp_config():
    """首次运行时把 seed/mcp_servers.json 里的外部 MCP 写入配置表。"""
    if not config.MCP_SEED_FILE.exists():
        return
    conn = get_conn()
    try:
        if conn.execute("SELECT COUNT(*) AS c FROM mcp_servers").fetchone()["c"] > 0:
            return
        data = json.loads(config.MCP_SEED_FILE.read_text(encoding="utf-8"))
        for item in data:
            conn.execute(
                "INSERT INTO mcp_servers(name,command,args,env,transport,enabled,status) VALUES(?,?,?,?,?,?,?)",
                (
                    item["name"],
                    item["command"],
                    json.dumps(item.get("args", []), ensure_ascii=False),
                    json.dumps(item.get("env", {}), ensure_ascii=False),
                    item.get("transport", "stdio"),
                    1 if item.get("enabled", True) else 0,
                    "unknown",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def seed_knowledge_if_empty():
    """导入 seed/knowledge_seed.json 知识库：仅补充标题不存在的文档，不覆盖人工修改。"""
    kb_file = config.SEED_DIR / "knowledge_seed.json"
    if not kb_file.exists():
        return
    conn = get_conn()
    try:
        data = json.loads(kb_file.read_text(encoding="utf-8"))
        added = 0
        for d in data:
            exists = conn.execute("SELECT id FROM knowledge_docs WHERE title=?", (d["title"],)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO knowledge_docs(title,category,content,source) VALUES(?,?,?,?)",
                    (d["title"], d.get("category", "通用"), d["content"], d.get("source", "")),
                )
                added += 1
        if added:
            conn.commit()
            print(f"[seed] 新增知识文档 {added} 条")
    finally:
        conn.close()


def get_user_by_username(username: str):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    finally:
        conn.close()


def get_user_by_id(uid: int):
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
