"""部署形态（public / selfhost）与「摘编优先 + 增强 + 额度」策略测试。

这一组用例守的是三条产品边界，任何一条破了都会造成实际损失：

1. **公开站点不采集用户密钥**：写接口必须 403，前端据 byok_allowed=false 隐藏入口；
2. **公开站点默认零成本**：不显式要求时返回原文摘编，而不是直接花部署方的钱；
3. **额度用完必须优雅降级**：退回摘编并说明原因，绝不能报 500、也不能静默继续花钱。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.errors import UserInputError
from app.service import llm_quota
from app.service.llm import LLMClient, LLMConfig
from app.service.qa import QAService
from app.web.server import plan_answer, resolve_answer_mode

FAKE_KEY = "sk-fake-000111222333"


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        db_path=str(tmp_path / "kb.db"),
        llm_config_secret="unit-test-secret",
        llm_config_test_interval=0,
    )
    base.update(overrides)
    return Settings(**base)


def _db(settings) -> Database:
    database = Database(settings.db_path)
    database.init_schema()
    return database


def _client(settings) -> TestClient:
    from app.web.server import create_app

    return TestClient(create_app(settings))


def _available_client(settings) -> LLMClient:
    """一个 available=True 的客户端；只用于判断策略，不会被真的调用。"""
    return LLMClient(
        settings,
        LLMConfig(base_url="https://api.example.com/v1", api_key=FAKE_KEY, model="fake-chat"),
    )


def _unavailable_client(settings) -> LLMClient:
    return LLMClient(
        settings, LLMConfig(base_url="", api_key="", model="", timeout=settings.llm_timeout)
    )


# ---------- 回答方式的解析 ----------


def test_resolve_mode_follows_deploy_mode(tmp_path):
    public = _settings(tmp_path, deploy_mode="public")
    selfhost = _settings(tmp_path, deploy_mode="selfhost")
    assert resolve_answer_mode("auto", public) == "extractive"
    assert resolve_answer_mode("auto", selfhost) == "llm"
    # 显式指定优先于部署形态
    assert resolve_answer_mode("llm", public) == "llm"
    assert resolve_answer_mode("extractive", selfhost) == "extractive"
    # 空值按 auto 处理（老前端不带这个字段也能工作）
    assert resolve_answer_mode(None, public) == "extractive"


def test_resolve_mode_rejects_unknown_value(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(UserInputError) as exc:
        resolve_answer_mode("fast", settings)
    assert "auto" in exc.value.message


def test_public_default_is_extractive_but_can_enhance(tmp_path):
    settings = _settings(tmp_path, deploy_mode="public", llm_enabled=True, llm_api_key=FAKE_KEY)
    plan = plan_answer(settings, _db(settings), "u1", _available_client(settings), "auto")
    assert plan.generate is False
    assert plan.notice is None
    # 有大模型可用 → 前端要显示"切到大模型增强"的引导
    assert plan.enhance is True


def test_selfhost_default_generates(tmp_path):
    settings = _settings(tmp_path, deploy_mode="selfhost", llm_enabled=True, llm_api_key=FAKE_KEY)
    plan = plan_answer(settings, _db(settings), "u1", _available_client(settings), "auto")
    assert plan.generate is True


def test_missing_key_degrades_with_notice(tmp_path):
    settings = _settings(tmp_path, deploy_mode="public", llm_enabled=False, llm_api_key="")
    plan = plan_answer(settings, _db(settings), "u1", _unavailable_client(settings), "llm")
    assert plan.generate is False
    assert "原文摘编" in plan.notice
    assert plan.enhance is False


# ---------- 额度 ----------


def test_quota_blocks_when_user_limit_reached(tmp_path):
    settings = _settings(
        tmp_path,
        deploy_mode="public",
        llm_enabled=True,
        llm_api_key=FAKE_KEY,
        public_llm_daily_per_user=1,
        public_llm_daily_total=100,
    )
    db = _db(settings)
    first = llm_quota.consume(db, user_id="u1", per_user_limit=1, total_limit=100)
    assert first.allowed is True

    plan = plan_answer(settings, db, "u1", _available_client(settings), "llm")
    assert plan.generate is False
    assert "额度" in plan.notice
    # 额度用完后不再引导增强，避免用户反复点却一直失败
    assert plan.enhance is False


def test_quota_blocks_when_global_limit_reached(tmp_path):
    settings = _settings(tmp_path)
    db = _db(settings)
    llm_quota.consume(db, user_id="a", per_user_limit=0, total_limit=2)
    llm_quota.consume(db, user_id="b", per_user_limit=0, total_limit=2)
    verdict = llm_quota.consume(db, user_id="c", per_user_limit=0, total_limit=2)
    assert verdict.allowed is False
    assert "全站" in verdict.message


def test_quota_counts_per_day(tmp_path):
    settings = _settings(tmp_path)
    db = _db(settings)
    llm_quota.consume(db, user_id="u1", per_user_limit=2, total_limit=0, day="2026-01-01")
    llm_quota.consume(db, user_id="u1", per_user_limit=2, total_limit=0, day="2026-01-01")
    same_day = llm_quota.consume(db, user_id="u1", per_user_limit=2, total_limit=0, day="2026-01-01")
    next_day = llm_quota.consume(db, user_id="u1", per_user_limit=2, total_limit=0, day="2026-01-02")
    assert same_day.allowed is False
    assert next_day.allowed is True


def test_quota_unlimited_when_both_limits_off(tmp_path):
    db = _db(_settings(tmp_path))
    for _ in range(5):
        assert llm_quota.consume(db, user_id="u1", per_user_limit=0, total_limit=0).allowed is True


def test_quota_fails_closed_on_storage_error(tmp_path):
    """额度统计坏掉时按超额处理：宁可答案朴素，也不能让账单失控。"""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    broken = Database(str(blocker / "sub" / "kb.db"))
    verdict = llm_quota.consume(broken, user_id="u1", per_user_limit=5, total_limit=50)
    assert verdict.allowed is False
    assert verdict.message


# ---------- 接口层 ----------


def test_public_mode_hides_byok_and_rejects_writes(tmp_path):
    settings = _settings(
        tmp_path,
        deploy_mode="public",
        llm_enabled=True,
        llm_api_key=FAKE_KEY,
        allow_server_llm_fallback=True,
    )
    client = _client(settings)

    # 读状态仍然 200：前端要据此判断"藏不藏入口"
    view = client.get("/api/v1/llm/config").json()
    assert view["byok_allowed"] is False
    assert view["default_mode"] == "extractive"

    # 三个写接口一律 403，且 detail 可区分于"未登录"
    for resp in (
        client.put("/api/v1/llm/config", json={"api_key": FAKE_KEY}),
        client.delete("/api/v1/llm/config"),
        client.post("/api/v1/llm/config/test", json={"api_key": FAKE_KEY}),
    ):
        assert resp.status_code == 403
        assert resp.json()["detail"] == "byok_disabled"


def test_selfhost_mode_allows_byok(tmp_path):
    settings = _settings(
        tmp_path,
        deploy_mode="selfhost",
        llm_enabled=True,
        llm_api_key=FAKE_KEY,
        allow_server_llm_fallback=True,
    )
    client = _client(settings)
    view = client.get("/api/v1/llm/config").json()
    assert view["byok_allowed"] is True
    assert view["default_mode"] == "llm"

    user = client.post("/api/v1/users", json={"username": "tester", "role": "student"}).json()
    headers = {"X-User-Token": user["api_token"]}
    saved = client.put(
        "/api/v1/llm/config", json={"api_key": FAKE_KEY, "model": "own-model"}, headers=headers
    )
    assert saved.status_code == 200
    assert saved.json()["source"] == "user"


def test_api_rejects_bad_mode(tmp_path):
    settings = _settings(tmp_path, deploy_mode="public")
    client = _client(settings)
    resp = client.post("/api/v1/ask", json={"question": "奖学金申请条件？", "mode": "fast"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_INPUT"


def test_health_exposes_deploy_mode(tmp_path):
    settings = _settings(tmp_path, deploy_mode="selfhost")
    body = _client(settings).get("/api/v1/health").json()
    assert body["deploy_mode"] == "selfhost"
    assert body["byok_allowed"] is True
    assert body["default_answer_mode"] == "llm"


# ---------- 服务层：摘编答案的形态 ----------


def test_extractive_answer_shape(imported_db, tmp_path):
    """摘编模式不调模型，但仍要给出完整答案 + 引用，并标记可增强。"""
    settings = _settings(
        tmp_path,
        deploy_mode="public",
        llm_enabled=True,
        llm_api_key=FAKE_KEY,
        allow_server_llm_fallback=True,
    )
    qa = QAService(imported_db, settings)
    ans = qa.ask("奖学金评选的比例是多少？", generate=False, enhance_allowed=True)

    assert ans.found is True
    assert ans.mode == "原文摘编"
    assert ans.citations
    assert ans.can_enhance is True
    assert ans.degraded is False
    payload = ans.to_dict()
    assert payload["mode_notice"] is None
    assert payload["can_enhance"] is True


def test_faq_answer_skips_llm_even_in_llm_mode(imported_db, tmp_path):
    """FAQ 是确定性答案：即使请求大模型生成也不该花钱，也不该引导"增强"。"""
    settings = _settings(
        tmp_path, deploy_mode="public", llm_enabled=True, llm_api_key=FAKE_KEY
    )
    qa = QAService(imported_db, settings)
    ans = qa.ask("奖学金申请需要什么条件？", generate=True, enhance_allowed=True)

    assert ans.mode == "精确FAQ"
    assert ans.can_enhance is False


def test_extractive_answer_keeps_enhance_hint(imported_db, tmp_path):
    """回归用例：摘编模式下传的是「不可用客户端」，不能因此把增强引导吞掉。

    摘编模式会用 disabled 客户端替换真实客户端（为了让多轮改写也走零成本分支），
    若用 client.available 判断能否增强，引导将永远不出现——线上表现为
    「公开站点永远看不到改用大模型的入口」。
    """
    settings = _settings(
        tmp_path, deploy_mode="public", llm_enabled=True, llm_api_key=FAKE_KEY
    )
    plan = plan_answer(settings, _db(settings), "u1", _available_client(settings), "auto")
    assert plan.generate is False
    assert plan.enhance is True
    assert plan.llm.available is False  # 传下去的确实是不可用客户端

    qa = QAService(imported_db, settings)
    ans = qa.ask(
        "奖学金评选的比例是多少？",
        llm=plan.llm,
        generate=plan.generate,
        extractive_notice=plan.notice,
        enhance_allowed=plan.enhance,
    )
    assert ans.mode == "原文摘编"
    assert ans.can_enhance is True


def test_anonymous_only_limited_by_global_quota(tmp_path):
    """匿名访客共用 user_id，若按人计额会把全站几十人压进同一份额度。"""
    from app.service import users as users_mod

    settings = _settings(
        tmp_path,
        deploy_mode="public",
        llm_enabled=True,
        llm_api_key=FAKE_KEY,
        public_llm_daily_per_user=1,
        public_llm_daily_total=100,
    )
    db = _db(settings)
    anon = users_mod.DEFAULT_USER
    for _ in range(3):
        assert plan_answer(settings, db, anon, _available_client(settings), "llm").generate is True

    # 登录身份才单独计人：第二次即被挡住
    assert plan_answer(settings, db, "u1", _available_client(settings), "llm").generate is True
    assert plan_answer(settings, db, "u1", _available_client(settings), "llm").generate is False
