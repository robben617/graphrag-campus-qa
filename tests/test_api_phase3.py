"""第三阶段 API 测试：用户 / 增量入库 / 评测 / 引用路径 / 健康含本地模型。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_data_dir):
    import os

    # 测试禁用本地大模型，避免每个 fixture 加载模型过慢
    os.environ["USE_LOCAL_EMBEDDING"] = "off"
    os.environ["USE_LOCAL_RERANKER"] = "off"

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


def _auth(client, role="teacher"):
    r = client.post("/api/v1/users", json={"username": f"u_{role}_{id(role)}", "role": role})
    return r.json()["api_token"]


def test_health_reports_local_models(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    data = r.json()
    assert "local_embedding" in data
    assert "local_reranker" in data


def test_whoami_anonymous(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json()["user_id"] == "anonymous"


def test_create_user_and_whoami(client):
    r = client.post("/api/v1/users", json={"username": "alice", "role": "student"})
    assert r.status_code == 200
    token = r.json()["api_token"]
    r = client.get("/api/v1/me", headers={"X-User-Token": token})
    assert r.json()["username"] == "alice"


def test_ingest_notice_requires_teacher(client):
    payload = {"text": "内容", "title": "通知", "category": "奖助学金"}
    # 匿名 → 403/400 权限不足
    r = client.post("/api/v1/ingest/notice", json=payload)
    assert r.status_code == 400  # AppError 默认 400


def test_ingest_notice_as_teacher(client):
    token = _auth(client, "teacher")
    payload = {"text": "2026年奖学金：一等奖5000元。", "title": "2026年奖学金评定通知", "category": "奖助学金"}
    r = client.post("/api/v1/ingest/notice", json=payload, headers={"X-User-Token": token})
    assert r.status_code == 200
    assert r.json()["clauses"] >= 1


def test_evaluation_endpoints(client):
    token = _auth(client, "teacher")
    r = client.post("/api/v1/evaluation/run", headers={"X-User-Token": token})
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 20
    r2 = client.get("/api/v1/evaluation/latest", headers={"X-User-Token": token})
    assert r2.status_code == 200
    assert r2.json()["exists"] is True


def test_relation_paths_endpoint(client):
    r = client.get("/api/v1/graph/paths/ent:jxjpd")
    assert r.status_code == 200
    assert "paths" in r.json()


def test_user_isolation_sessions(client):
    token_a = _auth(client, "student")
    r = client.post("/api/v1/sessions", json={"title": "A"}, headers={"X-User-Token": token_a})
    sid = r.json()["session_id"]
    # 匿名用户看不到 A 的会话
    r2 = client.get("/api/v1/sessions")
    assert sid not in [s["session_id"] for s in r2.json()]
    # A 自己能看
    r3 = client.get("/api/v1/sessions", headers={"X-User-Token": token_a})
    assert sid in [s["session_id"] for s in r3.json()]
