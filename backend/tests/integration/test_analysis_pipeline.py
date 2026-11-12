"""导入 → 分析 → 入库 → 诊断读回的完整集成测试（真实 PostgreSQL）。

走的是 Worker 的真实接法：导入任务成功后自动入队 ``analysis.run``，分析处理器经
``PluginRegistry.invoke`` 执行 2.1 / 2.2，结论写回同一个采集，再由 4.3 的诊断读侧取出。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from system_analyzer.core.config import Settings
from system_analyzer.diagnostics import ServiceDiagnosticsEngine
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.jobs.models import ANALYSIS_JOB_KIND, SOURCE_IMPORT_JOB_KIND
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore
from system_analyzer.worker.analysis_jobs import analysis_handlers
from system_analyzer.worker.import_jobs import import_handlers
from system_analyzer.worker.runner import Worker

pytestmark = pytest.mark.integration

OOM_FIXTURE = Path(__file__).parent.parent / "fixtures" / "oom" / "journal.export"
LIFECYCLE_FIXTURE = Path(__file__).parent.parent / "fixtures" / "systemd_lifecycle" / "boot.export"
TABLES = ("records", "entities", "collections", "boots", "machines", "jobs")


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def run_import_then_analysis(
    engine: AsyncEngine, database_url: str, fixture: Path
) -> tuple[JobQueue, UUID, UUID]:
    settings = Settings(database_url=database_url)
    registry = register_builtins(PluginRegistry())
    queue = JobQueue(engine)
    worker = Worker(
        queue,
        {
            **import_handlers(settings, registry),
            **analysis_handlers(settings, registry),
        },
        worker_id="analysis-pipeline-worker",
        poll_interval_seconds=0,
    )
    import_job = await queue.enqueue(
        SOURCE_IMPORT_JOB_KIND,
        {
            "plugin_id": "journal.archive",
            "upload_id": "upload-analysis",
            "upload_path": str(fixture),
            "filename": fixture.name,
        },
    )

    assert await worker.run_once() is True
    import_record = await queue.get(import_job)
    assert import_record is not None
    analysis_id = import_record.payload["result"]["analysis_job_id"]
    assert isinstance(analysis_id, str)

    assert await worker.run_once() is True
    return queue, import_job, UUID(analysis_id)


async def test_import_enqueues_analysis_that_writes_conclusions_and_feeds_diagnostics(
    engine: AsyncEngine, test_database_url: str
) -> None:
    queue, import_job, analysis_job = await run_import_then_analysis(
        engine, test_database_url, OOM_FIXTURE
    )

    imported = await queue.get(import_job)
    assert imported is not None and imported.status.value == "succeeded"
    analyzed = await queue.get(analysis_job)
    assert analyzed is not None
    assert analyzed.kind == ANALYSIS_JOB_KIND
    assert analyzed.status.value == "succeeded", analyzed.last_error
    assert analyzed.payload["result"]["analyzers"]["oom.analyzer"] == 2

    store = DiagnosticStore(engine)
    records = await store.list_records_in_boot(MACHINE, BOOT)
    kinds = {record.kind for record in records}
    assert RecordKind.SPAN in kinds
    assert RecordKind.ANNOTATION in kinds
    annotations = [record for record in records if record.kind is RecordKind.ANNOTATION]
    oom_types = {
        record.payload["annotation_type"]
        for record in annotations
        if record.payload.get("analyzer") == "oom.analyzer"
    }
    assert oom_types == {
        "oom.kernel.killed_process",
        "oom.systemd_oomd.killed_cgroup",
    }

    result = await ServiceDiagnosticsEngine(store).service_at(
        MACHINE, BOOT, "service:firefox.service", at_ns=240_600_000_000
    )
    oom = next(
        item for item in result.annotations if item.annotation_type == "oom.kernel.killed_process"
    )
    assert oom.confidence == 0.3
    assert oom.evidence_missing is False
    assert [item.found for item in oom.evidence] == [True, True]
    assert all(item.message for item in oom.evidence)
    assert result.state.state == "failed"
    assert result.identity["pid"].value == "1"


async def test_normal_lifecycle_builds_service_list_and_cursor_state(
    engine: AsyncEngine, test_database_url: str
) -> None:
    await run_import_then_analysis(engine, test_database_url, LIFECYCLE_FIXTURE)

    store = DiagnosticStore(engine)
    services = await ServiceDiagnosticsEngine(store).services(NORMAL_MACHINE, NORMAL_BOOT)
    assert "service:nginx.service" in {item.entity for item in services.services}

    events = await store.list_records_in_boot(NORMAL_MACHINE, NORMAL_BOOT, kinds=[RecordKind.EVENT])
    moment = min(record.monotonic_ns or 0 for record in events)
    result = await ServiceDiagnosticsEngine(store).service_at(
        NORMAL_MACHINE, NORMAL_BOOT, "service:nginx.service", at_ns=moment
    )
    assert result.lifecycle
    assert result.journal.items
    assert result.journal.items[0].message


MACHINE = "machine:sas-oom"
BOOT = "9f2c4b7d-1e6a-4c8f-a1b2-c3d4e5f60718"
NORMAL_MACHINE = "machine:uos-PC"
NORMAL_BOOT = "7f2c5d4e-9a1b-4c3d-8e2f-1a2b3c4d5e6f"
