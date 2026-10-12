"""全局配置：从项目 .env（以及上级 E:\\pythonProject1\\.env）加载。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# 优先项目内 .env，其次读取上级目录（E:\pythonProject1\.env）中已有的 DeepSeek Key
load_dotenv(BASE_DIR / ".env", override=True)
load_dotenv(BASE_DIR.parent / ".env", override=False)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
JWT_SECRET = os.getenv("JWT_SECRET", "bhu-campus-agent-dev-secret-change-me").strip()
JWT_EXPIRE_DAYS = 7
AMAP_API_KEY = os.getenv("AMAP_API_KEY", "").strip()

PORT = int(os.getenv("PORT", "8000"))
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "campus.db")))
LOG_DIR = BASE_DIR / "logs"
SEED_DIR = BASE_DIR / "seed"
MCP_SEED_FILE = SEED_DIR / "mcp_servers.json"

# ---- RAG 知识库文件（文档 / PDF / 表格）----
# 启动时会自动扫描该目录，按文件内容哈希增量导入知识库
# 注意：环境变量留空字符串时按“未设置”处理，避免 Path("") 变成当前目录
KB_FILES_DIR = Path(os.getenv("KB_FILES_DIR", "").strip() or str(SEED_DIR / "kb_files"))
# 管理员从后台上传的文件落盘位置（同样会被重新扫描）
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
# 向量检索后端：auto=有 numpy 时用 LSA，否则用哈希向量；也可强制 hashing / lsa
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "").strip().lower() or "auto"
# LSA 语义向量维度
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "128"))
# 混合检索权重（BM25 : 向量），两者相加为 1
HYBRID_BM25_WEIGHT = float(os.getenv("HYBRID_BM25_WEIGHT", "0.55"))
HYBRID_VECTOR_WEIGHT = float(os.getenv("HYBRID_VECTOR_WEIGHT", "0.45"))
# 向量检索召回条数（融合前每路的候选数量）
RETRIEVE_CANDIDATES = int(os.getenv("RETRIEVE_CANDIDATES", "20"))

# 对话上下文最多保留的消息条数（多轮记忆）
MAX_HISTORY = 14
# Agent 单轮最多执行工具次数
MAX_TOOL_ROUNDS = 8
# 工具结果传给模型的截断长度，防止超长输出
TOOL_RESULT_MAX = 2000
