"""第二阶段测试：向量检索 / 混合 / 图谱 / 会话 / 时效 / 题库 / 掌握。"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.db import Database
from app.embeddings import HashingEmbedder, cosine_similarity
from app.ingest.importer import run_import


@pytest.fixture()
def settings() -> Settings:
    return Settings()


def test_hashing_embedder_deterministic():
    e = HashingEmbedder(256)
    a = e.embed("奖学金申请需要什么条件")
    b = e.embed("奖学金申请需要什么条件")
    assert a == b
    assert len(a) == 256
    # 相似文本相似度应高于不相关文本
    sim = cosine_similarity(a, e.embed("奖学金怎么申请"))
    diff = cosine_similarity(a, e.embed("宿舍门禁几点关"))
    assert sim > diff


def test_vector_index_built(imported_db, settings):
    from app.retrieval.vector import vector_count

    assert vector_count(imported_db) > 0


def test_vector_search_returns_hits(imported_db, settings):
    from app.retrieval.vector import search_vector

    embedder = HashingEmbedder(settings.embedding_dim)
    hits = search_vector(imported_db, "奖学金申请需要什么条件", embedder, top_k=5)
    assert len(hits) > 0
    assert all(h.doc_title for h in hits)


def test_hybrid_search(imported_db, settings):
    from app.retrieval import hybrid as hybrid_mod

    embedder = HashingEmbedder(settings.embedding_dim)
    cmp = hybrid_mod.search_hybrid(imported_db, "缓考怎么申请", embedder, top_k=5)
    assert cmp.keyword or cmp.vector or cmp.hybrid
    # 混合结果去重后数量不超过两路之和
    assert len(cmp.hybrid) <= 5


def test_graph_neighbors(imported_db):
    from app.service import graph as graph_mod

    sg = graph_mod.neighbors(imported_db, "dept:jwc", depth=1)
    assert sg["root"] == "dept:jwc"
    assert len(sg["nodes"]) >= 1
    # 教务处至少有一条 HANDLED_BY 关系
    assert any(e["rel"] == "HANDLED_BY" for e in sg["edges"])


def test_graph_two_hop(imported_db):
    from app.service import graph as graph_mod

    # ent:jxjpd（奖学金评定）有 AFFECTS / HANDLED_BY 等多条关系，可展开两跳
    sg = graph_mod.neighbors(imported_db, "ent:jxjpd", depth=2)
    assert sg["depth"] == 2
    assert len(sg["nodes"]) >= 2


def test_graph_node_search(imported_db):
    from app.service import graph as graph_mod

    nodes = graph_mod.search_nodes(imported_db, "教务处")
    assert any(n.name == "教务处" for n in nodes)


def test_graph_category_stats(imported_db):
    from app.service import graph as graph_mod

    stats = graph_mod.category_stats(imported_db)
    assert len(stats) >= 5


def test_versioning_active_and_superseded(imported_db):
    from app.service import versioning as v

    active = v.active_documents(imported_db)
    # 2024 通知已被替代，不应在 active 集合
    assert "doc:notice-jxj-2024" not in active
    assert "doc:notice-jxj-2025" in active
    # superseded_by 关系
    assert v.superseded_by(imported_db, "doc:notice-jxj-2024") == "doc:notice-jxj-2025"


def test_versioning_conflict_notice(imported_db):
    from app.service import versioning as v

    notice = v.check_conflict(imported_db, ["doc:notice-jxj-2024"])
    assert notice is not None


def test_session_create_and_history(imported_db, settings):
    from app.service.conversation import ConversationService

    conv = ConversationService(imported_db, settings)
    s = conv.create()
    conv.add_message(s.session_id, "user", "奖学金申请需要什么条件？")
    conv.add_message(s.session_id, "assistant", "需满足无挂科等条件", citations=[{"doc_title": "x"}])
    h = conv.history(s.session_id)
    assert len(h) == 2
    assert h[0]["role"] == "user"
    # 列表与删除
    assert any(x.session_id == s.session_id for x in conv.list())
    conv.delete(s.session_id)
    assert not any(x.session_id == s.session_id for x in conv.list())


def test_session_rewrite_reference(imported_db, settings):
    from app.service.conversation import ConversationService

    conv = ConversationService(imported_db, settings)
    history = [{"role": "user", "content": "违纪处分有哪些种类？"}]
    rewritten = conv.rewrite_question("那最严重的那种会开除吗", history)
    # 无论 LLM 是否可用，改写结果应包含历史线索（降级拼接也含）
    assert "最严重" in rewritten or "违纪" in rewritten


def test_quiz_draw_and_answer(imported_db):
    from app.service import quiz as quiz_mod

    qs = quiz_mod.draw_questions(imported_db, count=3)
    assert len(qs) == 3
    # 判题
    q = qs[0]
    options = q["options"]
    # 从题库取正确答案判一次
    with imported_db.session() as conn:
        row = conn.execute("SELECT answer FROM quiz WHERE qid=?", (q["qid"],)).fetchone()
    correct_idx = int(row["answer"])
    result = quiz_mod.submit_answer(imported_db, q["qid"], correct_idx)
    assert result["is_correct"] is True
    assert result["explanation"]


def test_quiz_wrong_answer_recorded(imported_db):
    from app.service import quiz as quiz_mod

    qs = quiz_mod.draw_questions(imported_db, count=1)
    q = qs[0]
    with imported_db.session() as conn:
        row = conn.execute("SELECT answer FROM quiz WHERE qid=?", (q["qid"],)).fetchone()
    correct_idx = int(row["answer"])
    wrong_idx = 0 if correct_idx != 0 else 1
    quiz_mod.submit_answer(imported_db, q["qid"], wrong_idx)
    wrong = quiz_mod.wrong_answers(imported_db)
    assert any(w["qid"] == q["qid"] for w in wrong)


def test_quiz_no_repeat_when_answered(imported_db):
    from app.service import quiz as quiz_mod

    # 答完一题后，后续抽题优先未答过的
    qs = quiz_mod.draw_questions(imported_db, count=1)
    q = qs[0]
    with imported_db.session() as conn:
        row = conn.execute("SELECT answer FROM quiz WHERE qid=?", (q["qid"],)).fetchone()
    quiz_mod.submit_answer(imported_db, q["qid"], int(row["answer"]))
    # 抽 5 题应优先不含已答的（题库 22 题足够）
    next_qs = quiz_mod.draw_questions(imported_db, count=5)
    assert q["qid"] not in [x["qid"] for x in next_qs]


def test_mastery_set_and_list(imported_db):
    from app.service import mastery as m

    m.set_mastery(imported_db, "ent:hukao", "entity", "缓考", "unmastered")
    items = m.list_mastery(imported_db)
    assert any(i["item_id"] == "ent:hukao" and i["status"] == "unmastered" for i in items)
    assert "缓考" in m.unmastered_names(imported_db)
    # 切换为已掌握
    m.set_mastery(imported_db, "ent:hukao", "entity", "缓考", "mastered")
    assert "缓考" not in m.unmastered_names(imported_db)


def test_qa_returns_subgraph(imported_db, settings):
    from app.service.qa import QAService

    qa = QAService(imported_db, settings)
    ans = qa.ask("缓考怎么申请？")
    assert ans.found is True
    # 返回子图（相关实体）结构
    assert hasattr(ans, "subgraph")
