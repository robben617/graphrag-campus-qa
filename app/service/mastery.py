"""掌握标记服务：条款/知识点标记「已掌握/未掌握」，未掌握项进入问答提示。

对齐设计文档 §6 标准版#11、验收 AT-07。
"""

from __future__ import annotations

from typing import List, Optional

from ..db import Database


def set_mastery(db: Database, item_id: str, item_type: str, name: str, status: str, user_id: str = "anonymous") -> dict:
    if status not in {"mastered", "unmastered"}:
        from ..errors import AppError

        raise AppError("掌握状态只能为 mastered 或 unmastered")
    if item_type not in {"entity", "clause"}:
        from ..errors import AppError

        raise AppError("item_type 只能为 entity 或 clause")
    with db.session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO mastery (item_id, user_id, item_type, name, status, updated_at) "
            "VALUES (?,?,?,?,?, datetime('now','localtime'))",
            (item_id, user_id, item_type, name, status),
        )
    return {"item_id": item_id, "item_type": item_type, "name": name, "status": status, "user_id": user_id}


def list_mastery(db: Database, status: Optional[str] = None, user_id: Optional[str] = None) -> List[dict]:
    sql = "SELECT item_id, item_type, name, status, updated_at, user_id FROM mastery WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY updated_at DESC"
    with db.session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_candidates(
    db: Database,
    keyword: Optional[str] = None,
    limit: int = 200,
    user_id: Optional[str] = None,
) -> List[dict]:
    """列出可标记的知识点（实体），并附带当前用户的掌握状态。

    用于前端"点选式"标记，避免用户手填内部 ID。
    排序：已标记优先，其次是领域对象、流程、部门。
    """
    sql = (
        "SELECT e.eid AS item_id, 'entity' AS item_type, e.name AS name, "
        "e.category AS category, e.kind AS kind, e.description AS description, m.status AS status "
        "FROM entities e "
        "LEFT JOIN mastery m ON m.item_id = e.eid AND m.user_id = ?"
    )
    params: list = [user_id or "anonymous"]
    if keyword:
        sql += " WHERE e.name LIKE ? OR e.description LIKE ?"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    sql += (
        " ORDER BY (m.status IS NULL), "
        "CASE e.kind WHEN 'entity' THEN 0 WHEN 'process' THEN 1 ELSE 2 END, "
        "e.name LIMIT ?"
    )
    params.append(int(limit))
    with db.session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def remove_mastery(db: Database, item_id: str, user_id: str = "anonymous") -> dict:
    """移除某知识点的掌握标记。"""
    with db.session() as conn:
        conn.execute("DELETE FROM mastery WHERE item_id = ? AND user_id = ?", (item_id, user_id))
    return {"removed": item_id, "user_id": user_id}


def get_mastery(db: Database, item_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    with db.session() as conn:
        if user_id:
            row = conn.execute(
                "SELECT item_id, item_type, name, status FROM mastery WHERE item_id = ? AND user_id = ?",
                (item_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT item_id, item_type, name, status FROM mastery WHERE item_id = ?", (item_id,)
            ).fetchone()
    return dict(row) if row else None


def unmastered_names(db: Database, user_id: Optional[str] = None) -> List[str]:
    """未掌握项名称列表，供问答服务拼入提示。"""
    with db.session() as conn:
        if user_id:
            rows = conn.execute("SELECT name FROM mastery WHERE status='unmastered' AND user_id=?", (user_id,)).fetchall()
        else:
            rows = conn.execute("SELECT name FROM mastery WHERE status='unmastered'").fetchall()
    return [r["name"] for r in rows]


def mastery_summary(db: Database, user_id: Optional[str] = None) -> dict:
    with db.session() as conn:
        if user_id:
            mastered = conn.execute("SELECT COUNT(*) FROM mastery WHERE status='mastered' AND user_id=?", (user_id,)).fetchone()[0]
            unmastered = conn.execute("SELECT COUNT(*) FROM mastery WHERE status='unmastered' AND user_id=?", (user_id,)).fetchone()[0]
        else:
            mastered = conn.execute("SELECT COUNT(*) FROM mastery WHERE status='mastered'").fetchone()[0]
            unmastered = conn.execute("SELECT COUNT(*) FROM mastery WHERE status='unmastered'").fetchone()[0]
    return {"mastered": mastered, "unmastered": unmastered}
