"""向量检索：对 clauses 做 Embedding 相似度检索（离线哈希向量器兜底）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


def ensure_vector_index(db: Database, embedder: BaseEmbedder) -> int:
    """为当前模型补齐缺失向量。模型切换时在同一事务中重建索引。"""
    model_key = embedder.index_key
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT model_key FROM vector_index_meta WHERE id = 1").fetchone()
        if meta is None or meta["model_key"] != model_key:
            conn.execute("DELETE FROM vector_index")
            conn.execute("INSERT OR REPLACE INTO vector_index_meta (id, model_key) VALUES (1, ?)", (model_key,))

        existing = conn.execute("SELECT dim FROM vector_index LIMIT 1").fetchone()
        dimension = existing["dim"] if existing else 0
        rows = conn.execute(
            """
            SELECT c.id, c.doc_id, c.content
            FROM clauses c
            LEFT JOIN vector_index v ON v.clause_id = c.id
            WHERE v.clause_id IS NULL
            ORDER BY c.id
            """
        ).fetchall()
        batch_size = embedder.index_batch_size
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            vectors = embedder.embed_batch([row["content"] for row in batch])
            if len(vectors) != len(batch):
                raise RuntimeError("向量数量与待索引条款数量不一致。")
            dimension = dimension or len(vectors[0])
            if not dimension or any(len(vec) != dimension for vec in vectors):
                raise RuntimeError("向量维度不一致，请确认 Embedding 模型配置。")
            conn.executemany(
                "INSERT INTO vector_index (clause_id, doc_id, dim, vector) VALUES (?,?,?,?)",
                [(row["id"], row["doc_id"], dimension, json.dumps(vec)) for row, vec in zip(batch, vectors)],
            )
    return len(rows)


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
    if active_doc_ids is not None and not active_doc_ids:
        return []
    hits: List[VectorHit] = []
    with db.session() as conn:
        conn.execute("BEGIN")
        meta = conn.execute("SELECT model_key FROM vector_index_meta WHERE id = 1").fetchone()
        if meta and meta["model_key"] != embedder.index_key:
            raise RuntimeError("Embedding 模型与已有向量索引不一致，请重新导入知识库。")
        sql = """
            SELECT v.clause_id, v.doc_id, c.chapter, c.article_no, c.content,
                   d.title, d.source_path, v.dim, v.vector
            FROM vector_index v
            JOIN clauses c ON c.id = v.clause_id
            JOIN documents d ON d.doc_id = v.doc_id
        """
        params: list = []
        if active_doc_ids is not None:
            placeholders = ",".join("?" for _ in active_doc_ids)
            sql += f" WHERE d.doc_id IN ({placeholders})"
            params = list(active_doc_ids)
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    qvec = embedder.embed(query)
    if any(row["dim"] != len(qvec) for row in rows):
        raise RuntimeError("查询向量与索引维度不一致，请重新导入知识库。")
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
