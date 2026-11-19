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
# 可自助注册的角色。老师与管理员不开放自助，必须由管理员注入名单——
# 否则「老师」这个身份在校内场景里没有任何背书，谁都能给自己发一个。
SELF_SERVICE_ROLES = {"student"}
DEFAULT_USER = "anonymous"
MAX_USERNAME_LEN = 64


def _token() -> str:
    return secrets.token_hex(16)


def user_count(db: Database) -> int:
    with db.session() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])


def _normalize_username(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise AppError("用户名不能为空")
    if len(name) > MAX_USERNAME_LEN:
        raise AppError(f"用户名过长（最多 {MAX_USERNAME_LEN} 个字符）")
    return name


def create_user(db: Database, username: str, role: str = "student", *, actor: Optional[dict] = None) -> dict:
    """创建用户。

    规则（按优先级）：
    1. **bootstrap**：系统内一个用户都没有时，允许创建任意角色的首个账号——
       否则空库永远产生不了第一个管理员，形成死锁。
    2. **admin**：管理员可创建任意角色，教师 / 管理员名单由这条路注入。
    3. **自助**：其余调用方（匿名访客、学生）只能创建 student。
       老师与管理员不开放自助注册，避免「谁都能给自己发一个老师身份」。

    actor 为调用方身份（由路由注入 get_current_user 得到，匿名用户为 role=student）。
    """
    if role not in ROLES:
        raise AppError(f"角色必须为 {'/'.join(sorted(ROLES))}")
    username = _normalize_username(username)
    uid = hashlib.sha1(username.encode("utf-8")).hexdigest()[:16]
    token = _token()
    with db.session() as conn:
        total = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
        exists = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise AppError(f"用户名已存在: {username}")
        if total > 0:
            actor_role = (actor or {}).get("role", DEFAULT_USER)
            if actor_role != "admin" and role not in SELF_SERVICE_ROLES:
                raise ForbiddenError(
                    "权限不足：学生只能创建学生身份；老师与管理员账号由管理员统一导入名单",
                    detail=f"当前角色: {actor_role}",
                )
        conn.execute(
            "INSERT INTO users (user_id, username, role, api_token) VALUES (?,?,?,?)",
            (uid, username, role, token),
        )
    return {"user_id": uid, "username": username, "role": role, "api_token": token}


def create_users_bulk(
    db: Database,
    entries: List[dict],
    *,
    actor: Optional[dict] = None,
    dry_run: bool = False,
) -> dict:
    """管理员批量注入名单（老师 / 管理员 / 学生都可）。

    逐条处理而不整体回滚：一份上百行的名单里有一行格式错误，
    不应该让其余 99 个人都进不来。因此返回三分明细，由管理员按明细修正后重跑——
    重复提交是安全的：已存在的用户名会落到 skipped，不会产生重复账号。

    dry_run=True 时只做校验与去重，不写库，用于导入前的预览。
    """
    if not actor or actor.get("role") != "admin":
        raise ForbiddenError(
            "权限不足：导入名单需要 admin 角色",
            detail=f"当前角色: {(actor or {}).get('role', DEFAULT_USER)}",
        )

    created: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    seen = set()

    # 一次性取出已存在的用户名，避免逐条查库；也让「重复行」与「已存在」的判定一致。
    names = []
    for e in entries:
        name = (e.get("username") or "").strip()
        if name:
            names.append(name)
    existing = set()
    if names:
        placeholders = ",".join("?" for _ in names)
        with db.session() as conn:
            rows = conn.execute(
                f"SELECT username FROM users WHERE username IN ({placeholders})", tuple(names)
            ).fetchall()
        existing = {r["username"] for r in rows}

    for idx, entry in enumerate(entries, start=1):
        name = (entry.get("username") or "").strip()
        role = (entry.get("role") or "student").strip() or "student"
        if not name:
            failed.append({"line": idx, "username": "", "reason": "用户名为空"})
            continue
        if len(name) > MAX_USERNAME_LEN:
            failed.append({"line": idx, "username": name, "reason": f"用户名超过 {MAX_USERNAME_LEN} 字符"})
            continue
        if role not in ROLES:
            failed.append({"line": idx, "username": name, "reason": f"角色必须为 {'/'.join(sorted(ROLES))}"})
            continue
        if name in seen:
            skipped.append({"line": idx, "username": name, "reason": "本次名单内重复"})
            continue
        if name in existing:
            seen.add(name)
            skipped.append({"line": idx, "username": name, "reason": "用户名已存在"})
            continue
        seen.add(name)
        if dry_run:
            created.append({"line": idx, "username": name, "role": role, "api_token": ""})
            continue
        try:
            created.append({**create_user(db, name, role, actor={"role": "admin"}), "line": idx})
        except AppError as exc:  # 并发下仍可能撞名/撞角色，按失败处理而不是中断整批
            failed.append({"line": idx, "username": name, "reason": exc.message})

    return {
        "dry_run": dry_run,
        "total": len(entries),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "summary": {
            "created": len(created),
            "skipped": len(skipped),
            "failed": len(failed),
        },
    }


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
