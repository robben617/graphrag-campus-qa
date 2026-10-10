"""向量检索：对 clauses 做 Embedding 相似度检索（离线哈希向量器兜底）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from ..db import Database
from ..embeddings import BaseEmbedder, cosine_similarity


@dataclass
class VectorHit:
    clause_id: int
    doc_id: str
    doc_title: str
    chapter: str
    article_no: str
    content: str
    score: float
    source_path: str = ""

    def citation(self) -> str:
        loc = f"第{self.article_no}条" if self.article_no else ""
        if self.chapter and loc:
            loc = f"{self.chapter} {loc}"
        elif self.chapter:
            loc = self.chapter
        return f"《{self.doc_title}》{loc}".rstrip("》")

    def fingerprint(self) -> str:
        import hashlib

        h = hashlib.md5(self.content.encode("utf-8")).hexdigest()[:8]
        return f"{self.doc_id}|{self.chapter}|{self.article_no}|{h}"


@dataclass
class VectorResult:
    hits: List[VectorHit]


def _current_dim(db: Database) -> int:
    """返回向量表中现有向量的维度；无向量返回 0。"""
    with db.session() as conn:
        row = conn.execute("SELECT dim FROM vector_index LIMIT 1").fetchone()
    return int(row["dim"]) if row else 0


def ensure_vector_index(db: Database, embedder: BaseEmbedder) -> int:
    """为尚无向量的条款补齐向量（幂等）。返回本次新增数量。

    维度一致性：若向量表已有维度与当前 embedder 维度不一致（如从哈希向量器
    切换为本地 BGE），则清空重建，避免余弦相似度因维度不匹配而恒为 0。
    """
    added = 0
    existing_dim = _current_dim(db)

    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.doc_id, c.chapter, c.article_no, c.content
            FROM clauses c
            LEFT JOIN vector_index v ON v.clause_id = c.id
            WHERE v.clause_id IS NULL
            """
        ).fetchall()
        # 维度不匹配 → 全量重建
        need_rebuild = existing_dim not in (0, None) and (
            (embedder.dim == 0) or (existing_dim != embedder.dim)
        )
        if need_rebuild:
            conn.execute("DELETE FROM vector_index")
            rows = conn.execute(
                "SELECT c.id, c.doc_id, c.chapter, c.article_no, c.content FROM clauses c"
            ).fetchall()
        for row in rows:
            vec = embedder.embed(row["content"])
            dim = len(vec)
            conn.execute(
                "INSERT OR REPLACE INTO vector_index (clause_id, doc_id, dim, vector) VALUES (?,?,?,?)",
                (row["id"], row["doc_id"], dim, json.dumps(vec, ensure_ascii=False)),
            )
            added += 1
    return added


def vector_count(db: Database) -> int:
    with db.session() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM vector_index").fetchone()[0])


def search_vector(
    db: Database,
    query: str,
    embedder: BaseEmbedder,
    top_k: int = 5,
    active_doc_ids: Optional[set] = None,
) -> List[VectorHit]:
    """对条款向量做余弦相似度检索，返回 Top-K。active_doc_ids 用于时效过滤。"""
    qvec = embedder.embed(query)
    hits: List[VectorHit] = []
    with db.session() as conn:
        sql = """
            SELECT v.clause_id, v.doc_id, c.chapter, c.article_no, c.content,
                   d.title, d.source_path, v.vector
            FROM vector_index v
            JOIN clauses c ON c.id = v.clause_id
            JOIN documents d ON d.doc_id = v.doc_id
        """
        params: list = []
        if active_doc_ids is not None:
            if not active_doc_ids:
                return []
            placeholders = ",".join("?" for _ in active_doc_ids)
            sql += f" WHERE d.doc_id IN ({placeholders})"
            params = list(active_doc_ids)
        rows = conn.execute(sql, params).fetchall()
    for row in rows:
        vec = json.loads(row["vector"])
        score = cosine_similarity(qvec, vec)
        if score > 0:
            hits.append(
                VectorHit(
                    clause_id=row["clause_id"],
                    doc_id=row["doc_id"],
                    doc_title=row["title"],
                    chapter=row["chapter"],
                    article_no=row["article_no"],
                    content=row["content"],
                    score=score,
                    source_path=row["source_path"],
                )
            )
    hits.sort(key=lambda h: (-h.score, h.doc_id))
    return hits[:top_k]
