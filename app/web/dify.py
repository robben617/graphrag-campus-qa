"""Dify 外部知识库适配器（External Knowledge API）。

作用：把本项目的混合检索（jieba 关键词 + BGE 向量 + RRF 融合 + BGE 重排）
按 Dify 官方约定的协议暴露出去，让 Dify 的「知识检索」节点可以把本系统当作
一个外部知识库来查——复用 Dify 的对话编排与 WebApp，而不必把条款重新灌进
Dify 自己的向量库。

协议（与 Dify External Knowledge API 一致）：
    请求  POST {api_endpoint}/retrieval      ← Dify 自动在 Endpoint 后追加 /retrieval
          Header: Authorization: Bearer {API_KEY}
          Body:   {"knowledge_id": "...", "query": "...",
                   "retrieval_setting": {"top_k": 3, "score_threshold": 0.5}}
    响应  200 {"records": [{"content": "...", "score": 0.98, "title": "...",
                           "metadata": {...}}, ...]}

三点设计取舍：
1. 只读。不提供写入，Dify 无法改动本系统的知识库内容。
2. 网关级开关。未配置 DIFY_RETRIEVAL_API_KEY 时该路由不会被注册（见 server.py），
   避免暴露一个不需要鉴权的检索端点。
3. score 是「排名归一化分」而非相似度。最终排序由 RRF + BGE-reranker 决定，
   两路原始分不同量纲（RRF 是 1/(k+rank)，只有 0.016 量级；reranker 输出可为负的
   logit）。与其把异构分数伪装成相似度，这里统一映射为 1.0 → 0.1 的单调递减分，
   仅用于让 Dify 侧的排序与阈值可用。因此 Dify 侧 Score 阈值建议关闭（设为 0），
   条数交给 Top K 控制。
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import Settings
from ..db import Database
from ..embeddings import BaseEmbedder
from ..logging_util import log_event
from ..retrieval import hybrid as hybrid_mod
from ..service import versioning
from . import deps

logger = logging.getLogger("school_agent.web.dify")

router = APIRouter(tags=["dify"])

# 排名分：第 1 名 1.0，之后每名递减 0.1，下限 0.1（仅用于 Dify 展示与排序）
_RANK_STEP = 0.1
_RANK_FLOOR = 0.1

# [待核实: 具体错误码取值请对照 Dify 官方 External Knowledge API 文档确认]
# 常见约定：1001=Authorization 头格式错误 / 1002=鉴权失败 / 2001=知识库不存在。
ERR_INVALID_REQUEST = 1001
ERR_AUTH_FAILED = 1002


class RetrievalSetting(BaseModel):
    top_k: int = Field(3, description="最大返回条数")
    score_threshold: float = Field(0.0, description="最低分数；本项目不做二次过滤，仅接收")


class RetrievalRequest(BaseModel):
    knowledge_id: str = Field("", description="外部知识库 ID，Dify 侧配置，原样回传")
    query: str = Field(..., description="用户查询")
    retrieval_setting: Optional[RetrievalSetting] = Field(None, description="检索参数")


def _rank_score(rank: int) -> float:
    """名次 → (0, 1] 的单调递减分。rank 从 1 开始。"""
    return max(_RANK_FLOOR, 1.0 - (rank - 1) * _RANK_STEP)


def _bearer_ok(request: Request, expected: str) -> bool:
    """常量时间比较 Bearer 令牌，避免时序侧信道。"""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix):].strip(), expected)


@router.post("/retrieval")
def dify_retrieval(
    req: RetrievalRequest,
    request: Request,
    db: Database = Depends(deps.get_db),
    embedder: BaseEmbedder = Depends(deps.get_embedder),
    settings: Settings = Depends(deps.get_app_settings),
):
    if not _bearer_ok(request, settings.dify_retrieval_api_key):
        return JSONResponse(
            status_code=403,
            content={"error_code": ERR_AUTH_FAILED, "error_msg": "Authorization failed"},
        )

    query = (req.query or "").strip()
    if not query:
        return JSONResponse(
            status_code=400,
            content={"error_code": ERR_INVALID_REQUEST, "error_msg": "query is required"},
        )

    asked_k = req.retrieval_setting.top_k if req.retrieval_setting else 3
    top_k = max(1, min(int(asked_k), settings.dify_max_top_k))

    start = time.perf_counter()

    # 与 /api/v1/ask 完全同一条检索链路：时效过滤 → 双路召回 → RRF → 可选 BGE 重排
    # active_doc_ids 在服务端算好，Dify 侧无需关心时效语义
    active_ids = versioning.active_doc_ids(db)
    comparison = hybrid_mod.search_hybrid(
        db, query, embedder, top_k=top_k, active_doc_ids=active_ids, settings=settings,
    )

    records: List[dict] = []
    # 拒答语义对齐 AT-03：关键词零命中说明知识库无实质依据，返回空记录，
    # 由 Dify 侧（提示词或条件分支）输出「未找到」话术
    if comparison.keyword:
        for rank, hit in enumerate(comparison.hybrid, start=1):
            records.append({
                "content": hit.content,
                "score": _rank_score(rank),
                "title": hit.citation(),
                "metadata": {
                    "doc_id": hit.doc_id,
                    "doc_title": hit.doc_title,
                    "chapter": hit.chapter,
                    "article_no": hit.article_no,
                    "source_path": hit.source_path,
                    "knowledge_id": req.knowledge_id,
                },
            })

    elapsed = int((time.perf_counter() - start) * 1000)
    log_event(
        logger, "dify_retrieval",
        request_id=getattr(request.state, "request_id", None),
        elapsed_ms=elapsed, top_k=top_k, records=len(records),
        degraded=bool(comparison.degraded_reason),
    )
    return {"records": records}
