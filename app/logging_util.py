"""结构化日志：统一格式 request_id / session_id / 耗时 / 事件。

输出单行可解析日志（键值对），便于定位（对齐 §7.1「记录检索命中数、模型耗时、降级原因」）。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Optional


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
        for attr in ("request_id", "session_id", "elapsed_ms", "event", "mode", "hits", "degraded"):
            val = getattr(record, attr, None)
            if val is not None:
                payload[attr] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
    **extra: Any,
) -> None:
    kwargs = {"event": event}
    if request_id:
        kwargs["request_id"] = request_id
    if session_id:
        kwargs["session_id"] = session_id
    if elapsed_ms is not None:
        kwargs["elapsed_ms"] = elapsed_ms
    kwargs.update(extra)
    logger.info(event, extra=kwargs)
