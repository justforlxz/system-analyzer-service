"""分析任务的 Worker 处理器。

任务由导入任务成功之后自动入队（``worker/import_jobs.py``）。处理器按 payload 里的
``machine_id`` 与 ``boot_id`` 构造同步数据入口，把 2.1 生命周期分析与 2.2 OOM 分析经
``PluginRegistry.invoke`` 执行；分析器自己把结论写回数据入口，这里只记录写回条数。

数据入口复用导入任务的同步桥接（``api/sync_store.SyncStoreAccess``）：插件入口是同步方法，
存储层是协程，两者之间的每次调用都在独立事件循环里执行，连接不跨事件循环复用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from system_analyzer.analyzers.oom import OomAnalyzer
from system_analyzer.analyzers.systemd_lifecycle_analyzer import (
    MACHINE_ID_OPTION,
    SystemdLifecycleAnalyzer,
)
from system_analyzer.api.sync_store import SyncStoreAccess
from system_analyzer.core.config import Settings, get_settings
from system_analyzer.core.db import create_engine
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.jobs.models import ANALYSIS_JOB_KIND
from system_analyzer.jobs.queue import Job, JobQueue, JobResultStore
from system_analyzer.plugins.contracts import AnalysisRequest, PluginInputError
from system_analyzer.plugins.registry import PluginRegistry, get_registry
from system_analyzer.worker.runner import JobHandler

logger = logging.getLogger(__name__)

INTERNAL_FAILURE_MESSAGE = "分析任务执行失败，具体原因见 Worker 日志"

DEFAULT_ANALYZERS: tuple[str, ...] = (
    SystemdLifecycleAnalyzer.spec.id,
    OomAnalyzer.spec.id,
)
"""没有指定分析器时按顺序执行的内置分析器：生命周期结论先写，OOM 结论后写。"""


class AnalysisJobPayload(BaseModel):
    """导入完成后写入任务行的 payload。"""

    model_config = ConfigDict(extra="ignore")

    machine_id: str = Field(min_length=1)
    boot_id: str = Field(min_length=1)
    collection_id: str | None = None
    analyzers: list[str] | None = None


class SyncAnalysisAccess(SyncStoreAccess):
    """分析插件要求的同步数据入口，作用域固定在一次启动。

    ``PluginDataAccess`` 与 ``LifecycleDataAccess`` 的 ``append_records`` 签名不同：一个批量写入
    （记录自带 ``collection_id``），一个按采集写入。这里按第一个参数的类型分派，插件看到的仍是
    各自契约里的形状。
    """

    def __init__(self, database_url: str, machine_id: str, boot_id: str) -> None:
        super().__init__(database_url)
        self.machine_id = machine_id
        self.boot_id = boot_id

    def list_records_in_boot(
        self,
        machine_id: str,
        boot_id: str,
        *,
        kinds: Sequence[RecordKind] | None = None,
        limit: int | None = None,
    ) -> Sequence[Any]:
        return self._run(
            lambda store: store.list_records_in_boot(machine_id, boot_id, kinds=kinds, limit=limit)
        )

    def ensure_entities(self, collection_id: str, entities: Sequence[Any]) -> int:
        return self._run(lambda store: store.ensure_entities(collection_id, entities))

    def query_records(
        self, *, record_kinds: Sequence[str] = (), limit: int | None = None
    ) -> Sequence[Any]:
        kinds = [RecordKind(kind) for kind in record_kinds] or None
        return self._run(
            lambda store: store.list_records_in_boot(
                self.machine_id, self.boot_id, kinds=kinds, limit=limit
            )
        )

    def append_records(
        self,
        collection_or_records: str | Sequence[Mapping[str, Any]],
        records: Sequence[Mapping[str, Any]] | None = None,
        *,
        plugin_id: str | None = None,
        source: str | None = None,
    ) -> int:
        if isinstance(collection_or_records, str):
            if records is None:
                raise PluginInputError("append_records 缺少要写入的记录")
            outcome = self._run(lambda store: store.append_records(collection_or_records, records))
            return outcome.records_written
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for record in collection_or_records:
            grouped.setdefault(str(record["collection_id"]), []).append(record)
        written = 0
        for collection_id, batch in grouped.items():
            outcome = self._run(
                lambda store, cid=collection_id, items=batch: store.append_records(cid, items)
            )
            written += outcome.records_written
        return written


async def run_analysis_job(
    job: Job,
    *,
    registry: PluginRegistry,
    queue: JobResultStore,
    access_factory: Callable[[], SyncAnalysisAccess],
) -> None:
    """执行一个分析任务，并把每个分析器写回的条数记到任务行。"""
    try:
        payload = AnalysisJobPayload.model_validate(job.payload)
        access = access_factory()
        written = await asyncio.to_thread(invoke_analyzers, registry, payload, access, str(job.id))
    except PluginInputError as exc:
        await queue.record_result(job.id, _failure_patch("invalid_input", str(exc)))
        raise
    except Exception:
        logger.exception("分析任务 %s 执行失败", job.id)
        await queue.record_result(job.id, _failure_patch("internal", INTERNAL_FAILURE_MESSAGE))
        raise
    await queue.record_result(job.id, {"analyzers": written})


def invoke_analyzers(
    registry: PluginRegistry,
    payload: AnalysisJobPayload,
    access: SyncAnalysisAccess,
    run_id: str,
) -> dict[str, int]:
    """按顺序经 ``PluginRegistry.invoke`` 执行分析器，返回每个分析器写回的结论条数。"""
    written: dict[str, int] = {}
    for plugin_id in payload.analyzers or DEFAULT_ANALYZERS:
        request = AnalysisRequest(
            boot_id=payload.boot_id,
            options={MACHINE_ID_OPTION: payload.machine_id},
        )
        result = registry.invoke(
            plugin_id,
            lambda plugin, context, request=request: plugin.analyze(request, context),
            data=access,
            run_id=run_id,
        )
        written[plugin_id] = result.annotations_written
    return written


def _failure_patch(kind: str, message: str) -> dict[str, Any]:
    return {"failure": {"kind": kind, "message": message}}


def analysis_handlers(
    settings: Settings | None = None,
    registry: PluginRegistry | None = None,
) -> dict[str, JobHandler]:
    """分析任务的处理器清单；每个任务自己开一条引擎与队列，用完就关。"""
    resolved_settings = settings or get_settings()
    resolved_registry = registry or get_registry()

    async def handle_analysis(job: Job) -> None:
        engine = create_engine(resolved_settings.database_url)
        try:
            queue = JobQueue(
                engine, lock_timeout_seconds=resolved_settings.worker_lock_timeout_seconds
            )
            payload = AnalysisJobPayload.model_validate(job.payload)
            await run_analysis_job(
                job,
                registry=resolved_registry,
                queue=queue,
                access_factory=lambda: SyncAnalysisAccess(
                    resolved_settings.database_url,
                    payload.machine_id,
                    payload.boot_id,
                ),
            )
        finally:
            await engine.dispose()

    return {ANALYSIS_JOB_KIND: handle_analysis}
