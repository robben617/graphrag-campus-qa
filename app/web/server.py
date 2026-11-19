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
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import PROJECT_ROOT, Settings
from ..crypto import SecretBox, build_secret_box
from ..db import Database
from ..embeddings import BaseEmbedder, get_embedder
from ..errors import AppError, ForbiddenError, InternalError, StorageUnavailable, UserInputError
from ..logging_util import log_event, setup_logging
from ..service import evaluation as eval_mod
from ..service import graph as graph_mod
from ..service import ingestion as ingest_mod
from ..service import llm_config as llm_cfg_mod
from ..service import llm_quota as quota_mod
from ..service import mastery as mastery_mod
from ..service import paths as paths_mod
from ..service import quiz as quiz_mod
from ..service import users as users_mod
from ..service import versioning as version_mod
from ..service.conversation import ConversationService
from ..service.llm import LLMClient
from ..service.qa import QAService
from . import deps
from .dify import router as dify_router

logger = logging.getLogger("school_agent.web")


class AskRequest(BaseModel):
    question: str = Field(..., description="提问内容")
    # 校验交给 QAService，避免 pydantic 422 与统一 400 错误不一致
    mode: str = Field(
        "auto",
        description="回答方式：auto（按部署形态）/ extractive（原文摘编，零成本）/ llm（大模型生成）",
    )


class SessionAskRequest(BaseModel):
    question: str = Field(..., description="追问内容")
    mode: str = Field("auto", description="回答方式：auto / extractive / llm")


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


class BulkUserItem(BaseModel):
    username: str = Field(..., description="用户名")
    role: str = Field("student", description="student | teacher | admin")


class BulkUsersRequest(BaseModel):
    """管理员批量注入名单。

    逐条处理、返回三分明细（created / skipped / failed），
    dry_run=true 只做校验与去重、不写库，用于导入前预览。
    """

    users: List[BulkUserItem] = Field(..., description="名单条目")
    dry_run: bool = Field(False, description="只预览不写库")


class LLMConfigRequest(BaseModel):
    """用户自带大模型配置。

    api_key 留空表示沿用已保存的 key（便于只改模型或地址）；
    base_url / model 留空则使用服务端默认值。
    """

    api_key: str = Field("", description="大模型 API Key（明文仅本请求体内传输）")
    base_url: str = Field("", description="OpenAI 兼容地址，如 https://api.deepseek.com/v1")
    model: str = Field("", description="模型名，如 deepseek-chat")
    enabled: bool = Field(True, description="是否启用该配置")


_ANSWER_MODES = {"auto", "extractive", "llm"}


def resolve_answer_mode(requested: Optional[str], settings: Settings) -> str:
    """把请求里的 mode 解析成最终要执行的方式。

    auto 交给部署形态决定：public 默认 extractive（零成本），selfhost 默认 llm。
    """
    mode = (requested or "auto").strip().lower()
    if mode not in _ANSWER_MODES:
        raise UserInputError("回答方式（mode）只能是 auto / extractive / llm")
    if mode == "auto":
        return settings.default_answer_mode
    return mode


@dataclass
class AnswerPlan:
    """本次请求的执行计划：用哪个客户端、要不要生成、能不能引导「增强」。"""

    llm: LLMClient
    generate: bool
    notice: Optional[str] = None
    enhance: bool = False


