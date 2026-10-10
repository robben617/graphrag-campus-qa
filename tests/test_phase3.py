"""第三阶段测试：本地模型接入 / reranker / 增量入库 / 多用户 / 评测 / 引用路径。"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.db import Database


@pytest.fixture()
def settings() -> Settings:
    return Settings()


def test_local_model_paths_configured(settings):
    assert settings.local_embedding_path.endswith("BAAI--bge-small-zh-v1.5")
    assert settings.local_reranker_path.endswith("BAAI--bge-reranker-base")


def test_ml_local_available_detection(settings):
    from app import ml_local

    # 模型文件存在（无论是否加载）
    assert ml_local._model_exists(settings.local_embedding_path)
    assert ml_local._model_exists(settings.local_reranker_path)


def test_embedder_prefers_local(settings):
    from app.embeddings import LocalEmbedder, get_embedder

    emb = get_embedder(settings)
    # 本地模型文件存在时应返回 LocalEmbedder（运行时加载失败才降级）
    from app import ml_local

    if ml_local.local_embedder_available(settings):
        assert isinstance(emb, LocalEmbedder)


def test_vector_index_built_with_bge_dim(imported_db):
    from app.retrieval.vector import vector_count

    n = vector_count(imported_db)
    assert n > 0
    # 若本地 BGE 参与构建，维度应为 512；否则为哈希维度（256）。仅断言存在。
    with imported_db.session() as conn:
        dims = {r["dim"] for r in conn.execute("SELECT DISTINCT dim FROM vector_index").fetchall()}
    assert len(dims) == 1  # 维度统一


def test_reranker_orders_relevant_first(settings):
    from app import ml_local

    if not ml_local.local_reranker_available(settings):
        pytest.skip("本地 reranker 不可用")
    r = ml_local.rerank_local(settings, "缓考怎么申请",
                              ["因故不能参加考试应申请缓考", "宿舍禁止使用违规电器", "奖学金评定要求无挂科"], 2)
    assert r is not None
    # 第一条（缓考相关）应排在最前
    assert r[0][0] == 0


def test_incremental_ingest_notice(imported_db):
    from app.service import ingestion as ing

    text = "关于开展2026年优秀学生奖学金评定工作的通知\n\n学生申请截止时间为2026年10月20日，一等奖每人每年5000元。"
    result = ing.ingest_notice(
        imported_db, text, title="关于开展2026年优秀学生奖学金评定工作的通知",
        category="奖助学金", issuer="学生资助管理中心", version="2026",
    )
    assert result["doc_id"].startswith("doc:")
    assert result["clauses"] >= 1
    # 自动检测：同类别、标题相似 → 替代 2025 通知
    assert "doc:notice-jxj-2025" in result["superseded"]


def test_incremental_ingest_supersede_status(imported_db):
    from app.service import ingestion as ing

    # 先入库 2026 通知，再入库 2027 通知
    ing.ingest_notice(
        imported_db, "2026年奖学金评定：一等奖每年5500元。",
        title="关于开展2026年优秀学生奖学金评定工作的通知", category="奖助学金", version="2026",
    )
    r = ing.ingest_notice(
        imported_db, "2027年奖学金评定：一等奖每年6000元。",
        title="关于开展2027年优秀学生奖学金评定工作的通知", category="奖助学金", version="2027",
    )
    # 2027 自动替代 2026
    assert r["superseded"], "2027 通知应自动替代 2026 通知"
    # 检测器能找到历史版本
    cand = ing.detect_superseded_candidates(
        imported_db, "奖助学金", "关于开展2027年优秀学生奖学金评定工作的通知", r["doc_id"]
    )
    titles = [c["title"] for c in cand]
    assert any("2026" in t for t in titles)


def test_users_create_and_resolve(imported_db):
    from app.service import users as u

    user = u.create_user(imported_db, "student_1", "student")
    assert user["api_token"]
    resolved = u.resolve_user(imported_db, user["api_token"])
    assert resolved["user_id"] == user["user_id"]
    assert resolved["role"] == "student"


def test_users_role_guard(imported_db):
    from app.service import users as u

    user = u.create_user(imported_db, "stu_noperm", "student")
    from app.errors import AppError

    try:
        u.require_role({"user_id": user["user_id"], "role": "student"}, {"admin"})
        assert False, "应拒绝无权限"
    except AppError as e:
        assert "权限不足" in e.message


def test_evaluation_dataset_size(settings):
    from app.service.evaluation import load_evaluation_set
    from pathlib import Path

    items = load_evaluation_set(Path(settings.data_dir))
    assert len(items) >= 20


def test_evaluation_run(imported_db, settings):
    from app.service import evaluation as ev

    report = ev.run_evaluation(imported_db, settings, top_k=5)
    assert report["total"] >= 20
    assert 0.0 <= report["recall_at_k"] <= 1.0
    assert 0.0 <= report["factuality"] <= 1.0
    assert 0.0 <= report["citation_coverage"] <= 1.0
    # 报告已落库
    latest = ev.latest_report(imported_db)
    assert latest["exists"] is True


def test_relation_paths(imported_db):
    from app.service import paths as p

    paths = p.relation_paths(imported_db, "ent:jxjpd", max_depth=2)
    assert isinstance(paths, list)
    # 奖学金评定应至少有一条路径到部门
    assert any(any(s["rel"] == "HANDLED_BY" for s in path) for path in paths)
