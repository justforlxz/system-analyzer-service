"""对外 API 的统一错误结构。

所有失败响应都是同一个信封，调用方不用按接口分别解析：

```json
{"error": {"type": "invalid_parameter", "message": "...", "details": ["..."]}}
```

``type`` 只有四个取值，区分「调用方给的参数不对」「上传的内容格式不对」「要找的东西
不存在」与「服务端内部出错」：

| type | HTTP | 什么时候出现 |
| --- | --- | --- |
| ``invalid_parameter`` | 422 | 查询参数、路径参数、分页与时间范围不合法 |
| ``invalid_format`` | 400 | 上传的文件没有导入插件认得，或插件判定输入格式不对 |
| ``not_found`` | 404 | 机器、启动、采集、任务或接口路径不存在 |
| ``internal_error`` | 500 | 服务端内部错误，包含插件抛出的未处理异常 |

插件契约里的错误也在这里转换成统一结构，不让插件自己的异常类型漏到 HTTP 层：

- ``PluginInputError`` → ``invalid_format``（输入不对，不是服务端缺陷）
- ``PluginExecutionError``（插件抛出的其它异常）→ ``internal_error``，
  只给固定文案加请求 id，原始异常进日志
- ``PluginNotRegisteredError`` → ``invalid_parameter``（调用方点了不存在的插件）
- ``StorageValidationError`` → ``invalid_format``（送进来的数据不符合数据模型）
- ``StorageStateError`` / 其它 ``StorageError`` → ``internal_error``（状态迁移不该由合法请求触发）
- ``TimelineRangeError`` / ``TimelineKindError`` → ``invalid_parameter``
- ``TimelineBootNotFoundError`` → ``not_found``
- ``TimelineScopeError`` → ``internal_error``（读到别的启动的记录，是实现缺陷）
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from system_analyzer.plugins.contracts import (
    PluginError,
    PluginExecutionError,
    PluginInputError,
)
from system_analyzer.plugins.registry import PluginNotRegisteredError
from system_analyzer.storage import StorageError, StorageValidationError
from system_analyzer.timeline import (
    TimelineBootNotFoundError,
    TimelineError,
    TimelineKindError,
    TimelineRangeError,
    TimelineScopeError,
)

logger = logging.getLogger(__name__)


class ErrorType(StrEnum):
    """统一错误响应里的 ``type`` 取值。"""

    INVALID_PARAMETER = "invalid_parameter"
    INVALID_FORMAT = "invalid_format"
    NOT_FOUND = "not_found"
    INTERNAL = "internal_error"


_STATUS_BY_TYPE: dict[ErrorType, int] = {
    ErrorType.INVALID_PARAMETER: 422,
    ErrorType.INVALID_FORMAT: 400,
    ErrorType.NOT_FOUND: 404,
    ErrorType.INTERNAL: 500,
}


class ApiError(Exception):
    """一次可以直接回给调用方的失败。"""

    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        *,
        details: Sequence[str] = (),
        status_code: int | None = None,
    ) -> None:
        self.error_type = error_type
        self.message = message
        self.details = list(details)
        self.status_code = status_code or _STATUS_BY_TYPE[error_type]
        super().__init__(message)

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={
                "error": {
                    "type": self.error_type.value,
                    "message": self.message,
                    "details": self.details,
                }
            },
        )


def invalid_parameter(message: str, *, details: Sequence[str] = ()) -> ApiError:
    return ApiError(ErrorType.INVALID_PARAMETER, message, details=details)


def invalid_format(message: str, *, details: Sequence[str] = ()) -> ApiError:
    return ApiError(ErrorType.INVALID_FORMAT, message, details=details)


def payload_too_large(message: str) -> ApiError:
    """上传内容超过大小上限：422 之外单独用 413，调用方据此知道该缩文件而不是改参数。"""
    return ApiError(ErrorType.INVALID_FORMAT, message, status_code=413)


def not_found(message: str) -> ApiError:
    return ApiError(ErrorType.NOT_FOUND, message)


def internal_error(message: str) -> ApiError:
    return ApiError(ErrorType.INTERNAL, message)


INTERNAL_MESSAGE = "服务端内部错误，请把请求 id {reference} 提供给运维，异常详情只记在服务端日志里"
"""内部错误对外的唯一文案：不含异常类型与原文，只给一个能在日志里检索的请求 id。"""


def unhandled_internal_error(
    exc: BaseException, *, where: str, request_id: str | None = None
) -> ApiError:
    """处理未预期异常：原文只进日志，响应里只留固定文案加请求 id。

    异常原文可能带文件路径、SQL 片段或库内部细节，不能回给调用方（本任务的验收标准要求
    「API 不暴露插件内部实现细节」）。
    """
    reference = request_id or uuid4().hex
    logger.error("请求 id %s 在 %s 处出现未处理异常", reference, where, exc_info=exc)
    return internal_error(INTERNAL_MESSAGE.format(reference=reference))


def domain_error(exc: BaseException, *, request_id: str | None = None) -> ApiError:
    """把契约层抛出的异常翻译成统一错误；不认识的异常按内部错误处理。

    ``PluginInputError`` 与 ``StorageValidationError`` 的文本是「这份输入哪里不对」的说明，
    按契约就是给调用方看的，原样透出。其余内部错误只给固定文案加请求 id，原文进日志。
    """
    if isinstance(exc, PluginNotRegisteredError):
        return invalid_parameter(str(exc))
    if isinstance(exc, PluginInputError):
        return invalid_format(str(exc))
    if isinstance(exc, TimelineBootNotFoundError):
        return not_found(str(exc))
    if isinstance(exc, TimelineRangeError):
        return invalid_parameter(str(exc))
    if isinstance(exc, TimelineKindError):
        return invalid_parameter(str(exc))
    if isinstance(exc, StorageValidationError):
        return invalid_format(str(exc), details=exc.problems)
    if isinstance(exc, PluginExecutionError):
        return unhandled_internal_error(exc, where="调用插件", request_id=request_id)
    if isinstance(exc, PluginError):
        return unhandled_internal_error(exc, where="调用插件", request_id=request_id)
    if isinstance(exc, TimelineScopeError):
        return unhandled_internal_error(exc, where="时间线查询", request_id=request_id)
    if isinstance(exc, TimelineError):
        return unhandled_internal_error(exc, where="时间线查询", request_id=request_id)
    if isinstance(exc, StorageError):
        return unhandled_internal_error(exc, where="访问存储", request_id=request_id)
    return unhandled_internal_error(exc, where="处理请求", request_id=request_id)


_HANDLED_EXCEPTIONS = (
    PluginError,
    PluginNotRegisteredError,
    StorageError,
    TimelineError,
)


def install_error_handlers(app: FastAPI) -> None:
    """把全部失败路径收敛成同一个信封。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [_describe_validation_error(item) for item in exc.errors()]
        return invalid_parameter("请求参数不合法", details=details).response()

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            error = not_found("接口不存在")
        elif exc.status_code < 500:
            error = ApiError(
                ErrorType.INVALID_PARAMETER,
                str(exc.detail),
                status_code=exc.status_code,
            )
        else:
            error = unhandled_internal_error(exc, where="HTTP 处理")
        return error.response()

    for exception_type in _HANDLED_EXCEPTIONS:

        async def _handle_domain(
            request: Request, exc: Exception, _type: type = exception_type
        ) -> JSONResponse:
            return domain_error(exc).response()

        app.add_exception_handler(exception_type, _handle_domain)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        where = f"{request.method} {request.url.path}"
        return unhandled_internal_error(exc, where=where).response()


def _describe_validation_error(item: dict) -> str:
    """把 FastAPI 的一条校验错误写成 ``位置: 原因`` 这样一行可读文本。"""
    location = ".".join(str(part) for part in item.get("loc", ())) or "请求"
    message = item.get("msg", "不合法")
    return f"{location}: {message}"
