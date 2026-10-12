"""FastAPI 应用：前后端分离的 REST API + Swagger + 统一异常 + 结构化日志。

依赖注入（详见 app/web/deps.py）：
- 应用级重资源（Database / QAService / ConversationService / Embedding 模型）在
  create_app 时创建一次并存入 app.state，路由通过 Depends 取用，全局复用；
- 请求级依赖（当前用户、角色权限）用 Depends 声明在路由签名中。

路由概览（均在 /api/v1 下）：
- POST /ask                      单轮问答（GraphRAG）
- GET/POST/DELETE /sessions       多轮会话（多用户隔离）
- GET /sessions/{id}/messages     会话历史
- POST /sessions/{id}/ask         会话内追问（含指代改写）
- GET /graph/stats                图谱统计
- GET /graph/categories           分类统计
- GET /graph/nodes?q=             节点搜索
- GET /graph/nodes/{id}/neighbors 邻居探索
- GET /graph/subgraph             子图导出
- GET /graph/paths/{id}           关系路径（引用展开）
- GET /quiz/categories            题库分类
- GET /quiz/questions             抽题
- POST /quiz/answer               判题
- GET /quiz/wrong                 错题集
- GET /quiz/summary               答题统计
- GET/POST/DELETE /mastery        掌握标记
- GET /search/compare             检索方式对比
- GET /versioning                 时效全景
- POST/GET /users  /me            用户与权限
- POST /ingest/notice             增量入库（老师）
- POST /evaluation/run  /latest   质量评测
- GET /health                     健康检查（含本地模型状态）
- GET /stats                      数据规模统计

Dify 外部知识库适配（独立前缀，仅在配置 DIFY_RETRIEVAL_API_KEY 时注册）：
- POST /dify/retrieval            按 Dify External Knowledge API 协议暴露混合检索
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings
from ..db import Database
from ..embeddings import BaseEmbedder, get_embedder
from ..errors import AppError, InternalError, StorageUnavailable
from ..logging_util import log_event, setup_logging
from ..service import evaluation as eval_mod
from ..service import graph as graph_mod
from ..service import ingestion as ingest_mod
from ..service import mastery as mastery_mod
from ..service import paths as paths_mod
from ..service import quiz as quiz_mod
from ..service import users as users_mod
from ..service import versioning as version_mod
from ..service.conversation import ConversationService
from ..service.qa import QAService
from . import deps
from .dify import router as dify_router

logger = logging.getLogger("school_agent.web")


class AskRequest(BaseModel):
    question: str = Field(..., description="提问内容")
    # 校验交给 QAService，避免 pydantic 422 与统一 400 错误不一致


class SessionAskRequest(BaseModel):
    question: str = Field(..., description="追问内容")


class SessionCreateRequest(BaseModel):
    title: str = Field("新会话", description="会话标题")


class MasterySetRequest(BaseModel):
    item_id: str = Field(..., description="实体 eid 或 clause:{id}")
    item_type: str = Field(..., description="entity | clause")
    name: str = Field(..., description="显示名称")
    status: str = Field(..., description="mastered | unmastered")


class NoticeIngestRequest(BaseModel):
    text: str = Field(..., description="通知全文")
    title: str = Field(..., description="通知标题")
    category: str = Field(..., description="分类")
    issuer: str = Field("", description="发文部门")
    publish_date: str = Field("", description="发布日期")
    effective_date: str = Field("", description="生效日期")
    version: str = Field("", description="版本")
    applies_to: str = Field("", description="适用对象")
    source_filename: str = Field("", description="原始文件名")
    auto_supersede: bool = Field(True, description="自动检测并建立 SUPERSEDES 关系")


class UserCreateRequest(BaseModel):
    username: str = Field(..., description="用户名")
    role: str = Field("student", description="student | teacher | admin")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)

    app = FastAPI(
        title="校园知识问答系统",
        description="GraphRAG 校园制度/通知知识问答：检索+图遍历+多轮会话+自测错题+掌握标记+本地模型。",
        version=__version__,
    )

    # 前后端分离：允许独立前端跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------- 依赖装配：重资源只创建一次，存入 app.state 供 Depends 注入 ----------
    db = Database(settings.db_path)
    db.init_schema()
    app.state.settings = settings
    app.state.db = db
    app.state.qa = QAService(db, settings)
    app.state.conv = ConversationService(db, settings)
    app.state.embedder = get_embedder(settings)

    # ---------- Dify 外部知识库适配（可选）----------
    # 仅在配置了密钥时注册，避免无意中暴露一个不需要鉴权的检索端点。
    if settings.dify_retrieval_api_key:
        app.include_router(dify_router, prefix="/dify")
        logger.info("dify_adapter_enabled path=/dify/retrieval")
    else:
        logger.info("dify_adapter_disabled reason=no_DIFY_RETRIEVAL_API_KEY")

    # ---------- 中间件：request_id / session_id / 耗时 ----------
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled_error request_id=%s", rid)
            raise
        elapsed = int((time.perf_counter() - start) * 1000)
        response.headers["X-Request-Id"] = rid
        return response

    # ---------- 异常处理 ----------
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_dict(getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(StorageUnavailable)
    async def storage_error_handler(request: Request, exc: StorageUnavailable):
        return JSONResponse(
            status_code=503,
            content=exc.to_dict(getattr(request.state, "request_id", None)),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("unhandled error request_id=%s", getattr(request.state, "request_id", None))
        err = InternalError(detail=str(exc))
        return JSONResponse(
            status_code=500,
            content=err.to_dict(getattr(request.state, "request_id", None)),
        )

    # ---------- 问答 ----------
    @app.post("/api/v1/ask")
    def ask(
        req: AskRequest,
        request: Request,
        qa: QAService = Depends(deps.get_qa),
    ):
        start = time.perf_counter()
        ans = qa.ask(req.question)
        elapsed = int((time.perf_counter() - start) * 1000)
        log_event(
            logger, "ask", request_id=request.state.request_id,
            elapsed_ms=elapsed, mode=ans.mode, hits=len(ans.citations), degraded=ans.degraded,
        )
        return {**ans.to_dict(), "request_id": request.state.request_id, "elapsed_ms": elapsed}

    # ---------- 多轮会话 ----------
    @app.post("/api/v1/sessions")
    def create_session(
        req: SessionCreateRequest,
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
    ):
        return conv.create(req.title, user_id=user["user_id"]).to_dict()

    @app.get("/api/v1/sessions")
    def list_sessions(
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
    ):
        return [s.to_dict() for s in conv.list(user_id=user["user_id"])]

    @app.get("/api/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
    ):
        s = conv.get(session_id, user_id=user["user_id"])
        return {**s.to_dict(), "messages": conv.history(session_id)}

    @app.delete("/api/v1/sessions/{session_id}")
    def delete_session(
        session_id: str,
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
    ):
        conv.delete(session_id, user_id=user["user_id"])
        return {"deleted": session_id}

    @app.get("/api/v1/sessions/{session_id}/messages")
    def session_messages(
        session_id: str,
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
    ):
        conv.get(session_id, user_id=user["user_id"])  # 校验存在
        return conv.history(session_id)

    @app.post("/api/v1/sessions/{session_id}/ask")
    def session_ask(
        session_id: str,
        req: SessionAskRequest,
        request: Request,
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
        qa: QAService = Depends(deps.get_qa),
    ):
        start = time.perf_counter()
        conv.get(session_id, user_id=user["user_id"])  # 校验存在
        history = conv.history(session_id)
        # 指代追问改写
        rewritten = conv.rewrite_question(req.question, history)
        ans = qa.ask(rewritten, history=history)
        # 记录消息（citations 转 dict 以便 JSON 持久化）
        conv.add_message(session_id, "user", req.question)
        conv.add_message(session_id, "assistant", ans.answer, citations=[c.to_dict() for c in ans.citations])
        elapsed = int((time.perf_counter() - start) * 1000)
        log_event(
            logger, "session_ask", request_id=request.state.request_id,
            session_id=session_id, elapsed_ms=elapsed, mode=ans.mode,
            hits=len(ans.citations), degraded=ans.degraded,
        )
        return {
            **ans.to_dict(),
            "rewritten_question": rewritten if rewritten != req.question else None,
            "request_id": request.state.request_id,
            "elapsed_ms": elapsed,
        }

    # ---------- 图谱 ----------
    @app.get("/api/v1/graph/stats")
    def graph_stats(db: Database = Depends(deps.get_db)):
        return graph_mod.graph_stats(db)

    @app.get("/api/v1/graph/categories")
    def graph_categories(db: Database = Depends(deps.get_db)):
        return graph_mod.category_stats(db)

    @app.get("/api/v1/graph/nodes")
    def graph_nodes(
        q: str = Query(..., min_length=1),
        limit: int = 20,
        db: Database = Depends(deps.get_db),
    ):
        return [n.to_dict() for n in graph_mod.search_nodes(db, q, limit)]

    @app.get("/api/v1/graph/nodes/{node_id}/neighbors")
    def graph_neighbors(
        node_id: str,
        depth: int = 1,
        db: Database = Depends(deps.get_db),
    ):
        return graph_mod.neighbors(db, node_id, depth)

    @app.get("/api/v1/graph/subgraph")
    def graph_subgraph(
        node_ids: Optional[str] = Query(None),
        depth: int = 1,
        db: Database = Depends(deps.get_db),
    ):
        ids = [x.strip() for x in node_ids.split(",")] if node_ids else None
        return graph_mod.subgraph(db, node_ids=ids, depth=depth)

    @app.get("/api/v1/graph/paths/{node_id}")
    def graph_paths(
        node_id: str,
        max_depth: int = 2,
        db: Database = Depends(deps.get_db),
    ):
        return {"node_id": node_id, "paths": paths_mod.relation_paths(db, node_id, max_depth)}

    # ---------- 自测题库 ----------
    @app.get("/api/v1/quiz/categories")
    def quiz_categories(db: Database = Depends(deps.get_db)):
        return quiz_mod.list_categories(db)

    @app.get("/api/v1/quiz/questions")
    def quiz_questions(
        category: Optional[str] = Query(None),
        difficulty: Optional[str] = Query(None),
        count: int = 5,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return quiz_mod.draw_questions(
            db, category=category, difficulty=difficulty, count=count, user_id=user["user_id"]
        )

    @app.post("/api/v1/quiz/answer")
    def quiz_answer(
        body: dict,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return quiz_mod.submit_answer(db, body["qid"], int(body["chosen"]), user_id=user["user_id"])

    @app.get("/api/v1/quiz/wrong")
    def quiz_wrong(
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return quiz_mod.wrong_answers(db, user_id=user["user_id"])

    @app.get("/api/v1/quiz/summary")
    def quiz_summary(
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return quiz_mod.quiz_summary(db, user_id=user["user_id"])

    # ---------- 掌握标记 ----------
    @app.get("/api/v1/mastery/candidates")
    def mastery_candidates(
        q: Optional[str] = Query(None),
        limit: int = 200,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        """可标记的知识点列表（供前端点选，无需手填内部 ID）。"""
        return mastery_mod.list_candidates(db, keyword=q, limit=limit, user_id=user["user_id"])

    @app.get("/api/v1/mastery")
    def mastery_list(
        status: Optional[str] = Query(None),
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return mastery_mod.list_mastery(db, status, user_id=user["user_id"])

    @app.post("/api/v1/mastery")
    def mastery_set(
        req: MasterySetRequest,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return mastery_mod.set_mastery(
            db, req.item_id, req.item_type, req.name, req.status, user_id=user["user_id"]
        )

    @app.delete("/api/v1/mastery")
    def mastery_remove(
        item_id: str = Query(..., min_length=1),
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        return mastery_mod.remove_mastery(db, item_id, user_id=user["user_id"])

    # ---------- 检索对比 ----------
    @app.get("/api/v1/search/compare")
    def search_compare(
        q: str = Query(..., min_length=1),
        top_k: int = 5,
        db: Database = Depends(deps.get_db),
        embedder: BaseEmbedder = Depends(deps.get_embedder),
        settings: Settings = Depends(deps.get_app_settings),
    ):
        from ..retrieval import hybrid as hybrid_mod

        active = version_mod.active_doc_ids(db)
        cmp = hybrid_mod.search_hybrid(db, q, embedder, top_k=top_k, active_doc_ids=active, settings=settings)
        return {
            "question": q,
            "keyword": [_hit_dict(h) for h in cmp.keyword],
            "vector": [_vhit_dict(h) for h in cmp.vector],
            "hybrid": [_hhit_dict(h) for h in cmp.hybrid],
        }

    # ---------- 时效 ----------
    @app.get("/api/v1/versioning")
    def versioning_report(db: Database = Depends(deps.get_db)):
        return version_mod.validity_report(db)

    # ---------- 用户与权限 ----------
    @app.post("/api/v1/users")
    def create_user(
        req: UserCreateRequest,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
    ):
        # 首个用户免鉴权（bootstrap），此后仅 admin 可创建 —— 规则在服务层判定
        return users_mod.create_user(db, req.username, req.role, actor=user)

    @app.get("/api/v1/users")
    def list_users(
        user: dict = Depends(deps.require_admin),
        db: Database = Depends(deps.get_db),
    ):
        return users_mod.list_users(db)

    @app.get("/api/v1/me")
    def whoami(user: dict = Depends(deps.get_current_user)):
        return user

    # ---------- 增量入库（老师维护端） ----------
    @app.post("/api/v1/ingest/notice")
    def ingest_notice(
        req: NoticeIngestRequest,
        user: dict = Depends(deps.require_teacher),
        db: Database = Depends(deps.get_db),
    ):
        return ingest_mod.ingest_notice(
            db, req.text, title=req.title, category=req.category, issuer=req.issuer,
            publish_date=req.publish_date, effective_date=req.effective_date,
            version=req.version, applies_to=req.applies_to,
            source_filename=req.source_filename, auto_supersede=req.auto_supersede,
        )

    # ---------- 质量评测 ----------
    @app.post("/api/v1/evaluation/run")
    def run_eval(
        top_k: int = 5,
        user: dict = Depends(deps.require_teacher),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
    ):
        return eval_mod.run_evaluation(db, settings, top_k=top_k)

    @app.get("/api/v1/evaluation/latest")
    def latest_eval(
        user: dict = Depends(deps.require_teacher),
        db: Database = Depends(deps.get_db),
    ):
        return eval_mod.latest_report(db)

    # ---------- 健康/统计 ----------
    @app.get("/api/v1/health")
    def health(
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
    ):
        try:
            empty = db.is_empty()
            status = "ok" if not empty else "empty"
        except StorageUnavailable as exc:
            return JSONResponse(status_code=503, content={"status": "unavailable", "detail": exc.detail})
        return {
            "status": status,
            "version": __version__,
            "llm_configured": settings.llm_configured,
            "llm_model": settings.llm_model,
            "embedding_configured": settings.embedding_enabled and bool(settings.embedding_api_key),
            "local_embedding": _local_embedding_status(settings),
            "local_reranker": _local_reranker_status(settings),
            "vector_count": _vector_count(db),
            "db_path": settings.db_path,
        }

    @app.get("/api/v1/stats")
    def stats(db: Database = Depends(deps.get_db)):
        from ..ingest.stats import check_minimum, collect, coverage_summary

        s = collect(db)
        checks, ok = check_minimum(s)
        return {
            "stats": s,
            "minimum_ok": ok,
            "checks": [c.__dict__ for c in checks],
            "coverage": coverage_summary(db),
        }

    return app


def _vector_count(db: Database) -> int:
    try:
        from ..retrieval.vector import vector_count

        return vector_count(db)
    except Exception:  # noqa: BLE001
        return 0


def _local_embedding_status(settings: Settings) -> str:
    from .. import ml_local

    if settings.use_local_embedding == "off":
        return "off"
    if ml_local.local_embedder_available(settings):
        return "available"
    return "unavailable"


def _local_reranker_status(settings: Settings) -> str:
    from .. import ml_local

    if settings.use_local_reranker == "off":
        return "off"
    if ml_local.local_reranker_available(settings):
        return "available"
    return "unavailable"


def _hit_dict(h) -> dict:
    return {
        "doc_title": h.doc_title,
        "chapter": h.chapter,
        "article_no": h.article_no,
        "content": h.content[:160],
        "score": round(h.score, 4),
        "matched": getattr(h, "matched", None),
    }


def _vhit_dict(h) -> dict:
    return {
        "doc_title": h.doc_title,
        "chapter": h.chapter,
        "article_no": h.article_no,
        "content": h.content[:160],
        "score": round(h.score, 4),
    }


def _hhit_dict(h) -> dict:
    return {
        "doc_title": h.doc_title,
        "chapter": h.chapter,
        "article_no": h.article_no,
        "content": h.content[:160],
        "score": round(h.score, 6),
    }
