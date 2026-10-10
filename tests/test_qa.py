"""QA 服务测试：AT-02（回答+引用）、AT-03（拒答）、AT-08（非法输入）、AT-09（降级）。"""

from __future__ import annotations

from app.config import Settings
from app.db import Database
from app.errors import EmptyQuestionError, QuestionTooLongError, StorageUnavailable
from app.embeddings import BaseEmbedder
from app.ingest.importer import run_import
from app.service.qa import QAService


def _qa(imported_db: Database) -> QAService:
    return QAService(imported_db, Settings())


class _BrokenEmbedder(BaseEmbedder):
    """模拟 Embedding 完全不可用（本地模型损坏 / API 挂掉）。"""

    dim = 512

    def embed(self, text: str):
        raise RuntimeError("模拟 Embedding 服务不可用")

    def embed_batch(self, texts):
        raise RuntimeError("模拟 Embedding 服务不可用")


def test_at02_answer_with_source(imported_db):
    ans = _qa(imported_db).ask("奖学金申请需要什么条件？")
    assert ans.found is True
    assert ans.answer
    # FAQ 精确命中时也应有引用（条款命中）
    assert ans.citations or ans.mode == "精确FAQ"
    # 有引用时，引用应含来源文件标题
    if ans.citations:
        assert all(c.doc_title for c in ans.citations)


def test_at03_refuse_out_of_scope(imported_db):
    ans = _qa(imported_db).ask("学校校车几点发车？")
    assert ans.found is False
    assert "未找到" in ans.answer or "未找到" in ans.answer
    assert ans.citations == []


def test_at08_empty_question(imported_db):
    try:
        _qa(imported_db).ask("   ")
        assert False, "空问题应拒绝"
    except EmptyQuestionError as e:
        assert "不能为空" in e.message


def test_at08_too_long_question(imported_db):
    q = "问" * 2001
    try:
        _qa(imported_db).ask(q)
        assert False, "超长问题应拒绝"
    except QuestionTooLongError as e:
        assert "过长" in e.message


def test_empty_knowledge_base_message(tmp_data_dir):
    db = Database(str(tmp_data_dir / "empty.db"))
    db.init_schema()
    ans = _qa(db).ask("奖学金怎么申请？")
    assert ans.found is False
    assert "导入" in ans.answer


def test_storage_unavailable_raises():
    """数据库路径不可用（父路径为文件）时抛 StorageUnavailable。"""
    from pathlib import Path

    blocker = Path(".tmp/storage_blocker.txt")
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("x", encoding="utf-8")
    bad_db = Database(str(blocker / "sub" / "kb.db"))
    from app.service.qa import QAService

    try:
        QAService(bad_db, Settings()).ask("奖学金")
        assert False, "应抛出存储不可用"
    except StorageUnavailable as e:
        assert "知识库连接失败" in e.message


def test_at09_embedding_down_degrades_to_keyword(imported_db):
    """AT-09：停掉 Embedding 后自动降级关键词检索，问答不中断且带提示。

    覆盖运行期降级：向量路抛异常时 search_hybrid 内部降级为关键词单路，
    不向用户抛 500，并由 degradation_notice 给出可见提示。
    """
    qa = _qa(imported_db)
    qa.embedder = _BrokenEmbedder()  # 注入一个必然失败的向量器

    ans = qa.ask("奖学金申请需要什么条件？")

    # 1) 请求没有失败：仍然给出答案与引用
    assert ans.found is True
    assert ans.answer
    assert ans.citations
    # 2) 降级被正确标记并透出提示
    assert ans.degraded is True
    assert ans.degradation_notice
    assert "关键词" in ans.degradation_notice
    # 3) 提示会进入 API 响应体
    assert ans.to_dict()["degradation_notice"] == ans.degradation_notice


def test_at09_embedding_down_still_refuses_out_of_scope(imported_db):
    """AT-09 补充：降级后库外问题仍必须拒答，不能因降级而放开编造。"""
    qa = _qa(imported_db)
    qa.embedder = _BrokenEmbedder()

    ans = qa.ask("学校校车几点发车？")

    assert ans.found is False
    assert ans.citations == []
    assert ans.degraded is True
    assert ans.degradation_notice
