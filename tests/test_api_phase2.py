"""第二阶段 API 测试：会话 / 图谱 / 题库 / 掌握 / 检索对比 / 时效。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_data_dir):
    from app.config import Settings
    from app.db import Database
    from app.ingest.importer import run_import
    from app.web.server import create_app
    from fastapi.testclient import TestClient

    db_path = str(tmp_data_dir / "school_kb.db")
    settings = Settings(db_path=db_path)
    run_import(Database(db_path), tmp_data_dir)
    app = create_app(settings)
    return TestClient(app)


def test_health_has_phase2_fields(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "vector_count" in data
    assert "embedding_configured" in data


def test_session_flow(client):
    # 创建
    r = client.post("/api/v1/sessions", json={"title": "测试会话"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    # 追问
    r = client.post(f"/api/v1/sessions/{sid}/ask", json={"question": "违纪处分有哪些种类？"})
    assert r.status_code == 200
    assert r.json()["found"] is True
    # 历史
    r = client.get(f"/api/v1/sessions/{sid}/messages")
    assert r.status_code == 200
    assert len(r.json()) >= 2
    # 删除
    r = client.delete(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200


def test_session_not_found(client):
    r = client.get("/api/v1/sessions/nonexistent")
    assert r.status_code == 404


def test_graph_endpoints(client):
    r = client.get("/api/v1/graph/stats")
    assert r.status_code == 200
    assert r.json()["nodes"] >= 30
    r = client.get("/api/v1/graph/categories")
    assert r.status_code == 200
    assert len(r.json()) >= 5
    r = client.get("/api/v1/graph/nodes", params={"q": "教务处"})
    assert r.status_code == 200
    assert len(r.json()) >= 1
    r = client.get("/api/v1/graph/nodes/dept:jwc/neighbors", params={"depth": 1})
    assert r.status_code == 200
    assert len(r.json()["nodes"]) >= 1


def test_quiz_endpoints(client):
    r = client.get("/api/v1/quiz/questions", params={"count": 3})
    assert r.status_code == 200
    assert len(r.json()) == 3
    q = r.json()[0]
    # 判题（先查答案）
    r2 = client.post("/api/v1/quiz/answer", json={"qid": q["qid"], "chosen": 0})
    assert r2.status_code == 200
    assert "is_correct" in r2.json()


def test_mastery_endpoint(client):
    r = client.post("/api/v1/mastery", json={
        "item_id": "ent:hukao", "item_type": "entity", "name": "缓考", "status": "unmastered",
    })
    assert r.status_code == 200
    r = client.get("/api/v1/mastery")
    assert r.status_code == 200
    assert any(i["item_id"] == "ent:hukao" for i in r.json())


def test_search_compare_endpoint(client):
    r = client.get("/api/v1/search/compare", params={"q": "缓考怎么申请"})
    assert r.status_code == 200
    data = r.json()
    assert "keyword" in data and "vector" in data and "hybrid" in data


def test_versioning_endpoint(client):
    r = client.get("/api/v1/versioning")
    assert r.status_code == 200
    docs = r.json()["documents"]
    d2024 = next(d for d in docs if d["doc_id"] == "doc:notice-jxj-2024")
    assert d2024["superseded_by"] == "doc:notice-jxj-2025"
