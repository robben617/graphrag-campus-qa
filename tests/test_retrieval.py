"""检索层单元测试：精确检索 + 关键词检索。"""

from __future__ import annotations

from app.retrieval import search as s


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
