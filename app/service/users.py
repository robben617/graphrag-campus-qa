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
from ..errors import AppError

ROLES = {"student", "teacher", "admin"}
DEFAULT_USER = "anonymous"


def _token() -> str:
    return secrets.token_hex(16)


def create_user(db: Database, username: str, role: str = "student") -> dict:
    if role not in ROLES:
        raise AppError(f"角色必须为 {'/'.join(sorted(ROLES))}")
    uid = hashlib.sha1(username.encode("utf-8")).hexdigest()[:16]
    token = _token()
    with db.session() as conn:
        exists = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise AppError(f"用户名已存在: {username}")
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
        raise AppError("权限不足：该操作需要 " + "/".join(sorted(roles)) + " 角色", detail=user["role"])
