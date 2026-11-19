"""时效过滤与 SUPERSEDES 冲突处理（对齐设计文档 §3.3、§6 标准版#8、验收 AT-11）。

规则：
- 默认检索仅命中「现行有效」且 effective_date <= 今天 的文档；
- 存在 SUPERSEDES 关系（新 doc -> 旧 doc）时，被替代文档默认不参与检索；
- 若某文档被更新版本替代，回答时给出提示。
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional

from ..db import Database


def _today() -> str:
    return datetime.date.today().isoformat()


def active_documents(db: Database) -> Dict[str, Dict]:
    """返回所有「参与默认检索」的文档（现行有效 + 已生效 + 未被替代）。"""
    today = _today()
    with db.session() as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            """
            SELECT doc_id, title, category, issuer, publish_date, effective_date,
                   status, version, applies_to, doc_type
            FROM documents
            WHERE status IN ('现行有效', '已被替代')
              AND (effective_date = '' OR effective_date <= ?)
            """,
            (today,),
        ).fetchall()
        replacements = conn.execute(
            """
            SELECT r.dst, successor.effective_date
            FROM relations r JOIN documents successor ON successor.doc_id = r.src
            WHERE r.rel = 'SUPERSEDES' AND successor.status IN ('现行有效', '已被替代')
            """
        ).fetchall()
    superseded = {r["dst"] for r in replacements if r["effective_date"] <= today}
    scheduled = {r["dst"] for r in replacements if r["effective_date"] > today}
    # 兼容旧库：旧版被未来生效的通知提前标记时，保留至替代关系生效。
    return {
        r["doc_id"]: dict(r) for r in rows
        if r["doc_id"] not in superseded and (r["status"] == "现行有效" or r["doc_id"] in scheduled)
    }


def active_doc_ids(db: Database) -> set:
    return set(active_documents(db).keys())


def superseded_by(db: Database, doc_id: str) -> Optional[str]:
    """返回已生效的替代文件 ID。"""
    with db.session() as conn:
        row = conn.execute(
            """
            SELECT r.src FROM relations r JOIN documents successor ON successor.doc_id = r.src
            WHERE r.rel = 'SUPERSEDES' AND r.dst = ?
              AND successor.status IN ('现行有效', '已被替代') AND successor.effective_date <= ?
            ORDER BY successor.effective_date DESC, successor.publish_date DESC, successor.version DESC, r.src
            LIMIT 1
            """,
            (doc_id, _today()),
        ).fetchone()
    return row["src"] if row else None


def validity_report(db: Database) -> Dict:
    """时效全景：现行有效 / 已废止 / 已被替代文档清单。"""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, status, version, effective_date FROM documents ORDER BY doc_id"
        ).fetchall()
    active = active_doc_ids(db)
    report = []
    for r in rows:
        item = dict(r)
        rep = superseded_by(db, r["doc_id"])
        if rep and item["status"] == "现行有效":
            item["status"] = "已被替代"
        elif r["doc_id"] in active:
            item["status"] = "现行有效"
        item["superseded_by"] = rep
        report.append(item)
    return {"documents": report}


def check_conflict(db: Database, doc_ids: List[str]) -> Optional[str]:
    """若命中文档中有被替代版本，返回冲突提示文案，否则 None。"""
    for did in doc_ids:
        rep = superseded_by(db, did)
        if rep:
            return f"注意：检索命中了已失效的旧版本，最新规定见其替代文件。"
    return None
