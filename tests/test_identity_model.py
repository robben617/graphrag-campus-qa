"""身份策略（学生自助 / 名单注入）与模型配置（请求级转发 / 管理员默认模型）测试。

这一组守四条边界，任何一条破了都会造成真实问题：

1. **老师与管理员不能自助获得**：否则「老师」在校内场景里没有任何背书，谁都能给自己发一个；
2. **名单导入必须逐条给明细且可重复执行**：一份上百人的名单不能因为一行错就整批失败；
3. **自定义模型密钥不落库**：只随请求头经过服务端，用完即弃（用户能自己看见、能删掉）；
4. **系统默认模型只有管理员能改**，且任何接口都不回明文。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database

FAKE_KEY = "sk-fake-abcdef-123456"

# 一个必然连不上的地址：用于验证「有客户端但调用失败 → 降级」，
# 而不是「没有客户端 → 直接摘编」。两者在响应里的区别是 can_enhance。
LLM_HEADERS = {
    "X-LLM-Key": FAKE_KEY,
    "X-LLM-Base-Url": "https://api.invalid/v1",
    "X-LLM-Model": "fake-chat",
}


def _settings(imported_db, **overrides) -> Settings:
    base = dict(
        db_path=imported_db.path,
        llm_config_secret="unit-test-secret",
        llm_config_test_interval=0,
        llm_enabled=False,
        llm_api_key="",
        allow_server_llm_fallback=False,
        deploy_mode="selfhost",
        llm_timeout=3,
    )
    base.update(overrides)
    return Settings(**base)


def _client(settings) -> TestClient:
    from app.web.server import create_app

    return TestClient(create_app(settings))


def _admin(client) -> dict:
    """空库首个账号免鉴权（bootstrap），用它建立管理员。"""
    r = client.post("/api/v1/users", json={"username": "root_admin", "role": "admin"})
    assert r.status_code == 200, r.text
    return r.json()


def _h(user: dict) -> dict:
    return {"X-User-Token": user["api_token"]}


# ---------- 身份：谁能创建谁 ----------


def test_bootstrap_first_user_may_be_admin(imported_db):
    """空库死锁的唯一解：第一个账号必须是任意角色，否则永远建不出管理员。"""
    client = _client(_settings(imported_db))
    r = client.post("/api/v1/users", json={"username": "first_root", "role": "admin"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_student_can_self_register_but_not_teacher(imported_db):
    client = _client(_settings(imported_db))
    _admin(client)

    r = client.post("/api/v1/users", json={"username": "stu_self", "role": "student"})
    assert r.status_code == 200
    assert r.json()["role"] == "student"

    r = client.post("/api/v1/users", json={"username": "fake_teacher", "role": "teacher"})
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"

    r = client.post("/api/v1/users", json={"username": "fake_admin", "role": "admin"})
    assert r.status_code == 403


def test_admin_can_create_teacher(imported_db):
    client = _client(_settings(imported_db))
    admin = _admin(client)
    r = client.post(
        "/api/v1/users",
        json={"username": "teacher_1", "role": "teacher"},
        headers=_h(admin),
    )
    assert r.status_code == 200
    assert r.json()["role"] == "teacher"


# ---------- 名单：批量注入 ----------


def test_bulk_import_reports_each_line(imported_db):
    """一行一个结果：新建 / 重复 / 非法角色分别落到 created / skipped / failed。"""
    client = _client(_settings(imported_db))
    admin = _admin(client)
    payload = {
        "users": [
            {"username": "s1", "role": "student"},
            {"username": "t1", "role": "teacher"},
            {"username": "s1", "role": "student"},          # 名单内重复
            {"username": "bad1", "role": "principal"},      # 非法角色
        ]
    }
    r = client.post("/api/v1/users/bulk", json=payload, headers=_h(admin))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["summary"] == {"created": 2, "skipped": 1, "failed": 1}
    assert {c["username"] for c in data["created"]} == {"s1", "t1"}
    assert data["skipped"][0]["line"] == 3
    assert data["failed"][0]["line"] == 4
    # 新建的条目必须带 token（管理员分发的唯一时机）
    assert all(len(c["api_token"]) >= 16 for c in data["created"])


def test_bulk_import_is_idempotent(imported_db):
    """重复提交同一份名单：全部落到 skipped，不产生重复账号。"""
    client = _client(_settings(imported_db))
    admin = _admin(client)
    payload = {"users": [{"username": "dup_a", "role": "student"}]}
    first = client.post("/api/v1/users/bulk", json=payload, headers=_h(admin)).json()
    assert first["summary"]["created"] == 1
    second = client.post("/api/v1/users/bulk", json=payload, headers=_h(admin)).json()
    assert second["summary"] == {"created": 0, "skipped": 1, "failed": 0}


def test_bulk_dry_run_does_not_write(imported_db):
    client = _client(_settings(imported_db))
    admin = _admin(client)
    before = len(client.get("/api/v1/users", headers=_h(admin)).json())
    r = client.post(
        "/api/v1/users/bulk",
        json={"users": [{"username": "preview_only", "role": "teacher"}], "dry_run": True},
        headers=_h(admin),
    )
    assert r.status_code == 200
    assert r.json()["summary"]["created"] == 1
    after = len(client.get("/api/v1/users", headers=_h(admin)).json())
    assert after == before


def test_bulk_requires_admin(imported_db):
    client = _client(_settings(imported_db))
    _admin(client)
    stu = client.post("/api/v1/users", json={"username": "stu_bulk", "role": "student"}).json()
    r = client.post(
        "/api/v1/users/bulk",
        json={"users": [{"username": "x1", "role": "teacher"}]},
        headers=_h(stu),
    )
    assert r.status_code == 403


# ---------- 模型：请求级凭据不落库 ----------


def test_transient_llm_headers_are_used_but_not_persisted(imported_db):
    """带请求头提问：路由认为确实有可用模型（can_enhance=True），
    但 user_llm_configs 里一行都不该有——密钥不落库是这套方案的核心承诺。"""
    settings = _settings(imported_db)
    client = _client(settings)
    client.post("/api/v1/users", json={"username": "u1", "role": "student"})

    r = client.post(
        "/api/v1/ask",
        json={"question": "学生申诉应该找哪个部门受理？", "mode": "extractive"},
        headers=LLM_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "原文摘编"
    assert body["can_enhance"] is True       # 路由判定「有可用模型」，来源就是请求头
    assert body["degraded"] is False         # 摘编是主动选择，不是故障降级

    db = Database(settings.db_path)
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM user_llm_configs").fetchone()["c"]
    assert n == 0


def test_public_mode_ignores_transient_headers(imported_db):
    """公开站点不采集密钥：即使有人手工塞请求头，也不认。"""
    settings = _settings(imported_db, deploy_mode="public")
    client = _client(settings)
    client.post("/api/v1/users", json={"username": "u2", "role": "student"})

    r = client.post(
        "/api/v1/ask",
        json={"question": "学生申诉应该找哪个部门受理？", "mode": "extractive"},
        headers=LLM_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["can_enhance"] is False


def test_request_llm_config_parses_headers(tmp_path):
    """单元级：请求头 → LLMConfig 的解析与禁用规则。"""
    from starlette.requests import Request

    from app.web import deps

    def _req(headers: dict) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})

    selfhost = Settings(
        db_path=str(tmp_path / "a.db"),
        deploy_mode="selfhost",
        llm_config_secret="unit-test-secret",
    )
    cfg = deps.request_llm_config(_req(LLM_HEADERS), selfhost)
    assert cfg is not None
    assert cfg.api_key == FAKE_KEY
    assert cfg.base_url == "https://api.invalid/v1"
    assert cfg.model == "fake-chat"

    # public：完全忽略，避免「说不采集却能收」的口径分裂
    public = Settings(
        db_path=str(tmp_path / "b.db"),
        deploy_mode="public",
        llm_config_secret="unit-test-secret",
    )
    assert deps.request_llm_config(_req(LLM_HEADERS), public) is None

    # 没带 key 时不做任何推断（不能拿空 key 去覆盖服务端配置）
    assert deps.request_llm_config(_req({"X-LLM-Model": "m"}), selfhost) is None


# ---------- 系统默认模型：仅管理员可改，只回掩码 ----------


def test_system_default_is_admin_only(imported_db):
    client = _client(_settings(imported_db))
    _admin(client)
    stu = client.post("/api/v1/users", json={"username": "stu_d", "role": "student"}).json()

    assert client.get("/api/v1/llm/default", headers=_h(stu)).status_code == 403
    assert (
        client.put("/api/v1/llm/default", json={"model": "x"}, headers=_h(stu)).status_code
        == 403
    )
    assert client.delete("/api/v1/llm/default", headers=_h(stu)).status_code == 403


def test_system_default_masked_and_encrypted(imported_db):
    settings = _settings(imported_db)
    client = _client(settings)
    admin = _admin(client)

    r = client.put(
        "/api/v1/llm/default",
        json={
            "api_key": FAKE_KEY,
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        },
        headers=_h(admin),
    )
    assert r.status_code == 200, r.text
    assert FAKE_KEY not in r.text           # 明文绝不出现在响应里
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "db"
    assert body["key_hint"].endswith(FAKE_KEY[-4:])

    got = client.get("/api/v1/llm/default", headers=_h(admin)).json()
    assert got["model"] == "deepseek-chat"
    assert FAKE_KEY not in str(got)

    # 密文落库：库里搜不到明文
    with Database(settings.db_path).session() as conn:
        row = conn.execute(
            "SELECT api_key_enc FROM system_llm_config WHERE id = 1"
        ).fetchone()
    assert row["api_key_enc"]
    assert FAKE_KEY not in row["api_key_enc"]


def test_system_default_delete_falls_back_to_env(imported_db):
    client = _client(_settings(imported_db))
    admin = _admin(client)
    client.put(
        "/api/v1/llm/default",
        json={"api_key": FAKE_KEY, "model": "deepseek-chat"},
        headers=_h(admin),
    )
    r = client.delete("/api/v1/llm/default", headers=_h(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True
    assert body["configured"] is False
    # 删掉页面配置后回落到 .env；本用例没有配 .env，因此是 none
    assert body["source"] in {"env", "none"}
