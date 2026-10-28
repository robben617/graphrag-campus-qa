"""统一错误类型与面向用户的提示文案。

设计（对应设计文档 §6 基础版错误处理、§7.1 关键指标）：
- 校验类错误（空问题/超长）→ UserInputError（HTTP 400）
- 权限不足 → ForbiddenError（HTTP 403）：请求本身合法，只是当前角色无权
- 数据库不可用 → StorageUnavailable（HTTP 503）
- 知识库为空 / 无结果 → 在回答层返回"拒答"文本（非异常），便于下游直接展示
- 服务内部错误 → InternalError（HTTP 500），日志带 request_id 可定位
"""

from __future__ import annotations

from typing import Optional


class AppError(Exception):
    """业务错误基类。"""

    http_status: int = 400
    code: str = "APP_ERROR"

    def __init__(self, message: str, *, detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self, request_id: Optional[str] = None) -> dict:
        payload: dict = {"code": self.code, "message": self.message, "request_id": request_id}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class UserInputError(AppError):
    """用户输入不合法（空问题、超长等），返回可理解的中文提示。"""

    http_status = 400
    code = "INVALID_INPUT"


class EmptyQuestionError(UserInputError):
    code = "EMPTY_QUESTION"

    def __init__(self):
        super().__init__("问题不能为空，请输入你想了解的内容，例如：奖学金申请需要什么条件？")


class QuestionTooLongError(UserInputError):
    code = "QUESTION_TOO_LONG"

    def __init__(self, limit: int):
        super().__init__(f"问题过长：最多支持 {limit} 个字符，请精简后再提问。")


class ForbiddenError(AppError):
    """权限不足：当前角色无权执行该操作（HTTP 403）。

    与 UserInputError（400）的区别：400 表示"请求本身不合法"，
    403 表示"请求合法、参数也对，但你没这个权限"。
    分开之后，前端可以只凭状态码就跳到"切换身份"引导，不必解析中文文案。
    """

    http_status = 403
    code = "FORBIDDEN"

    def __init__(self, message: str = "权限不足", *, detail: Optional[str] = None):
        super().__init__(message, detail=detail)


class StorageUnavailable(AppError):
    """数据库不可用（文件缺失/损坏/权限等）。"""

    http_status = 503
    code = "STORAGE_UNAVAILABLE"

    def __init__(self, detail: Optional[str] = None):
        super().__init__(
            "知识库连接失败：数据库不可用。请确认已执行 python -m app.cli import 完成数据导入，并检查存储配置。",
            detail=detail,
        )


class ImportError_(AppError):
    """数据导入失败（引用悬空、文件缺失、格式错误等）。"""

    http_status = 500
    code = "IMPORT_FAILED"

    def __init__(self, message: str, *, detail: Optional[str] = None):
        super().__init__(message, detail=detail)


class NotFoundError(AppError):
    """资源不存在（会话、题目、节点等）。"""

    http_status = 404
    code = "NOT_FOUND"

    def __init__(self, message: str = "请求的资源不存在", *, detail: Optional[str] = None):
        super().__init__(message, detail=detail)


class RateLimitedError(AppError):
    """请求过于频繁（连通性测试这类会打到外部服务的接口）。"""

    http_status = 429
    code = "RATE_LIMITED"

    def __init__(self, message: str = "请求过于频繁，请稍后再试", *, detail: Optional[str] = None):
        super().__init__(message, detail=detail)


class LLMUnavailable(AppError):
    """LLM 调用失败：由服务层捕获并降级为原文摘编，不向用户暴露为错误。"""

    http_status = 502
    code = "LLM_UNAVAILABLE"

    def __init__(self, detail: Optional[str] = None):
        super().__init__("大模型生成服务暂不可用，已切换为知识库原文摘编。", detail=detail)


class InternalError(AppError):
    http_status = 500
    code = "INTERNAL_ERROR"

    def __init__(self, detail: Optional[str] = None):
        super().__init__("服务内部错误，请稍后重试或联系管理员查看日志。", detail=detail)
