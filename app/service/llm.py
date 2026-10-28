"""可选 LLM 客户端（OpenAI Chat Completions 兼容）。

设计：
- 支持两种密钥来源：**服务器默认配置**（settings）与**用户自带配置**（LLMConfig）。
  前者由运营方付费，后者由用户自己付费；两条路径共用同一套请求与降级逻辑，
  避免出现"线上用用户 key、测试用服务器 key"两套行为对不上的问题。
- 未启用/无密钥/调用失败 → 抛 LLMUnavailable，由 QA 层降级为"原文摘编"。
- 重试：最多 2 次，指数退避 + 抖动（避免多实例同时重试形成尖峰）。
- 错误信息返回前脱敏：任何异常文本里若出现 key，替换为 ***。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import List, Optional

from ..config import Settings
from ..errors import LLMUnavailable

_SYSTEM_PROMPT = (
    "你是校园知识问答助手。你只能依据用户提供的【检索资料】回答，"
    "不得编造资料中不存在的校规、条款编号、部门名称或数字。"
    "回答要求：1) 用简洁中文；2) 如资料不足以回答，明确说'现有资料中未找到相关规定'并给出建议咨询部门；"
    "3) 在句末用〔资料N〕标注依据；4) 不输出与问题无关内容。"
)

# 单次调用的最大重试次数（含首次）。取 2 次是为了在偶发网络抖动与响应时延之间折中：
# 再多会让用户等待明显变长，而 LLM 类服务多数故障不是靠重试能恢复的。
_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class LLMConfig:
    """一次调用所需的全部连接信息（与来源解耦）。"""

    base_url: str
    api_key: str
    model: str
    timeout: int = 60

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


class LLMClient:
    def __init__(self, settings: Settings, config: Optional[LLMConfig] = None):
        self.settings = settings
        # 显式传入配置 = 用户自带密钥；未传 = 走服务器默认
        self.explicit = config is not None
        self.config = config or LLMConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )

    @property
    def available(self) -> bool:
        if not self.config.api_key or not self.config.base_url:
            return False
        # 用户显式配置时不受全局 LLM_ENABLED 开关影响（他已经明确要用自己的 key）
        if self.explicit:
            return True
        return self.settings.llm_enabled

    def _redact(self, text: str) -> str:
        key = self.config.api_key
        if key and key in text:
            return text.replace(key, "***")
        return text

    def _post(self, payload: dict) -> dict:
        if not self.available:
            raise LLMUnavailable(detail="未配置可用的 LLM 密钥")
        try:
            import requests  # noqa: WPS433
        except ImportError as exc:
            raise LLMUnavailable(detail="requests 未安装") from exc

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        last_err: Optional[Exception] = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = requests.post(
                    self.config.endpoint, json=payload, headers=headers, timeout=self.config.timeout
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - 全部转为可降级的统一异常
                last_err = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    # 指数退避 + 抖动：0.8~1.0s、1.6~2.0s
                    time.sleep(min(2 ** attempt, 3) * random.uniform(0.8, 1.0))
        raise LLMUnavailable(detail=self._redact(f"LLM 调用失败: {last_err}"))

    def generate(self, question: str, evidence: List[dict]) -> str:
        """evidence: [{doc_title, chapter, article_no, content}]"""
        evidence_text = "\n\n".join(
            f"【资料{i+1}】《{e['doc_title']}》"
            + (f" {e['chapter']}" if e.get("chapter") else "")
            + (f" 第{e['article_no']}条" if e.get("article_no") else "")
            + f"\n{e['content']}"
            for i, e in enumerate(evidence)
        )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"问题：{question}\n\n【检索资料】\n{evidence_text}"},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        data = self._post(payload)
        return data["choices"][0]["message"]["content"].strip()

    def rewrite(self, question: str, history: List[dict]) -> str:
        """依据多轮历史把含指代的追问重写为自包含问题（AT-04）。"""
        history_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content']}" for m in history[-6:]
        )
        system = (
            "你是校园知识问答的多轮对话改写器。把用户的追问改写成不依赖上下文的完整问题，"
            "保留原意、补充被指代的对象（如'那补考呢'→'补考的规定是什么'）。只输出改写后的问题，不加解释。"
        )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"对话历史：\n{history_text}\n\n当前追问：{question}\n\n改写结果："},
            ],
            "temperature": 0.0,
            "stream": False,
        }
        data = self._post(payload)
        return data["choices"][0]["message"]["content"].strip()

    def ping(self) -> str:
        """最小连通性探测：只发一次、不重试，失败直接把原因抛给调用方。"""
        try:
            import requests  # noqa: WPS433
        except ImportError as exc:
            raise LLMUnavailable(detail="requests 未安装") from exc

        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 4,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                self.config.endpoint, json=payload, headers=headers, timeout=min(self.config.timeout, 20)
            )
            resp.raise_for_status()
            data = resp.json()
            if "choices" not in data:
                raise ValueError("响应中没有 choices 字段，可能不是 OpenAI 兼容接口")
            return "ok"
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(detail=self._redact(_friendly_error(exc))) from exc


def _friendly_error(exc: Exception) -> str:
    """把 requests 异常翻译成用户能看懂的一句话，避免把整个堆栈甩到界面上。"""
    import requests  # noqa: WPS433

    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 401:
            return "认证失败（401）：API Key 无效或已过期"
        if status == 403:
            return "无权限（403）：该 Key 不能访问此模型"
        if status == 404:
            return "接口不存在（404）：请检查 base_url 是否包含正确的版本路径（如 /v1）"
        if status == 429:
            return "触发限流（429）：该 Key 的额度或频率已用尽"
        return f"服务返回错误（{status}）"
    if isinstance(exc, requests.exceptions.ConnectTimeout) or isinstance(exc, requests.exceptions.ReadTimeout):
        return "连接超时：请检查 base_url 是否可达、网络是否需要代理"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "连接失败：域名无法解析或对方拒绝连接"
    return f"{type(exc).__name__}: {exc}"
