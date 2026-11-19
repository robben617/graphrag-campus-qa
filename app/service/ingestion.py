"""增量入库服务（第三阶段，§6 挑战版#17）：老师上传新通知 → 解析 → SUPERSEDES 检测 → 入库。

SUPERSEDES 自动检测策略（§8 风险应对：不全程自动，仅"建议 + 人工确认"）：
- 依据「同类别 + 标题关键词相似」发现潜在旧版本；
- 标题一致且顺序可判定时，将新版本指向旧版本；
- 到生效日才应用替代关系。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from sqlite3 import Connection
from typing import List, Optional

from ..db import Database
from ..errors import AppError
from . import versioning

YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _doc_id(title: str) -> str:
    return "doc:" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]


def _normalize_title(t: str) -> str:
    """标题归一：去年份/标点/空格，用于相似度判断。"""
    return re.sub(r"[\s\W_]+", "", YEAR.sub("", t))


def detect_superseded_candidates(db: Database, category: str, title: str, exclude_doc_id: str) -> List[dict]:
    """找出与新通知同类别、标题高度相似的现存文档，作为潜在替代对象。"""
    with db.session() as conn:
        return _find_candidates(conn, category, title, exclude_doc_id)


def _find_candidates(conn: Connection, category: str, title: str, exclude_doc_id: str) -> List[dict]:
    norm_title = _normalize_title(title)
    if not norm_title:
        return []
    rows = conn.execute(
        """
        SELECT doc_id, title, status, version, publish_date, effective_date, issuer, applies_to
        FROM documents WHERE category = ? AND doc_id != ? ORDER BY doc_id
        """,
        (category, exclude_doc_id),
    ).fetchall()
    candidates = []
    for r in rows:
        norm = _normalize_title(r["title"])
        if norm and (norm_title in norm or norm in norm_title):
            candidates.append(dict(r))
    return candidates


def _revision(doc: dict) -> tuple[int, ...]:
    for value in (doc["effective_date"], doc["publish_date"]):
        if value:
            try:
                day = date.fromisoformat(value)
                return day.year, day.month, day.day
            except ValueError:
                continue
    year = YEAR.search(doc["version"]) or YEAR.search(doc["title"])
    return (int(year[0]),) if year else ()


def _write_replacement(conn: Connection, newer: dict, older: dict, today: str) -> bool:
    conn.execute(
        "DELETE FROM relations WHERE src = ? AND rel = 'SUPERSEDES' AND dst = ?",
        (older["doc_id"], newer["doc_id"]),
    )
    conn.execute(
        "INSERT OR REPLACE INTO relations (src, rel, dst, note) VALUES (?, 'SUPERSEDES', ?, ?)",
        (newer["doc_id"], older["doc_id"], f"{newer['title']}替代{older['title']}"),
    )
    effective = newer["effective_date"] <= today
    if effective:
        conn.execute(
            "UPDATE documents SET status = '已被替代' WHERE doc_id = ? AND status = '现行有效'",
            (older["doc_id"],),
        )
    return effective


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
    for value in (publish_date, effective_date):
        if value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError(value)
            except ValueError as exc:
                raise AppError("发布日期和生效日期必须采用 YYYY-MM-DD 格式。") from exc

    doc_id = _doc_id(title)
    effective_date = effective_date or publish_date
    blocks = doc_parser.split_into_blocks(text, mode="paragraph")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    notice = dict(doc_id=doc_id, title=title, publish_date=publish_date, effective_date=effective_date, version=version)
    revision = _revision(notice)
    today = versioning._today()
    superseded = []
    scheduled = []
    archived_by = []

    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidates = _find_candidates(conn, category, title, doc_id)
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

        if auto_supersede:
            for cand in candidates:
                if (
                    _normalize_title(title) != _normalize_title(cand["title"])
                    or (issuer and cand["issuer"] and issuer != cand["issuer"])
                    or (applies_to and cand["applies_to"] and applies_to != cand["applies_to"])
                ):
                    continue
                current = _revision(cand)
                if not revision or not current or revision == current:
                    continue
                if revision[0] == current[0] and len(revision) != len(current):
                    continue
                if revision > current:
                    effective = _write_replacement(conn, notice, cand, today)
                    (superseded if effective else scheduled).append(cand["doc_id"])
                elif cand["status"] in ("现行有效", "已被替代"):
                    if _write_replacement(conn, cand, notice, today):
                        archived_by.append(cand["doc_id"])

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
        "scheduled_supersedes": scheduled,
        "archived_by": archived_by,
        "candidates": [c["doc_id"] for c in candidates],
    }
