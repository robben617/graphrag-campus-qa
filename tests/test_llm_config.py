"""用户自带大模型密钥（BYOK）的接口测试。

覆盖点（都是"错了会直接泄密或越权"的地方）：
1. 匿名不能保存配置（没有 user_id 可挂，静默接受等于丢数据）
2. 明文 key 不出现在任何响应里
3. 明文 key 不出现在数据库与磁盘文件里（加密落库）
4. 用户之间互相看不到对方的配置
5. base_url 的协议与内网地址被拦截（SSRF 面）
6. 留空 key 时沿用旧 key，而不是把配置清空
7. 服务器兜底开关关闭后，没有个人配置就明确"不可用"
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database

PLAIN_KEY = "sk-test-abcdefghijklmnop-9876"


@pytest.fixture()
def client(tmp_path):
    from app.web.server import create_app

    settings = Settings(
        db_path=str(tmp_path / "kb.db"),
        llm_config_secret="unit-test-secret",
        # 测试里不连真实模型：不配置服务器 key，也不允许兜底
        llm_enabled=False,
        llm_api_key="",
        allow_server_llm_fallback=False,
        llm_config_test_interval=0,
    )
    Database(str(tmp_path / "kb.db")).init_schema()
    return TestClient(create_app(settings))


def _new_user(client, name: str = "tester", role: str = "student", actor: dict | None = None) -> dict:
    """建用户。系统内首个账号免鉴权（bootstrap），之后必须由 admin 创建。"""
    headers = {"X-User-Token": actor["api_token"]} if actor else {}
    resp = client.post("/api/v1/users", json={"username": name, "role": role}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(user: dict) -> dict:
    return {"X-User-Token": user["api_token"]}


def test_anonymous_cannot_save_or_test(client):
    r = client.put("/api/v1/llm/config", json={"api_key": PLAIN_KEY})
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"

    r = client.post("/api/v1/llm/config/test", json={"api_key": PLAIN_KEY})
    assert r.status_code == 403


def test_anonymous_can_read_status(client):
    """匿名也要能看状态，前端据此提示"去登录才能配"。"""
    r = client.get("/api/v1/llm/config")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["source"] == "none"
    assert body["effective"] is False


def test_save_then_read_masks_key(client):
    user = _new_user(client)
    r = client.put(
        "/api/v1/llm/config",
        json={"api_key": PLAIN_KEY, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        headers=_headers(user),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "user"
    assert body["model"] == "deepseek-chat"
    # 只回掩码，且掩码必须保留末 4 位便于用户辨认
    assert body["key_hint"].endswith(PLAIN_KEY[-4:])
    assert PLAIN_KEY not in json.dumps(body, ensure_ascii=False)

    r2 = client.get("/api/v1/llm/config", headers=_headers(user))
    assert r2.status_code == 200
    assert PLAIN_KEY not in r2.text
    assert r2.json()["key_hint"] == body["key_hint"]


def test_key_is_encrypted_at_rest(client, tmp_path):
    user = _new_user(client)
    client.put(
        "/api/v1/llm/config",
        json={"api_key": PLAIN_KEY, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        headers=_headers(user),
    )
    conn = sqlite3.connect(str(tmp_path / "kb.db"))
    try:
        enc = conn.execute("SELECT api_key_enc FROM user_llm_configs").fetchone()[0]
    finally:
        conn.close()
    assert enc and enc != PLAIN_KEY
    assert PLAIN_KEY not in enc
    # 整库文件里也不应出现明文
    assert PLAIN_KEY.encode() not in (tmp_path / "kb.db").read_bytes()


def test_config_is_isolated_between_users(client):
    a = _new_user(client, "alice", role="admin")
    b = _new_user(client, "bob", actor=a)
    client.put(
        "/api/v1/llm/config",
        json={"api_key": PLAIN_KEY, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        headers=_headers(a),
    )
    r = client.get("/api/v1/llm/config", headers=_headers(b))
    assert r.json()["configured"] is False
    # b 保存自己的 key 不影响 a
    client.put(
        "/api/v1/llm/config",
        json={"api_key": "sk-bob-000000000000", "base_url": "https://api.moonshot.cn/v1", "model": "kimi"},
        headers=_headers(b),
    )
    a_view = client.get("/api/v1/llm/config", headers=_headers(a)).json()
    assert a_view["model"] == "deepseek-chat"


def test_base_url_validation(client):
    user = _new_user(client)
    h = _headers(user)

    r = client.put("/api/v1/llm/config", json={"api_key": PLAIN_KEY, "base_url": "ftp://x.com"}, headers=h)
    assert r.status_code == 400

    r = client.put(
        "/api/v1/llm/config", json={"api_key": PLAIN_KEY, "base_url": "http://127.0.0.1:9000/v1"}, headers=h
    )
    assert r.status_code == 400
    assert "内网" in r.json()["message"]

    r = client.put(
        "/api/v1/llm/config", json={"api_key": PLAIN_KEY, "base_url": "http://10.1.2.3/v1"}, headers=h
    )
    assert r.status_code == 400

    # 误粘完整端点 → 自动裁掉 /chat/completions，避免路径重复
    r = client.put(
        "/api/v1/llm/config",
        json={"api_key": PLAIN_KEY, "base_url": "https://api.deepseek.com/v1/chat/completions"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["base_url"] == "https://api.deepseek.com/v1"


def test_blank_key_keeps_existing(client):
    user = _new_user(client)
    h = _headers(user)
    first = client.put(
        "/api/v1/llm/config",
        json={"api_key": PLAIN_KEY, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        headers=h,
    ).json()

    # 只改模型，不重填 key
    second = client.put(
        "/api/v1/llm/config",
        json={"api_key": "", "base_url": "", "model": "deepseek-reasoner"},
        headers=h,
    )
    assert second.status_code == 200
    body = second.json()
    assert body["configured"] is True
    assert body["model"] == "deepseek-reasoner"
    assert body["key_hint"] == first["key_hint"]
    assert body["base_url"] == "https://api.deepseek.com/v1"


def test_first_save_requires_key(client):
    user = _new_user(client)
    r = client.put("/api/v1/llm/config", json={"api_key": "", "model": "x"}, headers=_headers(user))
    assert r.status_code == 400


def test_delete_config(client):
    user = _new_user(client)
    h = _headers(user)
    client.put(
        "/api/v1/llm/config",
        json={"api_key": PLAIN_KEY, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        headers=h,
    )
    r = client.delete("/api/v1/llm/config", headers=h)
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert client.get("/api/v1/llm/config", headers=h).json()["configured"] is False
    # 再删一次返回 false，而不是报错
    assert client.delete("/api/v1/llm/config", headers=h).json()["deleted"] is False


def test_server_fallback_flag(client):
    """关闭兜底后，没有个人配置 → 明确不可用（QA 会退化为原文摘编）。"""
    user = _new_user(client)
    view = client.get("/api/v1/llm/config", headers=_headers(user)).json()
    assert view["server_fallback"] is False
    assert view["source"] == "none"
    assert view["effective"] is False
