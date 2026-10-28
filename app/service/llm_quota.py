"""大模型每日额度：限制公开部署下「借用服务器密钥」的生成次数。

为什么需要它：
公开演示时最贵的不是检索（jieba + BGE + RRF 全在本地，零成本），而是最后那一步
生成。若「大模型增强」可以无限点，任何一个人写个循环就能把部署方的额度刷空。
检索链路本身不花钱，所以只要卡住生成这一步，成本上限就是确定的。

设计取舍：
- **按天分区**：day 用本地日期字符串，跨天自然归零，不需要定时清理任务。
- **双闸门**：单用户日额度（防单个刷子）+ 全局日额度（防众生踩踏）。
- **失败即拒绝**：任何异常都按「超额」处理，让回答退回零成本的原文摘编。
  这里选择 fail-closed 而不是 fail-open —— 额度统计坏掉时，宁可答案朴素一点，
  也不能让账单失控；而退回摘编对用户仍是可用结果，不是报错。
- **best-effort 计数**：读-判-写在一个事务内完成，单进程足够准确；多进程部署下
  并发瞬时可能多放行一两次。额度是成本护栏而非计费依据，这个精度够用，
  不必为此引入分布式锁。

隐私：本模块只存「某天、某个 user_id、用了几次」，不存问题内容。
subject 用的是随机生成的 user_id，不是学号/姓名/IP。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..db import Database

_GLOBAL_SUBJECT = "all"
_SCOPE_GLOBAL = "global"
_SCOPE_USER = "user"


@dataclass
class QuotaVerdict:
    """一次额度申请的结果。"""

    allowed: bool
    used_user: int = 0
    used_total: int = 0
    limit_user: int = 0
    limit_total: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "used_user": self.used_user,
            "used_total": self.used_total,
            "limit_user": self.limit_user,
            "limit_total": self.limit_total,
        }


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read(conn, day: str, scope: str, subject: str) -> int:
    row = conn.execute(
        "SELECT count FROM llm_usage WHERE day = ? AND scope = ? AND subject = ?",
        (day, scope, subject),
    ).fetchone()
    return int(row["count"]) if row else 0


def _incr(conn, day: str, scope: str, subject: str) -> None:
    conn.execute(
        """
        INSERT INTO llm_usage (day, scope, subject, count, updated_at)
        VALUES (?,?,?,1, datetime('now','localtime'))
        ON CONFLICT(day, scope, subject) DO UPDATE SET
            count = count + 1,
            updated_at = excluded.updated_at
        """,
        (day, scope, subject),
    )


def consume(
    db: Database,
    *,
    user_id: str,
    per_user_limit: int,
    total_limit: int,
    day: Optional[str] = None,
) -> QuotaVerdict:
    """尝试占用一次生成额度。返回 allowed=False 时调用方应退回原文摘编。

    limit <= 0 表示该维度不限；两个维度都不限时直接放行，不碰数据库。
    """
    if per_user_limit <= 0 and total_limit <= 0:
        return QuotaVerdict(allowed=True, limit_user=per_user_limit, limit_total=total_limit)

    today = day or _today()
    try:
        with db.session() as conn:
            used_total = _read(conn, today, _SCOPE_GLOBAL, _GLOBAL_SUBJECT)
            used_user = _read(conn, today, _SCOPE_USER, user_id or _GLOBAL_SUBJECT)

            if total_limit > 0 and used_total >= total_limit:
                return QuotaVerdict(
                    allowed=False,
                    used_user=used_user,
                    used_total=used_total,
                    limit_user=per_user_limit,
                    limit_total=total_limit,
                    message=(
                        f"今日全站「大模型增强」额度（{total_limit} 次）已用完，"
                        "已自动改为返回知识库原文摘编。"
                    ),
                )
            if per_user_limit > 0 and used_user >= per_user_limit:
                return QuotaVerdict(
                    allowed=False,
                    used_user=used_user,
                    used_total=used_total,
                    limit_user=per_user_limit,
                    limit_total=total_limit,
                    message=(
                        f"你今天的「大模型增强」额度（{per_user_limit} 次）已用完，"
                        "已自动改为返回知识库原文摘编，明天恢复。"
                    ),
                )

            _incr(conn, today, _SCOPE_GLOBAL, _GLOBAL_SUBJECT)
            _incr(conn, today, _SCOPE_USER, user_id or _GLOBAL_SUBJECT)
    except Exception:  # noqa: BLE001 - 统计失败不得让问答报错，按超额处理
        return QuotaVerdict(
            allowed=False,
            limit_user=per_user_limit,
            limit_total=total_limit,
            message="额度统计暂不可用，本次已改为返回知识库原文摘编。",
        )

    return QuotaVerdict(
        allowed=True,
        used_user=used_user + 1,
        used_total=used_total + 1,
        limit_user=per_user_limit,
        limit_total=total_limit,
    )


def snapshot(db: Database, *, user_id: str, day: Optional[str] = None) -> dict:
    """只读查询今日用量（供运维/前端展示，不占额度）。"""
    today = day or _today()
    try:
        with db.session() as conn:
            return {
                "day": today,
                "used_user": _read(conn, today, _SCOPE_USER, user_id or _GLOBAL_SUBJECT),
                "used_total": _read(conn, today, _SCOPE_GLOBAL, _GLOBAL_SUBJECT),
            }
    except Exception:  # noqa: BLE001
        return {"day": today, "used_user": 0, "used_total": 0}
