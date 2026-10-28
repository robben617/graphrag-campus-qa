"""用户自带大模型密钥（BYOK）：存取、校验、连通性测试。

职责边界：
- 只负责「用户配置」这一份数据，不负责决定用哪把 key 去回答（那是 deps.get_llm_for_user 的事）；
- 对外输出永远是掩码后的视图，明文 key 不出这个模块；
- base_url 由用户填写，属于不可信输入，必须做协议与内网地址校验（SSRF 面）。

为什么 base_url 要拦内网：
这个字段会被服务端拿去发 HTTP 请求。若允许填 `http://169.254.169.254/` 或
`http://10.0.0.5:6379/`，攻击者就能借我们的服务探测内网、甚至读云元数据。
只拦字面量私网地址不是完整防护（DNS 重绑定仍可绕过），但能挡住绝大多数误用与低成本的探测；
生产环境还应配合容器出站策略。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..config import Settings
from ..crypto import SecretBox, key_hint
from ..db import Database
from ..errors import RateLimitedError, UserInputError
from .llm import LLMClient, LLMConfig

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "host.docker.internal",
    "metadata.google.internal",
}

_MIN_KEY_LEN = 8
_probe_lock = threading.Lock()
_probe_last: dict = {}


@dataclass
class UserLLMConfig:
    user_id: str
    base_url: str
    model: str
    api_key: str
    hint: str
    enabled: bool
    updated_at: str = ""

    def to_client(self, settings: Settings) -> LLMClient:
        return LLMClient(
            settings,
            LLMConfig(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model,
                timeout=settings.llm_timeout,
            ),
        )


def normalize_base_url(raw: str, *, allow_private: bool = False) -> str:
    """校验并规范化用户填写的 base_url（末尾不带 /chat/completions）。"""
    value = (raw or "").strip()
    if not value:
        raise UserInputError("请填写 Base URL，例如 https://api.deepseek.com/v1")
    if not value.lower().startswith(("http://", "https://")):
        raise UserInputError("Base URL 必须以 http:// 或 https:// 开头")
    parsed = urlparse(value)
    if not parsed.hostname:
        raise UserInputError("Base URL 格式不正确，缺少域名")
    if parsed.username or parsed.password:
        raise UserInputError("Base URL 里不要包含账号密码，请把 Key 填到单独的输入框")

    host = parsed.hostname.lower()
    if not allow_private:
        blocked = host in _BLOCKED_HOSTS
        try:
            ip = ipaddress.ip_address(host)
            blocked = blocked or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        except ValueError:
            pass  # 不是 IP 字面量，按域名放行（完整 SSRF 防护需出站策略配合）
        if blocked:
            raise UserInputError(
                "Base URL 不能指向本机或内网地址。如果确实需要（例如自建网关），"
                "请在服务端设置 ALLOW_PRIVATE_LLM_BASE_URL=true"
            )

    path = parsed.path.rstrip("/")
    # 常见误填：直接把完整的 /chat/completions 端点粘进来，会导致路径重复
    suffix = "/chat/completions"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}{path}"


def validate_api_key(raw: str) -> str:
    value = (raw or "").strip()
    if len(value) < _MIN_KEY_LEN:
        raise UserInputError("API Key 太短，请检查是否复制完整")
    if any(ch.isspace() for ch in value):
        raise UserInputError("API Key 中不能包含空格或换行，请重新复制")
    return value


def load_config(db: Database, box: SecretBox, user_id: str) -> Optional[UserLLMConfig]:
    with db.session() as conn:
        row = conn.execute(
            "SELECT user_id, base_url, model, api_key_enc, key_hint, enabled, updated_at "
            "FROM user_llm_configs WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    plain = box.decrypt(row["api_key_enc"])
    return UserLLMConfig(
        user_id=row["user_id"],
        base_url=row["base_url"],
        model=row["model"],
        api_key=plain,
        hint=row["key_hint"],
        enabled=bool(row["enabled"]),
        updated_at=row["updated_at"],
    )


def save_config(
    db: Database,
    box: SecretBox,
    settings: Settings,
    user_id: str,
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    enabled: bool = True,
) -> UserLLMConfig:
    """保存配置。api_key 留空表示「沿用已存的 key」，便于只改模型或地址。"""
    existing = load_config(db, box, user_id)

    if api_key.strip():
        plain_key = validate_api_key(api_key)
        encrypted = box.encrypt(plain_key)
        hint = key_hint(plain_key)
    else:
        if not existing or not existing.api_key:
            raise UserInputError("首次配置必须填写 API Key")
        plain_key = existing.api_key
        encrypted = box.encrypt(plain_key)
        hint = existing.hint

    if base_url.strip():
        norm_url = normalize_base_url(base_url, allow_private=settings.allow_private_llm_base_url)
    else:
        norm_url = (existing.base_url if existing else "") or settings.llm_base_url

    norm_model = (model or "").strip() or (existing.model if existing else "") or settings.llm_model
    if len(norm_model) > 64 or " " in norm_model:
        raise UserInputError("模型名称不合法（不能包含空格，长度不超过 64）")

    with db.session() as conn:
        conn.execute(
            """
            INSERT INTO user_llm_configs (user_id, base_url, model, api_key_enc, key_hint, enabled, updated_at)
            VALUES (?,?,?,?,?,?, datetime('now','localtime'))
            ON CONFLICT(user_id) DO UPDATE SET
                base_url = excluded.base_url,
                model = excluded.model,
                api_key_enc = excluded.api_key_enc,
                key_hint = excluded.key_hint,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (user_id, norm_url, norm_model, encrypted, hint, 1 if enabled else 0),
        )
    return UserLLMConfig(
        user_id=user_id,
        base_url=norm_url,
        model=norm_model,
        api_key=plain_key,
        hint=hint,
        enabled=enabled,
    )


