"""可选 LLM 客户端（OpenAI Chat Completions 兼容）。

设计（对齐 §6 基础版#3、§7.2 降级）：
- 未启用/无密钥/调用失败 → 抛 LLMUnavailable，由 QA 层降级为"原文摘编"。
- 带简单重试（最多 2 次，指数退避）。
- 依赖 requests（若未安装则视为不可用）。
"""

from __future__ import annotations

import time
from typing import List, Optional

from ..config import Settings
from ..errors import LLMUnavailable

_SYSTEM_PROMPT = (
    "你是校园知识问答助手。你只能依据用户提供的【检索资料】回答，"
    "不得编造资料中不存在的校规、条款编号、部门名称或数字。"
    "回答要求：1) 用简洁中文；2) 如资料不足以回答，明确说'现有资料中未找到相关规定'并给出建议咨询部门；"
    "3) 在句末用〔资料N〕标注依据；4) 不输出与问题无关内容。"
)


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def available(self) -> bool:
        return self.settings.llm_enabled and bool(self.settings.llm_api_key)

    def generate(self, question: str, evidence: List[dict]) -> str:
        """evidence: [{doc_title, chapter, article_no, content}]"""
        if not self.available:
            raise LLMUnavailable(detail="未配置 LLM_API_KEY 或未启用 LLM")
        try:
            import requests  # noqa: WPS433
        except ImportError as exc:
            raise LLMUnavailable(detail="requests 未安装") from exc

        evidence_text = "\n\n".join(
            f"【资料{i+1}】《{e['doc_title']}》"
            + (f" {e['chapter']}" if e.get("chapter") else "")
            + (f" 第{e['article_no']}条" if e.get("article_no") else "")
            + f"\n{e['content']}"
            for i, e in enumerate(evidence)
        )
        user_prompt = f"问题：{question}\n\n【检索资料】\n{evidence_text}"

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"

        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.settings.llm_timeout)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(min(2 ** attempt, 3))
        raise LLMUnavailable(detail=f"LLM 调用失败: {last_err}")

    def rewrite(self, question: str, history: List[dict]) -> str:
        """依据多轮历史把含指代的追问重写为自包含问题（AT-04）。"""
        if not self.available:
            raise LLMUnavailable(detail="LLM 未配置")
        try:
            import requests  # noqa: WPS433
        except ImportError as exc:
            raise LLMUnavailable(detail="requests 未安装") from exc

        history_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content']}" for m in history[-6:]
        )
        system = (
            "你是校园知识问答的多轮对话改写器。把用户的追问改写成不依赖上下文的完整问题，"
            "保留原意、补充被指代的对象（如'那补考呢'→'补考的规定是什么'）。只输出改写后的问题，不加解释。"
        )
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"对话历史：\n{history_text}\n\n当前追问：{question}\n\n改写结果："},
            ],
            "temperature": 0.0,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        resp = requests.post(url, json=payload, headers=headers, timeout=self.settings.llm_timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
