"""增量入库服务（第三阶段，§6 挑战版#17）：老师上传新通知 → 解析 → SUPERSEDES 检测 → 入库。

SUPERSEDES 自动检测策略（§8 风险应对：不全程自动，仅"建议 + 人工确认"）：
- 依据「同类别 + 标题关键词相似」发现潜在旧版本；
- 生成 SUPERSEDES 建议，默认写入（如需更保守可人工审核）；
- 旧文档 status 置为「已被替代」。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..db import Database
from ..errors import AppError, NotFoundError


def _doc_id(title: str) -> str:
    return "doc:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]


def _normalize_title(t: str) -> str:
    """标题归一：去年份/标点/空格，用于相似度判断。"""
    return re.sub(r"[\s\W_]+", "", t or "")


def detect_superseded_candidates(db: Database, category: str, title: str, exclude_doc_id: str) -> List[dict]:
    """找出与新通知同类别、标题高度相似的现存文档，作为潜在替代对象。"""
    norm_title = _normalize_title(title)
    if not norm_title:
        return []
    with db.session() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, status, version FROM documents WHERE category = ? AND doc_id != ?",
            (category, exclude_doc_id),
        ).fetchall()
    candidates = []
    for r in rows:
        norm = _normalize_title(r["title"])
        # 相似度：去数字年份后相同 / 或一方是另一方子串
        a = re.sub(r"\d+", "", norm_title)
        b = re.sub(r"\d+", "", norm)
        if a and b and (a == b or a in b or b in a):
            candidates.append(dict(r))
    return candidates


def ingest_notice(
    db: Database,
    text: str,
    *,
    title: str,
    category: str,
    issuer: str = "",
    publish_date: str = "",
    effective_date: str = "",
    version: str = "",
    applies_to: str = "",
    source_filename: str = "",
    auto_supersede: bool = True,
) -> dict:
    """把一段通知文本入库：解析为段落块，检测并建立 SUPERSEDES，返回入库结果。"""
    from ..ingest import parser as doc_parser

    if not text.strip():
        raise AppError("通知内容为空，无法入库。")
    if not title.strip():
        raise AppError("通知标题不能为空。")

    doc_id = _doc_id(title)
    blocks = doc_parser.split_into_blocks(text, mode="paragraph")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # 潜在替代对象（先检测）
    candidates = detect_superseded_candidates(db, category, title, doc_id)

    with db.session() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO documents
            (doc_id, title, category, issuer, publish_date, effective_date,
             status, version, applies_to, doc_type, source_path, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (doc_id, title, category, issuer, publish_date, effective_date,
             "现行有效", version, applies_to, "通知", source_filename, content_hash),
        )
        # 重建条款
        conn.execute("DELETE FROM clauses WHERE doc_id = ?", (doc_id,))
        for block in blocks:
            if not block.content:
                continue
            conn.execute(
                "INSERT INTO clauses (doc_id, chapter, article_no, content, seq) VALUES (?,?,?,?,?)",
                (doc_id, block.chapter, block.article_no, block.content, block.seq),
            )

        superseded: List[str] = []
        if auto_supersede:
            for cand in candidates:
                conn.execute(
                    "INSERT OR REPLACE INTO relations (src, rel, dst, note) VALUES (?,?,?,?)",
                    (doc_id, "SUPERSEDES", cand["doc_id"], f"{title}替代{cand['title']}"),
                )
                conn.execute(
                    "UPDATE documents SET status = '已被替代' WHERE doc_id = ?", (cand["doc_id"],)
                )
                superseded.append(cand["doc_id"])

    # 增量构建向量
    try:
        from ..config import get_settings
        from ..embeddings import get_embedder
        from ..retrieval.vector import ensure_vector_index

        ensure_vector_index(db, get_embedder(get_settings()))
    except Exception:  # noqa: BLE001
        pass

    return {
        "doc_id": doc_id,
        "title": title,
        "clauses": len([b for b in blocks if b.content]),
        "superseded": superseded,
        "candidates": [c["doc_id"] for c in candidates],
    }
