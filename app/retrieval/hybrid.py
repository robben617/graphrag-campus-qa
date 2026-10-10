"""混合检索：关键词 + 向量，RRF（Reciprocal Rank Fusion）融合 + 检索方式对比。

RRF 公式：score(d) = Σ_{r} 1 / (k + rank_r(d))，k 默认 60。

运行期降级（对齐设计文档 §7.2）：
- 向量路抛异常（本地模型损坏、API 抖动等）时**不中断问答**，自动降级为
  「关键词单路检索」并记录降级原因，由上层提示用户
  「语义检索暂不可用，已切换关键词模式」。
- 只有一路时 RRF 依然成立（等价于该路的排名），无需分支处理。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from ..db import Database
from ..embeddings import BaseEmbedder
from . import search as keyword_search
from . import vector as vector_search

logger = logging.getLogger("school_agent.retrieval.hybrid")


@dataclass
class HybridHit:
    doc_id: str
    doc_title: str
    chapter: str = ""
    article_no: str = ""
    content: str = ""
    score: float = 0.0
    keyword_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    source_path: str = ""

    def citation(self) -> str:
        loc = f"第{self.article_no}条" if self.article_no else ""
        if self.chapter and loc:
            loc = f"{self.chapter} {loc}"
        elif self.chapter:
            loc = self.chapter
        return f"《{self.doc_title}》{loc}".rstrip("》")


def rrf_fusion(
    keyword_hits: List[keyword_search.Hit],
    vector_hits: List[vector_search.VectorHit],
    top_k: int = 5,
    k: int = 60,
) -> List[HybridHit]:
    """按 RRF 融合两路检索结果，返回融合后的 Top-K 命中。"""
    scores: dict = {}
    meta: dict = {}

    for rank, h in enumerate(keyword_hits, start=1):
        key = h.fingerprint()
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        meta.setdefault(key, h)

    for rank, h in enumerate(vector_hits, start=1):
        key = h.fingerprint()
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        meta.setdefault(key, h)

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    result: List[HybridHit] = []
    for key, score in ranked[:top_k]:
        src = meta[key]
        result.append(
            HybridHit(
                doc_id=src.doc_id,
                doc_title=src.doc_title,
                chapter=src.chapter,
                article_no=src.article_no,
                content=src.content,
                score=score,
                source_path=getattr(src, "source_path", ""),
            )
        )
    return result


@dataclass
class RetrievalComparison:
    question: str
    keyword: List[keyword_search.Hit]
    vector: List[vector_search.VectorHit]
    hybrid: List[HybridHit]
    # 向量路不可用时的降级原因；空串表示正常（两路都在）
    degraded_reason: str = ""


def search_hybrid(
    db: Database,
    question: str,
    embedder: BaseEmbedder,
    top_k: int = 5,
    active_doc_ids: Optional[set] = None,
    settings=None,
) -> RetrievalComparison:
    kw = keyword_search.search(db, question, top_k=top_k).hits

    # 向量路：失败即降级为关键词单路，保证问答链路不中断（§7.2）
    degraded_reason = ""
    try:
        vec = vector_search.search_vector(
            db, question, embedder, top_k=top_k, active_doc_ids=active_doc_ids
        )
    except Exception as exc:  # noqa: BLE001 - 任何向量化/检索异常都降级
        vec = []
        degraded_reason = "语义检索暂不可用，已切换关键词模式。"
        logger.warning(
            "vector_retrieval_degraded err_type=%s err=%s", type(exc).__name__, exc
        )

    fused = rrf_fusion(kw, vec, top_k=top_k)

    # 可选 reranker 二次排序（本地 BGE-reranker）
    if settings is not None and fused:
        reranked = rerank_hits(db, question, fused, top_k=top_k, settings=settings)
        if reranked:
            fused = reranked

    return RetrievalComparison(
        question=question, keyword=kw, vector=vec, hybrid=fused,
        degraded_reason=degraded_reason,
    )


def rerank_hits(
    db: Database,
    question: str,
    hits: List[HybridHit],
    top_k: int = 5,
    settings=None,
) -> Optional[List[HybridHit]]:
    """用本地 BGE-reranker 对候选重排；不可用返回 None（调用方保留 RRF 结果）。"""
    from .. import ml_local  # noqa: WPS433

    if settings is None or not ml_local.local_reranker_available(settings):
        return None
    passages = [h.content for h in hits]
    ranked = ml_local.rerank_local(settings, question, passages, top_k)
    if ranked is None:
        return None
    out = [hits[i] for i, _ in ranked]
    return out
