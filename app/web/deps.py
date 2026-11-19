"""FastAPI 依赖注入（Dependency Injection）声明。

本项目采用「应用工厂 + Depends」混合策略，原因是依赖的生命周期不同：

1. 应用级重资源（Database、QAService、ConversationService、Embedding/Reranker 模型）
   在 create_app 时创建一次并存入 app.state，通过 Depends 取用。
   这些资源必须全局复用——本地 BGE 模型加载一次约 18 秒，若按请求重建会导致系统不可用。

2. 请求级依赖（当前用户身份、角色权限）用 Depends 声明，写在路由函数签名里，
   好处是：显式可见、可在 Swagger 中体现、可被其他依赖嵌套复用。

使用示例（见 app/web/server.py）：

    @app.post("/api/v1/sessions")
    def create_session(
        req: SessionCreateRequest,
        user: dict = Depends(deps.get_current_user),
        conv: ConversationService = Depends(deps.get_conv),
    ):
        return conv.create(req.title, user_id=user["user_id"]).to_dict()
"""

from __future__ import annotations

from fastapi import Depends, Request

from typing import Optional

from ..config import Settings
from ..crypto import SecretBox
from ..db import Database
from ..embeddings import BaseEmbedder
from ..service import llm_config as llm_config_mod
from ..service import users as users_mod
from ..service.conversation import ConversationService
from ..service.llm import LLMClient, LLMConfig
from ..service.qa import QAService

# ============ 应用级依赖：从 app.state 取全局复用实例 ============


def get_db(request: Request) -> Database:
    """数据库实例（应用启动时创建，全局复用）。"""
    return request.app.state.db


def get_app_settings(request: Request) -> Settings:
    """应用配置。"""
    return request.app.state.settings


def get_qa(request: Request) -> QAService:
    """问答服务（持有 LLM 客户端与 Embedding 模型，必须全局复用）。"""
    return request.app.state.qa


def get_conv(request: Request) -> ConversationService:
    """会话服务（全局复用）。"""
    return request.app.state.conv


def get_embedder(request: Request) -> BaseEmbedder:
    """向量化器（可能持有本地 BGE 模型，必须全局复用）。"""
    return request.app.state.embedder


def get_secret_box(request: Request) -> SecretBox:
    """用户密钥的加解密器（主密钥在应用启动时确定，全局复用）。"""
    return request.app.state.secret_box


# ============ 请求级依赖：身份与权限 ============


def get_current_user(request: Request, db: Database = Depends(get_db)) -> dict:
    """从 X-User-Token 请求头解析当前用户，未提供令牌则为匿名用户。

    注意这里演示了「依赖嵌套」：get_current_user 自身依赖 get_db。
    """
    token = request.headers.get("X-User-Token")
    return users_mod.resolve_user(db, token)


def disabled_llm(settings: Settings) -> LLMClient:
    """一个「不可用」的客户端：调用必然降级为原文摘编。

    用于两种场景：本次请求按策略不该花钱（摘编模式），
    以及额度已用完——把 client.available 置为 False 后，
    多轮改写与生成都会自动走零成本的降级分支，不需要各处再判断开关。
    """
    return LLMClient(
        settings,
        LLMConfig(base_url="", api_key="", model="", timeout=settings.llm_timeout),
    )


def request_llm_config(request: Request, settings: Settings) -> Optional[LLMConfig]:
    """从请求头读取「本次请求临时使用的模型凭据」。

    为什么用请求头而不是请求体：模型凭据属于传输层元信息，不该混进业务参数里
    （业务模型多一个字段，所有构造请求的地方都要跟着改）。更重要的是——
    **服务端不落库**：这三个头只在本次请求的内存里存在，用完即弃。

    仅在 selfhost 形态生效。公开站点（public）不采集任何用户密钥，
    即使有人手工塞了这几个头也一律忽略，避免「说好不收集，却能收」的口径分裂。
    """
    if not settings.byok_allowed:
        return None
    api_key = (request.headers.get("X-LLM-Key") or "").strip()
    if not api_key:
        return None
    base_url = (request.headers.get("X-LLM-Base-Url") or "").strip()
    model = (request.headers.get("X-LLM-Model") or "").strip()
    cfg = llm_config_mod.build_transient_config(
        settings, api_key=api_key, base_url=base_url, model=model
    )
    return LLMConfig(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        timeout=settings.llm_timeout,
    )


def get_llm_for_user(request: Request, user: dict = Depends(get_current_user)) -> LLMClient:
    """解析本次请求该用哪把大模型密钥。

    优先级（前者优先）：
    1. 请求头里的临时凭据 —— 聊天窗口「自定义模型」，密钥只在浏览器，服务端不存；
    2. 用户存在库里的个人配置 —— BYOK 旧路径，自部署者仍可用；
    3. 管理员在库里设置的默认模型 —— 改完立即生效，不用重启；
    4. 服务端 .env 配置（需 allow_server_llm_fallback=true）；
    5. 都没有 → 不可用客户端，QA 层自动降级为原文摘编。

    这里不做缓存：SQLite 单行读取 + Fernet 解密都在微秒级，
    而缓存会引入"用户改了 key 却还在用旧 key"的一致性问题，不划算。
    """
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    box: SecretBox = request.app.state.secret_box

    # 1. 请求级临时凭据（仅 selfhost）
    transient = request_llm_config(request, settings)
    if transient is not None:
        return LLMClient(settings, transient)

    # 2. 用户自己的配置
    cfg = llm_config_mod.load_config(db, box, user["user_id"])
    if cfg and cfg.enabled and cfg.api_key:
        return cfg.to_client(settings)

    # 3. 管理员设置的默认模型
    system_cfg = llm_config_mod.load_system_config(db, box)
    if system_cfg:
        if system_cfg.api_key:
            return system_cfg.to_client(settings)
        # 只改了模型名 / 地址，key 仍用 .env 的；此时必须尊重 LLM_ENABLED
        if settings.llm_configured:
            return LLMClient(
                settings,
                LLMConfig(
                    base_url=system_cfg.base_url or settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=system_cfg.model or settings.llm_model,
                    timeout=settings.llm_timeout,
                ),
            )

    # 4. 服务端兜底
    if settings.allow_server_llm_fallback:
        return LLMClient(settings)

    # 5. 不可用 → 摘编
    return disabled_llm(settings)


def require_roles(*roles: str):
    """依赖工厂：生成一个「要求用户具备指定角色之一」的校验依赖。

    用法：
        @app.post("/api/v1/ingest/notice")
        def ingest_notice(user: dict = Depends(require_teacher)): ...
    """

    def _checker(user: dict = Depends(get_current_user)) -> dict:
        users_mod.require_role(user, set(roles))
        return user

    return _checker


# 预置权限依赖
require_teacher = require_roles("teacher", "admin")
require_admin = require_roles("admin")
