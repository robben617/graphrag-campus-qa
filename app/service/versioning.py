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
        rows = conn.execute(
            """
            SELECT doc_id, title, category, issuer, publish_date, effective_date,
                   status, version, applies_to, doc_type
            FROM documents
            WHERE status = '现行有效'
              AND (effective_date = '' OR effective_date <= ?)
            """,
            (today,),
        ).fetchall()
        superseded = {
            r["dst"] for r in conn.execute("SELECT dst FROM relations WHERE rel = 'SUPERSEDES'").fetchall()
        }
    return {r["doc_id"]: dict(r) for r in rows if r["doc_id"] not in superseded}


def active_doc_ids(db: Database) -> set:
    return set(active_documents(db).keys())


def superseded_by(db: Database, doc_id: str) -> Optional[str]:
    """若 doc 被某新文档替代，返回替代者 doc_id，否则 None。"""
    with db.session() as conn:
        row = conn.execute(
            "SELECT src FROM relations WHERE rel='SUPERSEDES' AND dst = ?", (doc_id,)
        ).fetchone()
    return row["src"] if row else None


def validity_report(db: Database) -> Dict:
    """时效全景：现行有效 / 已废止 / 已被替代文档清单。"""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, status, version, effective_date FROM documents ORDER BY doc_id"
        ).fetchall()
    report = []
    for r in rows:
        item = dict(r)
        rep = superseded_by(db, r["doc_id"])
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
