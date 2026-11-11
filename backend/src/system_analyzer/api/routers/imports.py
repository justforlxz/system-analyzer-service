"""导入接口：上传文件、提交导入任务、查询任务状态。

上传与导入分两次进行：

1. ``POST /api/imports`` 只把文件写到上传目录、算出摘要、挑一个导入插件、
   把任务写进队列，然后立刻返回；
2. Worker 领取任务后在后台解析并写库（见 ``worker/import_jobs.py``）；
3. ``GET /api/imports/{job_id}`` 把 ``jobs`` 表里的任务状态与它写回的采集状态一起返回。

请求线程里不做解析：一份归档解析后可能占几百 MiB，放进请求里既拖长响应又挡住事件循环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile

from system_analyzer.api.deps import app_settings, get_job_queue, get_plugins, get_store
from system_analyzer.api.errors import not_found, payload_too_large
from system_analyzer.api.schemas import (
    ImportJobFailure,
    ImportJobResult,
    ImportJobStatus,
    ImportStatusResponse,
    ImportSubmitResponse,
    collection_response,
)
from system_analyzer.api.sync_store import SyncStoreAccess
from system_analyzer.api.uploads import (
    UploadTooLargeError,
    discard_upload,
    resolve_importer,
    save_upload,
)
from system_analyzer.core.config import Settings
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.plugins.contracts import ImportSource
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore

router = APIRouter(prefix="/imports", tags=["imports"])

_RESULT_FIELDS = tuple(ImportJobResult.model_fields)


@router.post(
    "",
    status_code=202,
    response_model=ImportSubmitResponse,
    summary="上传文件并提交导入任务",
)
async def submit_import(
    file: UploadFile = File(..., description="待导入的文件：.bootrec 容器或 Journal 归档"),
    plugin_id: str | None = Form(
        None,
        description="导入插件 id；不传则按文件内容自动识别（bootrec.importer / journal.archive）",
    ),
    settings: Settings = Depends(app_settings),
    plugins: PluginRegistry = Depends(get_plugins),
    queue: JobQueue = Depends(get_job_queue),
) -> ImportSubmitResponse:
    try:
        saved = await save_upload(
            file, Path(settings.upload_dir), max_bytes=settings.max_upload_bytes
        )
    except UploadTooLargeError as exc:
        raise payload_too_large(
            f"上传文件超过上限 {exc.limit} 字节；请拆分归档或调高 SAS_MAX_UPLOAD_BYTES"
        ) from exc
    try:
        source = ImportSource(path=saved.path, size_bytes=saved.size_bytes)
        access = SyncStoreAccess(settings.database_url)
        # 插件的 detect 只读文件头，但仍然放到线程里跑：它经由 registry.invoke 调用，
        # 不该在事件循环线程里执行。
        resolved = await asyncio.to_thread(
            resolve_importer,
            plugins,
            source,
            plugin_id=plugin_id,
            data=access,
            display_name=saved.filename or "上传文件",
        )
        job_id = await queue.enqueue(
            SOURCE_IMPORT_JOB_KIND,
            {
                "plugin_id": resolved,
                "upload_id": saved.upload_id,
                "upload_path": str(saved.path),
                "filename": saved.filename,
                "source_sha256": saved.source_sha256,
                "size_bytes": saved.size_bytes,
            },
        )
    except BaseException:
        # 格式不被接受、插件崩溃或入队失败：删掉本次写入的文件，被拒的上传不留垃圾文件
        await asyncio.to_thread(discard_upload, saved.path)
        raise
    return ImportSubmitResponse(
        job_id=str(job_id),
        upload_id=saved.upload_id,
        plugin_id=resolved,
        filename=saved.filename,
        source_sha256=saved.source_sha256,
        size_bytes=saved.size_bytes,
    )


@router.get(
    "/{job_id}",
    response_model=ImportStatusResponse,
    summary="导入任务状态与它写回的采集",
)
async def read_import_status(
    job_id: UUID,
    queue: JobQueue = Depends(get_job_queue),
    store: DiagnosticStore = Depends(get_store),
) -> ImportStatusResponse:
    record = await queue.get(job_id)
    if record is None:
        raise not_found(f"任务 {job_id} 不存在")
    payload: dict[str, Any] = record.payload or {}
    raw_result = payload.get("result")
    result = _job_result(raw_result)
    failure = _job_failure(raw_result)
    collection = None
    if result is not None and result.collection_id is not None:
        state = await store.get_collection(result.collection_id)
        collection = None if state is None else collection_response(state)
    return ImportStatusResponse(
        job=ImportJobStatus(
            id=str(record.id),
            kind=record.kind,
            status=record.status,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            failure=failure,
            plugin_id=_optional_str(payload.get("plugin_id")),
            upload_id=_optional_str(payload.get("upload_id")),
            filename=_optional_str(payload.get("filename")),
            result=result,
        ),
        collection=collection,
    )


def _job_result(raw: Any) -> ImportJobResult | None:
    """只取约定字段：任务结果的原始字典里可能有执行方内部用的键，不整份透出去。"""
    if not isinstance(raw, dict):
        return None
    return ImportJobResult(**{name: raw.get(name) for name in _RESULT_FIELDS})


def _job_failure(raw: Any) -> ImportJobFailure | None:
    """任务失败的分类说明。

    只读处理器写回的 ``result.failure``。任务行的 ``last_error`` 带异常类型与原文（可能是文件
    路径或 SQL 片段），不对外暴露。
    """
    if not isinstance(raw, dict):
        return None
    failure = raw.get("failure")
    if not isinstance(failure, dict):
        return None
    try:
        return ImportJobFailure.model_validate(
            {"kind": failure.get("kind"), "message": failure.get("message")}
        )
    except ValueError:
        # 处理器写回的形状不对时不给调用方看半成品，任务状态本身仍然可查
        return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
