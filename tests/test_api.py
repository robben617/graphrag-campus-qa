"""Web API 测试：最小页面接口 + 统一错误响应。"""

from __future__ import annotations

def test_health_ok(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ask_success(client):
    resp = client.post("/api/v1/ask", json={"question": "奖学金申请需要什么条件？"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is True
    assert data["answer"]


def test_ask_empty_question_400(client):
    resp = client.post("/api/v1/ask", json={"question": ""})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] in {"EMPTY_QUESTION", "INVALID_INPUT"}


def test_ask_too_long_400(client):
    resp = client.post("/api/v1/ask", json={"question": "问" * 2001})
    assert resp.status_code == 400
    assert resp.json()["code"] == "QUESTION_TOO_LONG"


def test_ask_out_of_scope_refuse(client):
    resp = client.post("/api/v1/ask", json={"question": "学校校车几点发车？"})
    assert resp.status_code == 200
    assert resp.json()["found"] is False
