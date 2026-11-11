"""导入任务的 Worker 处理器。

任务来自 ``POST /api/imports``：payload 里是「用哪个导入插件」与「上传文件落在哪」。处理器做三件事：

1. 把 ``PluginRegistry.invoke`` 放到线程里跑（``asyncio.to_thread``）：插件入口是同步方法，
   线程里每次存储层调用都开一条短命事件循环，见 ``api/sync_store.py``；
2. 插件经由 ``context.data``（``SyncStoreAccess``）真的把整份采集写进库；
3. 把「写进了哪次采集、写了多少条」写回任务行，供 ``GET /api/imports/{job_id}`` 查询。

失败时异常原样上抛：``Worker`` 会调 ``queue.fail`` 记下原因并按重试策略决定是否重跑。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from system_analyzer.api.sync_store import SyncCollectionAccess, SyncStoreAccess
from system_analyzer.core.config import Settings, get_settings
from system_analyzer.core.db import create_engine
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND
from system_analyzer.jobs.queue import Job, JobQueue, JobResultStore
from system_analyzer.plugins.contracts import ImportResult, ImportSource, PluginInputError
from system_analyzer.plugins.registry import PluginRegistry, get_registry
from system_analyzer.worker.runner import JobHandler

logger = logging.getLogger(__name__)

INTERNAL_FAILURE_MESSAGE = "导入任务执行失败，具体原因见 Worker 日志"
"""内部失败给调用方的固定说明：异常类型、堆栈与文件路径只写进日志。"""


class ImportJobPayload(BaseModel):
    """``POST /api/imports`` 写进任务行的那段 payload。"""

    model_config = ConfigDict(extra="ignore")

    plugin_id: str = Field(min_length=1)
    upload_id: str = Field(min_length=1)
    upload_path: str = Field(min_length=1)
    filename: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


async def run_import_job(
    job: Job,
    *,
    registry: PluginRegistry,
    queue: JobResultStore,
    access_factory: Callable[[], SyncCollectionAccess],
) -> None:
    """执行一个导入任务，并把结果或失败分类写回任务行。

    插件调用放进线程：插件入口是同步的，而它里面的存储层调用要开自己的事件循环，
    不能占用当前事件循环。线程结束时结果已经可见，不需要额外的同步手段。

    失败分类写在 ``payload.result.failure``：``invalid_input`` 带上插件给出的、面向调用方的
    拒绝原因；``internal`` 只给固定文案，异常原文只进日志（状态查询不暴露内部细节）。
    """
    try:
        payload = ImportJobPayload.model_validate(job.payload)
        access = access_factory()
        result = await asyncio.to_thread(invoke_import, registry, payload, access, str(job.id))
    except PluginInputError as exc:
        await queue.record_result(job.id, _failure_patch("invalid_input", str(exc)))
        raise
    except Exception:
        logger.exception("导入任务 %s 执行失败", job.id)
        await queue.record_result(job.id, _failure_patch("internal", INTERNAL_FAILURE_MESSAGE))
        raise
    await queue.record_result(job.id, _result_patch(result, access))


def invoke_import(
    registry: PluginRegistry,
    payload: ImportJobPayload,
    access: SyncCollectionAccess,
    run_id: str,
) -> ImportResult:
    """经 ``PluginRegistry.invoke`` 调插件的 ``import_source``（同步，在 Worker 线程里执行）。"""
    path = Path(payload.upload_path)
    if not path.is_file():
        raise PluginInputError(f"上传文件 {payload.upload_path} 不见了，无法导入")
    source = ImportSource(path=path, size_bytes=payload.size_bytes)
    return registry.invoke(
        payload.plugin_id,
        lambda plugin, context: plugin.import_source(source, context),
        data=access,
        run_id=run_id,
    )


def _failure_patch(kind: str, message: str) -> dict[str, Any]:
    """任务失败时写回的分类与说明。"""
    return {"failure": {"kind": kind, "message": message}}


def _result_patch(result: ImportResult, access: SyncCollectionAccess) -> dict[str, Any]:
    """任务结果：插件只报写入条数，采集号从数据入口留下的那次写入里取。"""
    patch: dict[str, Any] = {"records_written": result.records_written}
    outcome = access.last_outcome
    if outcome is not None:
        patch.update(
            {
                "collection_id": outcome.collection_id,
                "import_status": outcome.status.value,
                "records_skipped": outcome.records_skipped,
                "skipped_reason": outcome.skipped_reason,
            }
        )
    return patch


def import_handlers(
    settings: Settings | None = None,
    registry: PluginRegistry | None = None,
) -> dict[str, JobHandler]:
    """导入任务的处理器清单。

    每个任务自己开一条引擎与一个队列，用完就关：``default_handlers()`` 拿不到 ``run_worker``
    里的那个队列，而一次导入任务里多开一条连接的开销可以忽略。
    """
    resolved_settings = settings or get_settings()
    resolved_registry = registry or get_registry()

    async def handle_import(job: Job) -> None:
        engine = create_engine(resolved_settings.database_url)
        try:
            queue = JobQueue(
                engine, lock_timeout_seconds=resolved_settings.worker_lock_timeout_seconds
            )
            await run_import_job(
                job,
                registry=resolved_registry,
                queue=queue,
                access_factory=lambda: SyncStoreAccess(resolved_settings.database_url),
            )
        finally:
            await engine.dispose()

    return {SOURCE_IMPORT_JOB_KIND: handle_import}
