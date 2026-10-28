"""多轮会话服务：新建/列表/详情/删除 + 消息历史 + 指代追问。

指代追问（AT-04）实现策略：
- 优先：用 LLM 依据历史把"那补考呢？"重写为自包含问题；
- 降级：无 LLM 时把上一轮问题与当前问题拼接作为检索查询（简单共指启发式）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import List, Optional

from ..config import Settings
from ..db import Database
from .llm import LLMClient

_REFERENCE_HINTS = ("那", "这", "它", "他", "她", "这个", "那个", "那么", "那这")


@dataclass
class SessionInfo:
    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
        }


class ConversationService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.llm = LLMClient(settings)

    def create(self, title: str = "新会话", user_id: str = "anonymous") -> SessionInfo:
        sid = uuid.uuid4().hex
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, user_id, title) VALUES (?,?,?)",
                (sid, user_id, title or "新会话"),
            )
        return self.get(sid, user_id=user_id)

    def get(self, session_id: str, user_id: Optional[str] = None) -> SessionInfo:
        with self.db.session() as conn:
            if user_id:
                row = conn.execute(
                    "SELECT session_id, title, created_at, updated_at FROM sessions WHERE session_id = ? AND user_id = ?",
                    (session_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT session_id, title, created_at, updated_at FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        if not row:
            from ..errors import NotFoundError

            raise NotFoundError("会话不存在", detail=session_id)
        count = self.message_count(session_id)
        return SessionInfo(
            session_id=row["session_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=count,
        )

    def list(self, limit: int = 50, user_id: Optional[str] = None) -> List[SessionInfo]:
        with self.db.session() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT session_id, title, created_at, updated_at FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            out.append(
                SessionInfo(
                    session_id=r["session_id"],
                    title=r["title"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    message_count=self.message_count(r["session_id"]),
                )
            )
        return out

    def delete(self, session_id: str, user_id: Optional[str] = None) -> None:
        with self.db.session() as conn:
            if user_id:
                conn.execute("DELETE FROM sessions WHERE session_id = ? AND user_id = ?", (session_id, user_id))
            else:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def message_count(self, session_id: str) -> int:
        with self.db.session() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
            )

    def history(self, session_id: str, limit: Optional[int] = None) -> List[dict]:
        limit = limit or self.settings.session_history_limit
        with self.db.session() as conn:
            rows = conn.execute(
                """
                SELECT role, content, citations, created_at FROM messages
                WHERE session_id = ? ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        rows = list(reversed(rows))
        out = []
        for r in rows:
            try:
                citations = json.loads(r["citations"])
            except (TypeError, json.JSONDecodeError):
                citations = []
            out.append({"role": r["role"], "content": r["content"], "citations": citations, "created_at": r["created_at"]})
        return out

    def add_message(self, session_id: str, role: str, content: str, citations: Optional[List[dict]] = None) -> None:
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, citations) VALUES (?,?,?,?)",
                (session_id, role, content, json.dumps(citations or [], ensure_ascii=False)),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = datetime('now','localtime') WHERE session_id = ?",
                (session_id,),
            )
        # 首条用户消息作为会话标题（截断）
        if role == "user" and self.message_count(session_id) <= 2:
            self._maybe_set_title(session_id, content)

    def _maybe_set_title(self, session_id: str, content: str) -> None:
        title = content.strip()[:20]
        if title:
            with self.db.session() as conn:
                conn.execute(
                    "UPDATE sessions SET title = ? WHERE session_id = ? AND title = '新会话'",
                    (title, session_id),
                )

    def rewrite_question(
        self, question: str, history: List[dict], llm: Optional[LLMClient] = None
    ) -> str:
        """把含指代的追问重写为自包含问题（LLM 优先，失败降级）。

        llm 允许按请求注入用户自带的客户端；不传则用服务器默认配置。
        """
        client = llm or self.llm
        # 无历史或无指代迹象则原样返回
        if not history or not any(h in question for h in _REFERENCE_HINTS):
            return question
        last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
        # 降级启发式：拼接上一轮用户问题
        if not client.available:
            return f"{last_user}；{question}" if last_user else question
        try:
            return client.rewrite(question, history)
        except Exception:  # noqa: BLE001
            return f"{last_user}；{question}" if last_user else question
