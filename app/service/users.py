"""多用户与权限服务（第三阶段，§6 挑战版#15、§9 用户隔离/权限控制）。

角色：
- student：学生，只能查看/使用自己的会话、掌握、答题记录
- teacher：老师，可增量入库（上传通知）与查看评测报告
- admin：管理员，全部权限 + 用户管理

鉴权：极简 API Token（X-User-Token 请求头），用户表存 token。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import List, Optional

from ..db import Database
from ..errors import AppError, ForbiddenError

ROLES = {"student", "teacher", "admin"}
DEFAULT_USER = "anonymous"


def _token() -> str:
    return secrets.token_hex(16)


def user_count(db: Database) -> int:
    with db.session() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])


def create_user(db: Database, username: str, role: str = "student", *, actor: Optional[dict] = None) -> dict:
    """创建用户。

    鉴权规则（bootstrap）：系统内**一个用户都没有**时，允许任何人创建首个账号——
    否则空库永远无法产生第一个管理员，形成死锁。此后创建用户必须由 admin 发起。

    actor 为调用方身份（由路由注入 get_current_user 得到，匿名用户为 role=student）。
    """
    if role not in ROLES:
        raise AppError(f"角色必须为 {'/'.join(sorted(ROLES))}")
    uid = hashlib.sha1(username.encode("utf-8")).hexdigest()[:16]
    token = _token()
    with db.session() as conn:
        total = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
        exists = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise AppError(f"用户名已存在: {username}")
        if total > 0 and (actor is None or actor.get("role") != "admin"):
            raise ForbiddenError(
                "权限不足：创建用户需要 admin 角色（仅系统内首个用户可免鉴权创建）",
                detail=f"当前角色: {(actor or {}).get('role', DEFAULT_USER)}",
            )
        conn.execute(
            "INSERT INTO users (user_id, username, role, api_token) VALUES (?,?,?,?)",
            (uid, username, role, token),
        )
    return {"user_id": uid, "username": username, "role": role, "api_token": token}


def list_users(db: Database) -> List[dict]:
    with db.session() as conn:
        rows = conn.execute("SELECT user_id, username, role, created_at FROM users ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def resolve_user(db: Database, token: Optional[str], user_id: Optional[str] = None) -> dict:
    """根据 token（或显式 user_id）解析用户；未提供则返回匿名用户。"""
    if token:
        with db.session() as conn:
            row = conn.execute("SELECT user_id, username, role FROM users WHERE api_token = ?", (token,)).fetchone()
        if row:
            return {"user_id": row["user_id"], "username": row["username"], "role": row["role"]}
    if user_id:
        with db.session() as conn:
            row = conn.execute("SELECT user_id, username, role FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return {"user_id": row["user_id"], "username": row["username"], "role": row["role"]}
    return {"user_id": DEFAULT_USER, "username": "匿名用户", "role": "student"}


def require_role(user: dict, roles: set) -> None:
    if user["role"] not in roles:
        raise ForbiddenError(
            "权限不足：该操作需要 " + "/".join(sorted(roles)) + " 角色",
            detail=f"当前角色: {user['role']}",
        )
