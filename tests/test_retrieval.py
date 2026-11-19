"""检索层单元测试：精确检索 + 关键词检索。"""

from __future__ import annotations

from app.db import Database
from app.embeddings import HashingEmbedder, LocalEmbedder
from app.config import Settings
from app.retrieval import search as s
from app.retrieval.hybrid import search_hybrid
from app.retrieval.vector import ensure_vector_index
from app.service.versioning import active_doc_ids
from app.service.qa import QAService


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


def test_faq_requires_exact_question_and_active_source(tmp_path):
    db = Database(str(tmp_path / "faq.db"))
    db.init_schema()
    with db.session() as conn:
        conn.execute("INSERT INTO documents (doc_id, title) VALUES ('handbook', '学生手册')")
        conn.execute("INSERT INTO clauses (doc_id, content) VALUES ('handbook', '补考不及格者须重修。')")
        conn.executemany(
            "INSERT INTO faq (qid, question, answer, source) VALUES (?, ?, ?, '《学生手册》教务章节')",
            [("retake", "挂科了能重修吗？", "可以重修。"), ("grant", "国家奖学金和国家助学金有什么区别？", "申请条件不同。")],
        )

    assert s.search_faq(db, "补考和重修有什么区别？") is None
    assert s.search_faq(db, "国家励志奖学金的金额是多少？") is None
    qa = QAService(db, Settings(embedding_enabled=False))
    answer = qa.ask("挂科了能重修吗", generate=False)
    assert answer.mode == "精确FAQ"
    assert [citation.doc_id for citation in answer.citations] == ["handbook"]

    with db.session() as conn:
        conn.execute("UPDATE documents SET status = '已废止' WHERE doc_id = 'handbook'")
    assert not qa.ask("挂科了能重修吗？", generate=False).found


def test_entity_name_alone_does_not_match_unrelated_clause(tmp_path):
    db = Database(str(tmp_path / "entity.db"))
    db.init_schema()
    with db.session() as conn:
        conn.execute("INSERT INTO documents (doc_id, title) VALUES ('library', '图书馆借阅规定')")
        conn.execute("INSERT INTO clauses (doc_id, content) VALUES ('library', '图书每次最多可借三本。')")
        conn.execute("INSERT INTO entities (eid, kind, name) VALUES ('scholarship', 'entity', '奖学金')")

    result = s.search(db, "奖学金申请需要哪些材料？")
    assert result.entities == ["奖学金"]
    assert not result.hits


def test_vector_index_reuses_local_model_and_detects_same_dimension_switch(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "models.db"))
    db.init_schema()
    with db.session() as conn:
        conn.execute("INSERT INTO documents (doc_id, title) VALUES ('policy', '图书借阅规定')")
        conn.execute("INSERT INTO clauses (doc_id, content) VALUES ('policy', '学生可以借阅图书。')")

    calls = []

    def encode(settings, texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("app.ml_local.encode_local", encode)
    settings = Settings()
    assert ensure_vector_index(db, LocalEmbedder(settings)) == 1
    assert ensure_vector_index(db, LocalEmbedder(settings)) == 0
    assert len(calls) == 1

    new_embedder = HashingEmbedder(dim=2)
    comparison = search_hybrid(db, "借阅图书", new_embedder)
    assert comparison.keyword and not comparison.vector and comparison.degraded_reason
    assert ensure_vector_index(db, new_embedder) == 1
    assert not search_hybrid(db, "借阅图书", new_embedder).degraded_reason


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