def delete_config(db: Database, user_id: str) -> bool:
    with db.session() as conn:
        cur = conn.execute("DELETE FROM user_llm_configs WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0


def build_transient_config(
    settings: Settings, *, api_key: str, base_url: str = "", model: str = ""
) -> UserLLMConfig:
    """构造一份「还没保存」的配置，仅用于连通性测试（不落库）。"""
    plain = validate_api_key(api_key)
    url = (
        normalize_base_url(base_url, allow_private=settings.allow_private_llm_base_url)
        if base_url.strip()
        else settings.llm_base_url
    )
    mdl = (model or "").strip() or settings.llm_model
    if len(mdl) > 64 or " " in mdl:
        raise UserInputError("模型名称不合法（不能包含空格，长度不超过 64）")
    return UserLLMConfig(
        user_id="",
        base_url=url,
        model=mdl,
        api_key=plain,
        hint=key_hint(plain),
        enabled=True,
    )


def public_view(
    settings: Settings, cfg: Optional[UserLLMConfig], *, server_available: bool
) -> dict:
    """给前端的视图：不含明文 key，也不含服务器 key 的任何片段。"""
    has_user = bool(cfg and cfg.api_key)
    if has_user:
        source = "user"
    elif server_available and settings.allow_server_llm_fallback:
        source = "server"
    else:
        source = "none"
    return {
        "configured": has_user,
        "enabled": bool(cfg.enabled) if cfg else False,
        "base_url": cfg.base_url if cfg else "",
        "model": cfg.model if cfg else "",
        "key_hint": cfg.hint if cfg else "",
        "updated_at": cfg.updated_at if cfg else "",
        "source": source,
        "server_fallback": bool(settings.allow_server_llm_fallback and server_available),
        # 前端据此提示「没配 key 就只能看原文摘编」，而不是让用户猜为什么答案变短了
        "effective": source != "none",
    }


def _check_rate_limit(user_id: str, interval: int) -> None:
    now = time.time()
    with _probe_lock:
        last = _probe_last.get(user_id, 0.0)
        if interval > 0 and now - last < interval:
            raise RateLimitedError(
                f"测试过于频繁，请 {int(interval - (now - last)) + 1} 秒后再试"
            )
        _probe_last[user_id] = now


def reset_probe_limiter() -> None:
    """测试用：清空限频状态。"""
    with _probe_lock:
        _probe_last.clear()


def probe(settings: Settings, cfg: UserLLMConfig, *, user_id: str) -> dict:
    """真实发一次最小请求验证 key / 地址 / 模型三者是否匹配。

    不重试：这是人机交互里的"点一下试试"，失败要立刻把原因说清楚，
    重试只会让用户多等两倍时间还看不出区别。
    """
    _check_rate_limit(user_id, settings.llm_config_test_interval)
    client = cfg.to_client(settings)
    start = time.perf_counter()
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 统一转成结构化结果
        detail = getattr(exc, "detail", None) or str(exc)
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "model": cfg.model,
            "message": detail,
        }
    return {
        "ok": True,
        "latency_ms": int((time.perf_counter() - start) * 1000),
        "model": cfg.model,
        "message": "连接成功",
    }
