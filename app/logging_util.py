"""结构化日志：统一格式 request_id / session_id / 耗时 / 事件。

输出单行可解析日志（键值对），便于定位（对齐 §7.1「记录检索命中数、模型耗时、降级原因」）。

**踩过的坑**：`logging.Logger.info(..., extra=...)` 会直接把 extra 的键写进 LogRecord，
一旦键名与 LogRecord 的内置属性重名（`created` / `name` / `message` / `asctime` / `levelno` …），
标准库会抛 `KeyError: Attempt to overwrite 'created' in LogRecord`——
这不是日志丢一条，而是整个请求 500。
因此这里做两件事：写入前对冲突键加前缀，输出时把剩余自定义字段一并序列化。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

# LogRecord 自带的属性名：既不能覆盖，也不该重复输出
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "exc_text", "stack_info"}


def _safe_extra(extra: dict) -> dict:
    """把会与 LogRecord 内置属性冲突的键改名，而不是让整个请求失败。"""
    safe: dict = {}
    for key, value in extra.items():
        safe[f"x_{key}" if key in _STANDARD_ATTRS else key] = value
    return safe


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(h, _JSONFormatter) for h in root.handlers):
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JSONFormatter())
    root.handlers = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # 除标准属性外全部输出：否则调用方传了 actor / n_created 也看不见，
        # 日志里"有字段"和"能查到"是两件事。
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
    **extra: Any,
) -> None:
    kwargs: dict = {"event": event}
    if request_id:
        kwargs["request_id"] = request_id
    if session_id:
        kwargs["session_id"] = session_id
    if elapsed_ms is not None:
        kwargs["elapsed_ms"] = elapsed_ms
    kwargs.update(_safe_extra(extra))
    logger.info(event, extra=kwargs)
