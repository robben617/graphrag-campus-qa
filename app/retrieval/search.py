"""关键词/精确检索：条款 + 实体 + FAQ。

P0 检索（对齐设计文档 §6 基础版#1/#2）：
- 精确检索：实体名精确匹配、条款号精确匹配（"第15条"）
- 关键词检索：问句分词后在条款正文做命中文案评分，返回 Top-K

评分策略（简单、可解释、不依赖外部服务）：
- 每个关键词命中一次 +1；命中实体名额外 +2；命中条号 +3；
- 弱语义词（什么/怎么）命中不计入主分，仅作并列排序辅助。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..db import Database
from . import tokenizer

import re as _re

ARTICLE_NO_RE = _re.compile(r"^第\s*([0-9一二三四五六七八九十百零〇]+)\s*条$")


@dataclass
class Hit:
    doc_id: str
    doc_title: str
    chapter: str = ""
    article_no: str = ""
    content: str = ""
    score: float = 0.0
    matched: List[str] = field(default_factory=list)
    source_path: str = ""

    def citation(self) -> str:
        loc = f"第{self.article_no}条" if self.article_no else ""
        if self.chapter and loc:
            loc = f"{self.chapter} {loc}"
        elif self.chapter:
            loc = self.chapter
        return f"《{self.doc_title}》{loc}".rstrip("》")

    def fingerprint(self) -> str:
        """用于混合检索去重的稳定指纹：doc_id + 章/条 + 内容哈希前 8 位。"""
        import hashlib

        h = hashlib.md5(self.content.encode("utf-8")).hexdigest()[:8]
        return f"{self.doc_id}|{self.chapter}|{self.article_no}|{h}"


@dataclass
class SearchResult:
    hits: List[Hit]
    entities: List[str] = field(default_factory=list)
    faq_match: Optional[dict] = None


def search_entities(db: Database, tokens: List[str]) -> List[dict]:
    """精确/关键词命中实体：返回 {eid,name,kind,category} 列表（供答案标注相关实体）。"""
    if not tokens:
        return []
    out = []
    with db.session() as conn:
        for t in tokens:
            row = conn.execute(
                "SELECT eid, name, kind, category, description FROM entities WHERE name = ?", (t,)
            ).fetchone()
            if row and row["name"] not in [x["name"] for x in out]:
                out.append(dict(row))
        # 关键词部分匹配（名称为关键词的子串或关键词为名称子串）
        if len(out) == 0:
            for t in tokens:
                rows = conn.execute(
                    "SELECT eid, name, kind, category, description FROM entities WHERE name LIKE ? OR ? LIKE '%'||name||'%'",
                    (f"%{t}%", t),
                ).fetchall()
                for row in rows:
                    if row["name"] not in [x["name"] for x in out]:
                        out.append(dict(row))
    return out


def _search_clauses(db: Database, tokens: List[str], top_k: int) -> List[Hit]:
    if not tokens:
        return []
    weak = set(tokenizer.weak_tokens(tokens))
    hits: List[Hit] = []

    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT c.doc_id, c.chapter, c.article_no, c.content, c.seq,
                   d.title, d.category, d.status, d.source_path
            FROM clauses c JOIN documents d ON c.doc_id = d.doc_id
            """
        ).fetchall()

        # 实体名映射：用于"命中实体名额外加分"
        ent_rows = conn.execute("SELECT name, category FROM entities").fetchall()
        entity_names = {r["name"] for r in ent_rows}
        entity_cats = {r["name"]: r["category"] for r in ent_rows}

    for row in rows:
        content = row["content"]
        title = row["title"]
        score = 0.0
        matched: List[str] = []
        for t in tokens:
            if t in weak:
                continue
            hit = 0
            if t in content:
                hit += 1
            if t in title:
                hit += 0.5
            if t in entity_names:
                hit += 2
            if hit > 0:
                score += hit
                matched.append(t)
        # 条号精确加分（"第15条"）
        for t in tokens:
            m = ARTICLE_NO_RE.match(t)
            if m and row["article_no"] == m.group(1):
                score += 3
                if t not in matched:
                    matched.append(t)
        if score > 0:
            hits.append(
                Hit(
                    doc_id=row["doc_id"],
                    doc_title=title,
                    chapter=row["chapter"],
                    article_no=row["article_no"],
                    content=content,
                    score=score,
                    matched=matched,
                    source_path=row["source_path"],
                )
            )

    hits.sort(key=lambda h: (-h.score, h.doc_id, -len(h.matched)))
    return hits[:top_k]


def search_faq(db: Database, question: str) -> Optional[dict]:
    """FAQ 精确/关键词匹配：优先精确去符号等值，其次关键词重合率高的 top 命中。"""
    import re
    norm = lambda s: re.sub(r"[\s\W_]+", "", s)  # noqa: E731
    q_norm = norm(question)
    with db.session() as conn:
        faqs = conn.execute("SELECT qid, question, answer, category, source FROM faq").fetchall()
    if not faqs:
        return None
    # 精确匹配
    for f in faqs:
        if norm(f["question"]) == q_norm:
            return dict(f)
    # 关键词重合匹配
    q_tokens = set(tokenizer.extract_keywords(question))
    best, best_score = None, 0.0
    for f in faqs:
        f_tokens = set(tokenizer.extract_keywords(f["question"]))
        if not f_tokens:
            continue
        overlap = len(q_tokens & f_tokens) / len(f_tokens)
        if overlap > best_score:
            best, best_score = dict(f), overlap
    if best_score >= 0.5:
        return best
    return None


def search(db: Database, question: str, top_k: int = 5) -> SearchResult:
    """综合检索：FAQ 匹配 + 条款命中 + 相关实体。"""
    tokens = tokenizer.extract_keywords(question)
    faq_hit = search_faq(db, question)
    entities = search_entities(db, tokens)
    hits = _search_clauses(db, tokens, top_k)
    return SearchResult(hits=hits, entities=[e["name"] for e in entities], faq_match=faq_hit)
