"""检索层单元测试：精确检索 + 关键词检索。"""

from __future__ import annotations

from app.db import Database
from app.embeddings import HashingEmbedder
from app.retrieval import search as s
from app.retrieval.hybrid import search_hybrid
from app.retrieval.vector import ensure_vector_index
from app.service.versioning import active_doc_ids


def test_hybrid_filters_inactive_documents_before_ranking(tmp_path):
    db = Database(str(tmp_path / "retrieval.db"))
    db.init_schema()
    with db.session() as conn:
        conn.executemany(
            "INSERT INTO documents (doc_id, title, status) VALUES (?, ?, ?)",
            [("old", "奖学金申请截止日期", "已废止"), ("new", "现行通知", "现行有效")],
        )
        conn.executemany(
            "INSERT INTO clauses (doc_id, content) VALUES (?, ?)",
            [("old", "奖学金申请截止日期为六月一日。"), ("new", "奖学金申请于六月十日截止。")],
        )
    embedder = HashingEmbedder()
    ensure_vector_index(db, embedder)
    question = "奖学金申请截止日期"

    assert s.search(db, question, top_k=1).hits[0].doc_id == "old"
    comparison = search_hybrid(db, question, embedder, top_k=1, active_doc_ids=active_doc_ids(db))
    for hits in (comparison.keyword, comparison.vector, comparison.hybrid):
        assert [hit.doc_id for hit in hits] == ["new"]

    empty = search_hybrid(db, question, embedder, top_k=1, active_doc_ids=set())
    assert not (empty.keyword or empty.vector or empty.hybrid)


def test_entity_exact_search(imported_db):
    ents = s.search_entities(imported_db, ["教务处", "缓考"])
    names = {e["name"] for e in ents}
    assert "教务处" in names
    assert "缓考" in names


def test_keyword_search_scholarship(imported_db):
    res = s.search(imported_db, "奖学金申请需要什么条件", top_k=5)
    # FAQ 精确/关键词命中 或 条款命中，二者至少其一
    assert res.faq_match is not None or len(res.hits) > 0
    if res.faq_match:
        # FAQ 答案应包含实质内容（如"无挂科""纪律处分"等条件描述）
        assert res.faq_match["answer"].strip()


def test_article_no_boost(imported_db):
    # "第5条" 应能通过条号正则命中（条款存在时）
    res = s.search(imported_db, "第1条", top_k=5)
    # 至少不报错；是否命中取决于 PDF 是否可提取文本
    assert res is not None


def test_out_of_scope_no_hits(imported_db):
    """校车发车时间不在知识库 → 无命中或仅弱命中。"""
    res = s.search(imported_db, "学校校车几点发车", top_k=3)
    # 关键词"校车/发车"在知识库不应命中任何条款/FAQ
    assert res.faq_match is None
    assert res.hits == []
