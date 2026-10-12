"""向量检索后端：RAG 的稠密召回侧。

知识库原来只有 BM25（词面匹配，稀疏检索）。这里补上一条向量通路：

- **lsa（默认，需 numpy）**：把 TF-IDF 矩阵做 SVD 降维（LSA / 潜在语义分析），
  得到每个知识块的稠密语义向量，用余弦相似度召回。能处理“同义不同词”的
  问题，例如用户问“生病了怎么办”也能召回“就医流程/校医院”相关内容。
- **hashing（兜底）**：numpy 不可用时使用符号哈希把词映射到固定维度向量
  （Hashing Trick），同样做 IDF 加权和 L2 归一化，接口完全一致。
- **neural（可选）**：若部署了 sentence-transformers + 本地中文向量模型，
  可通过环境变量 EMBEDDING_BACKEND=neural 切换，这里保留接入位置。

三种后端对上层暴露相同接口（build / query / info），所以 knowledge.py
不需要知道底层用的是哪一种；日后要换成更强的向量后端，只改这一层即可。
"""
import hashlib
import math

try:  # numpy 只用于 LSA，装不上也不影响 BM25 与哈希向量
    import numpy as _np
    HAS_NUMPY = True
except Exception:  # pragma: no cover - 环境缺 numpy 时的兜底
    _np = None
    HAS_NUMPY = False


def _tf_weight(count: int) -> float:
    """次线性词频：抑制高频词，避免长文档靠“词多”刷分。"""
    return 1.0 + math.log(count) if count > 0 else 0.0


def _l2(vec) -> None:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-12:
        for i in range(len(vec)):
            vec[i] /= norm


class _BaseEmbedder:
    backend = "base"

    def __init__(self) -> None:
        self.idf: dict[str, float] = {}
        self.default_idf = 1.0

    def _fit_idf(self, docs_tokens: list[list[str]]) -> None:
        n = max(1, len(docs_tokens))
        df: dict[str, int] = {}
        for toks in docs_tokens:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self.idf = {
            t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }
        vals = sorted(self.idf.values())
        self.default_idf = vals[len(vals) // 2] if vals else 1.0


class HashingEmbedder(_BaseEmbedder):
    """符号哈希向量：零依赖、确定性（用 md5 而不是内置 hash，保证跨进程一致）。"""

    backend = "hashing"

    def __init__(self, dim: int = 512) -> None:
        super().__init__()
        self.dim = dim

    def fit(self, docs_tokens: list[list[str]]) -> None:
        self._fit_idf(docs_tokens)

    def encode(self, tokens: list[str]) -> list[float]:
        if not tokens:
            return [0.0] * self.dim
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        vec = [0.0] * self.dim
        for t, c in tf.items():
            digest = hashlib.md5(t.encode("utf-8")).digest()
            h = int.from_bytes(digest[:8], "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign * self.idf.get(t, self.default_idf) * _tf_weight(c)
        _l2(vec)
        return vec


class LsaEmbedder(_BaseEmbedder):
    """TF-IDF + 截断 SVD（LSA）语义向量。"""

    backend = "lsa"

    def __init__(self, dim: int = 128) -> None:
        super().__init__()
        self.dim = dim
        self.vocab: dict[str, int] = {}
        self.components = None  # (k, vocab) 投影矩阵
        self.doc_vectors = None  # (n, k) 已归一化的块向量

    def _vectorize(self, tokens: list[str]) -> "list[float]":
        vec = [0.0] * len(self.vocab)
        if not tokens:
            return vec
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            j = self.vocab.get(t)
            if j is None:
                continue
            vec[j] = self.idf.get(t, self.default_idf) * _tf_weight(c)
        _l2(vec)
        return vec

    def fit(self, docs_tokens: list[list[str]]) -> None:
        self._fit_idf(docs_tokens)
        vocab: dict[str, int] = {}
        for toks in docs_tokens:
            for t in toks:
                if t not in vocab:
                    vocab[t] = len(vocab)
        self.vocab = vocab
        n = len(docs_tokens)
        if n == 0 or not vocab:
            self.doc_vectors = _np.zeros((0, 0)) if HAS_NUMPY else None
            self.components = _np.zeros((0, 0)) if HAS_NUMPY else None
            return
        mat = _np.zeros((n, len(vocab)), dtype="float32")
        for i, toks in enumerate(docs_tokens):
            row = self._vectorize(toks)
            mat[i, :] = row
        k = max(1, min(self.dim, min(mat.shape)))
        try:
            u, s, vt = _np.linalg.svd(mat, full_matrices=False)
        except _np.linalg.LinAlgError:  # 极端情况下退化为恒等投影
            u, s, vt = mat, _np.ones(mat.shape[1]), _np.eye(mat.shape[1])
        k = max(1, min(k, s.shape[0]))
        doc_vec = u[:, :k] * s[:k]
        norms = _np.linalg.norm(doc_vec, axis=1, keepdims=True)
        norms[norms < 1e-12] = 1.0
        self.doc_vectors = doc_vec / norms
        self.components = vt[:k]

    def encode(self, tokens: list[str]) -> "list[float]":
        if not HAS_NUMPY or self.components is None or self.components.size == 0:
            return []
        q = _np.asarray(self._vectorize(tokens), dtype="float32")
        vec = self.components @ q  # (k,)
        norm = float(_np.linalg.norm(vec))
        if norm < 1e-12:
            return [0.0] * vec.shape[0]
        return (vec / norm).tolist()


def _dot(a, b) -> float:
    """余弦相似度点积；有 numpy 用 numpy，没有就用纯 Python。"""
    if HAS_NUMPY:
        return float(_np.dot(_np.asarray(a, dtype="float32"), _np.asarray(b, dtype="float32")))
    return float(sum(x * y for x, y in zip(a, b)))


class VectorIndex:
    """对上层暴露统一接口的向量索引（构建时把知识块一次性编码好）。"""

    def __init__(self, dim: int = 128, backend: str = "auto") -> None:
        wanted = (backend or "auto").lower()
        if wanted == "neural":
            # 预留：未安装神经向量模型时自动降级，不影响服务启动
            wanted = "auto"
        if wanted == "lsa" and HAS_NUMPY:
            self.embedder = LsaEmbedder(dim=dim)
        elif wanted == "hashing" or not HAS_NUMPY:
            self.embedder = HashingEmbedder(dim=512)
        else:
            self.embedder = LsaEmbedder(dim=dim)
        self.size = 0
        self._docs: list[list[float]] = []

    def build(self, docs_tokens: list[list[str]]) -> None:
        self.embedder.fit(docs_tokens)
        self._docs = [self.embedder.encode(t) for t in docs_tokens]
        self.size = len(docs_tokens)

    def query(self, tokens: list[str], top_k: int = 20) -> list[tuple[int, float]]:
        """返回 [(块下标, 余弦相似度)]，按相似度降序。"""
        if not tokens or not self._docs:
            return []
        qv = self.embedder.encode(tokens)
        if not qv:
            return []
        sims = [(i, _dot(dv, qv)) for i, dv in enumerate(self._docs)]
        sims = [x for x in sims if x[1] > 1e-6]
        sims.sort(key=lambda x: x[1], reverse=True)
        return sims[:top_k]

    def info(self) -> dict:
        return {
            "backend": self.embedder.backend,
            "dim": getattr(self.embedder, "dim", 0),
            "vectors": self.size,
            "has_numpy": HAS_NUMPY,
        }
