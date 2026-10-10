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

from ..config import Settings
from ..db import Database
from ..embeddings import BaseEmbedder
from ..service import users as users_mod
from ..service.conversation import ConversationService
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


# ============ 请求级依赖：身份与权限 ============


def get_current_user(request: Request, db: Database = Depends(get_db)) -> dict:
    """从 X-User-Token 请求头解析当前用户，未提供令牌则为匿名用户。

    注意这里演示了「依赖嵌套」：get_current_user 自身依赖 get_db。
    """
    token = request.headers.get("X-User-Token")
    return users_mod.resolve_user(db, token)


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