def plan_answer(
    settings: Settings,
    db: Database,
    user_id: str,
    llm: LLMClient,
    requested_mode: Optional[str],
) -> AnswerPlan:
    """决定这次到底要不要花钱调大模型。

    三道闸门按顺序生效，任一道不过就退回零成本的原文摘编——
    摘编对用户仍是完整可用的答案，因此这道"拒绝"不是错误，而是降级：
    1. 用户选的方式（auto 时按部署形态推导）；
    2. 是否真的存在可用密钥；
    3. 公开部署下还有没有每日额度（只在服务器出钱时才计）。
    """
    mode = resolve_answer_mode(requested_mode, settings)

    if mode != "llm":
        # 摘编模式：换掉客户端而不是加一个 if，这样多轮改写也会自动走零成本分支
        return AnswerPlan(deps.disabled_llm(settings), False, None, llm.available)

    if not llm.available:
        return AnswerPlan(
            deps.disabled_llm(settings),
            False,
            "当前没有可用的大模型密钥，已返回知识库原文摘编。",
            False,
        )

    if settings.is_public:
        # 匿名访客共用一个 user_id，若按人计额会把全站几十个人压到同一份额度里；
        # 匿名只受全站额度约束，登录身份才单独计人。
        per_user = 0 if user_id == users_mod.DEFAULT_USER else settings.public_llm_daily_per_user
        verdict = quota_mod.consume(
            db,
            user_id=user_id,
            per_user_limit=per_user,
            total_limit=settings.public_llm_daily_total,
        )
        if not verdict.allowed:
            return AnswerPlan(deps.disabled_llm(settings), False, verdict.message, False)

    return AnswerPlan(llm, True, None, False)


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
    # 用户密钥的加解密器：主密钥来自 LLM_CONFIG_SECRET，缺省时落 data/.llm_secret
    app.state.secret_box = build_secret_box(
        settings.llm_config_secret, secret_dir=str(Path(settings.db_path).parent)
    )
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
        user: dict = Depends(deps.get_current_user),
        qa: QAService = Depends(deps.get_qa),
        llm: LLMClient = Depends(deps.get_llm_for_user),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
    ):
        start = time.perf_counter()
        plan = plan_answer(settings, db, user["user_id"], llm, req.mode)
        ans = qa.ask(
            req.question,
            llm=plan.llm,
            generate=plan.generate,
            extractive_notice=plan.notice,
            enhance_allowed=plan.enhance,
        )
        elapsed = int((time.perf_counter() - start) * 1000)
        log_event(
            logger, "ask", request_id=request.state.request_id,
            elapsed_ms=elapsed, mode=ans.mode, hits=len(ans.citations), degraded=ans.degraded,
            generated=plan.generate,
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
        llm: LLMClient = Depends(deps.get_llm_for_user),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
    ):
        start = time.perf_counter()
        conv.get(session_id, user_id=user["user_id"])  # 校验存在
        history = conv.history(session_id)
        plan = plan_answer(settings, db, user["user_id"], llm, req.mode)
        # 指代追问改写：摘编模式下 plan.llm 是不可用客户端，会自动走零成本启发式
        rewritten = conv.rewrite_question(req.question, history, llm=plan.llm)
        ans = qa.ask(
            rewritten,
            history=history,
            llm=plan.llm,
            generate=plan.generate,
            extractive_notice=plan.notice,
            enhance_allowed=plan.enhance,
        )
        # 记录消息（citations 转 dict 以便 JSON 持久化）
        conv.add_message(session_id, "user", req.question)
        conv.add_message(session_id, "assistant", ans.answer, citations=[c.to_dict() for c in ans.citations])
        elapsed = int((time.perf_counter() - start) * 1000)
        log_event(
            logger, "session_ask", request_id=request.state.request_id,
            session_id=session_id, elapsed_ms=elapsed, mode=ans.mode,
            hits=len(ans.citations), degraded=ans.degraded, generated=plan.generate,
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

    @app.post("/api/v1/users/bulk")
    def create_users_bulk(
        req: BulkUsersRequest,
        request: Request,
        user: dict = Depends(deps.require_admin),
        db: Database = Depends(deps.get_db),
    ):
        """管理员批量注入名单（老师 / 管理员 / 学生）。

        逐条处理而不是整批原子提交：一行格式错误不该让其余 99 人进不来。
        返回 created / skipped / failed 三分明细，管理员按明细修正后重跑即可（幂等）。
        """
        result = users_mod.create_users_bulk(
            db,
            [{"username": item.username, "role": item.role} for item in req.users],
            actor=user,
            dry_run=req.dry_run,
        )
        log_event(
            logger, "users_bulk_import", request_id=request.state.request_id,
            actor=user["user_id"], dry_run=req.dry_run,
            # 不要把 summary 直接 ** 展开：created 与 LogRecord 内置属性同名，
            # logging 会抛 KeyError（现在 log_event 也做了兜底，但不该靠它）
            n_created=result["summary"]["created"],
            n_skipped=result["summary"]["skipped"],
            n_failed=result["summary"]["failed"],
        )
        return result

    @app.get("/api/v1/users")
    def list_users(
        user: dict = Depends(deps.require_admin),
        db: Database = Depends(deps.get_db),
    ):
        return users_mod.list_users(db)

    @app.get("/api/v1/me")
    def whoami(user: dict = Depends(deps.get_current_user)):
        return user

    # ---------- 大模型密钥（用户自带 Key，BYOK） ----------
    def _require_identity(user: dict) -> dict:
        """配置按用户隔离，匿名身份无处可存，直接拒绝而不是静默丢弃。"""
        if user["user_id"] == users_mod.DEFAULT_USER:
            raise ForbiddenError(
                "请先创建或登录身份，再配置大模型 Key",
                detail="anonymous",
            )
        return user

    def _require_byok_enabled(settings: Settings) -> None:
        """公开部署下不采集用户密钥。

        能力没有删除（自部署者仍需要），只是对外站点关闭写入口：
        让陌生访客在别人的站点上粘贴自己的 Key，无论加密做得多好，
        都是在要求用户先付出信任，而这一步通常直接把人劝走。
        """
        if not settings.byok_allowed:
            raise ForbiddenError(
                "当前为公开演示站点，未开放「自带密钥」。直接提问即可；"
                "如需使用自己的模型，请把项目部署到你自己的机器上（SCHOOL_DEPLOY_MODE=selfhost）。",
                detail="byok_disabled",
            )

    @app.get("/api/v1/llm/default")
    def get_default_llm(
        user: dict = Depends(deps.require_admin),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
        box: SecretBox = Depends(deps.get_secret_box),
    ):
        """管理员查看「所有人没选模型时用哪个」。只回掩码，不回明文 key。"""
        return llm_cfg_mod.system_public_view(
            settings, llm_cfg_mod.load_system_config(db, box)
        )

    @app.put("/api/v1/llm/default")
    def put_default_llm(
        req: LLMConfigRequest,
        request: Request,
        user: dict = Depends(deps.require_admin),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
        box: SecretBox = Depends(deps.get_secret_box),
    ):
        """设置系统默认模型。

        与用户 BYOK 的区别：这张表是全局唯一的「运营方配置」，改完下一次请求即生效，
        不需要改 .env、不需要重启——换模型属于运营动作，不该要求停机。
        """
        cfg = llm_cfg_mod.save_system_config(
            db, box, settings,
            api_key=req.api_key, base_url=req.base_url, model=req.model,
            actor_id=user["user_id"],
        )
        log_event(
            logger, "system_llm_saved", request_id=request.state.request_id,
            actor=user["user_id"], base_url=cfg.base_url, model=cfg.model,
            key_hint=cfg.hint,
        )
        return llm_cfg_mod.system_public_view(settings, cfg)

    @app.delete("/api/v1/llm/default")
    def delete_default_llm(
        request: Request,
        user: dict = Depends(deps.require_admin),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
        box: SecretBox = Depends(deps.get_secret_box),
    ):
        """清除数据库里的默认模型，落回 .env（不是把系统变成不可用）。"""
        removed = llm_cfg_mod.delete_system_config(db)
        log_event(
            logger, "system_llm_deleted", request_id=request.state.request_id,
            actor=user["user_id"], removed=removed,
        )
        return {
            "deleted": removed,
            **llm_cfg_mod.system_public_view(
                settings, llm_cfg_mod.load_system_config(db, box)
            ),
        }

    @app.get("/api/v1/llm/config")
    def get_llm_config(
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
        box: SecretBox = Depends(deps.get_secret_box),
    ):
        cfg = None
        if user["user_id"] != users_mod.DEFAULT_USER:
            cfg = llm_cfg_mod.load_config(db, box, user["user_id"])
        return llm_cfg_mod.public_view(
            settings, cfg, server_available=settings.llm_configured
        )

    @app.put("/api/v1/llm/config")
    def put_llm_config(
        req: LLMConfigRequest,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
        box: SecretBox = Depends(deps.get_secret_box),
    ):
        _require_byok_enabled(settings)
        _require_identity(user)
        cfg = llm_cfg_mod.save_config(
            db, box, settings, user["user_id"],
            api_key=req.api_key, base_url=req.base_url, model=req.model, enabled=req.enabled,
        )
        # 日志只记 hint（首尾各几位），绝不写明文 key
        log_event(
            logger, "llm_config_saved", user_id=user["user_id"],
            base_url=cfg.base_url, model=cfg.model, key_hint=cfg.hint, enabled=cfg.enabled,
        )
        return llm_cfg_mod.public_view(
            settings, cfg, server_available=settings.llm_configured
        )

    @app.delete("/api/v1/llm/config")
    def delete_llm_config(
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
    ):
        _require_byok_enabled(settings)
        _require_identity(user)
        removed = llm_cfg_mod.delete_config(db, user["user_id"])
        log_event(logger, "llm_config_deleted", user_id=user["user_id"], removed=removed)
        return {"deleted": removed}

    @app.post("/api/v1/llm/config/test")
    def test_llm_config(
        req: LLMConfigRequest,
        user: dict = Depends(deps.get_current_user),
        db: Database = Depends(deps.get_db),
        settings: Settings = Depends(deps.get_app_settings),
        box: SecretBox = Depends(deps.get_secret_box),
    ):
        """测试连通性：可带未保存的临时 key 试，也可以测已保存的那份。"""
        _require_byok_enabled(settings)
        _require_identity(user)
        if req.api_key.strip():
            cfg = llm_cfg_mod.build_transient_config(
                settings, api_key=req.api_key, base_url=req.base_url, model=req.model
            )
        else:
            cfg = llm_cfg_mod.load_config(db, box, user["user_id"])
            if not cfg or not cfg.api_key:
                raise UserInputError("请先填写 API Key，或先保存配置再测试")
        result = llm_cfg_mod.probe(settings, cfg, user_id=user["user_id"])
        log_event(
            logger, "llm_config_tested", user_id=user["user_id"],
            ok=result["ok"], latency_ms=result["latency_ms"], model=result["model"],
        )
        return result

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
    @app.get("/api/v1/ready")
    def ready(db: Database = Depends(deps.get_db)):
        with db.session() as conn:
            has_clauses = conn.execute("SELECT 1 FROM clauses LIMIT 1").fetchone()
        if not has_clauses:
            return JSONResponse(status_code=503, content={"status": "empty"})
        return {"status": "ready"}

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
            "deploy_mode": settings.deploy_mode,
            "byok_allowed": settings.byok_allowed,
            "default_answer_mode": settings.default_answer_mode,
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

    if settings.serve_frontend:
        app.mount("/", StaticFiles(directory=PROJECT_ROOT / "frontend", html=True), name="frontend")

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
