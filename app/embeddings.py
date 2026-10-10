"""向量化：Embedding 客户端 + 本地确定性兜底向量器。

设计（对齐 §5.3 / §7.2）：
- 配置了 EMBEDDING_API_KEY 时走 OpenAI 兼容 /embeddings 接口；
- 未配置时使用「字符 n-gram 哈希」确定性向量器，保证离线可运行、可复现、
  换机器结果一致（满足 AT-10）。兜底向量器语义能力弱于真实 Embedding，
  但足以演示"向量检索 vs 关键词检索 vs 混合(RRF)"的完整链路。

统一输出：L2 归一化后的 float 列表。
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import List, Sequence

from .config import Settings
from .errors import LLMUnavailable


class BaseEmbedder:
    dim: int

    def embed(self, text: str) -> List[float]:
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class HashingEmbedder(BaseEmbedder):
    """本地确定性字符 n-gram 哈希向量器（离线兜底）。

    对中文按字符 unigram/bigram 打哈希分桶 + 归一化；无外部依赖、结果确定。
    """

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _ngrams(self, text: str) -> List[str]:
        text = re.sub(r"\s+", "", text)
        grams: List[str] = []
        chars = list(text)
        grams.extend(chars)  # unigram
        for i in range(len(chars) - 1):
            grams.append(chars[i] + chars[i + 1])  # bigram
        # 英文/数字单词整体保留
        grams.extend(re.findall(r"[A-Za-z0-9]+", text))
        return grams

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for g in self._ngrams(text):
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            return vec
        return [x / norm for x in vec]


class OpenAIEmbedder(BaseEmbedder):
    """OpenAI 兼容 Embedding 接口（智谱/DeepSeek/Qwen/DashScope 等）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.dim = settings.embedding_dim

    def _call(self, texts: Sequence[str]) -> List[List[float]]:
        try:
            import requests  # noqa: WPS433
        except ImportError as exc:
            raise LLMUnavailable(detail="requests 未安装") from exc
        url = self.settings.embedding_base_url.rstrip("/") + "/embeddings"
        payload = {"model": self.settings.embedding_model, "input": list(texts)}
        headers = {"Authorization": f"Bearer {self.settings.embedding_api_key}", "Content-Type": "application/json"}
        last_err = None
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                data = resp.json()["data"]
                data = sorted(data, key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in data]
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(min(2 ** attempt, 3))
        raise LLMUnavailable(detail=f"Embedding 调用失败: {last_err}")

    def embed(self, text: str) -> List[float]:
        return self._call([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return self._call(texts)


class LocalEmbedder(BaseEmbedder):
    """本地 BGE Embedding 包装（惰性加载；不可用时 raise，调用方降级）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.dim = 0  # 首次 embed 后由实际向量长度确定

    def embed(self, text: str) -> List[float]:
        vec = self.embed_batch([text])[0]
        return vec

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        from .ml_local import encode_local

        vecs = encode_local(self.settings, texts)
        if vecs is None:
            raise RuntimeError("本地 Embedding 不可用")
        if vecs and self.dim == 0:
            self.dim = len(vecs[0])
        return vecs


def get_embedder(settings: Settings) -> BaseEmbedder:
    """按优先级选择向量器：本地 BGE → Embedding API → 哈希兜底。

    本地/API 在运行期失败时（embed 抛异常），上层 ensure/search 会降级到哈希向量器。
    """
    from . import ml_local  # noqa: WPS433 (延迟导入避免循环)

    if ml_local.local_embedder_available(settings):
        try:
            return LocalEmbedder(settings)
        except Exception:  # noqa: BLE001
            pass
    if settings.embedding_enabled and settings.embedding_api_key and settings.embedding_base_url:
        try:
            return OpenAIEmbedder(settings)
        except Exception:  # noqa: BLE001
            return HashingEmbedder(settings.embedding_dim)
    return HashingEmbedder(settings.embedding_dim)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（两向量已 L2 归一化时为点积）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot))
