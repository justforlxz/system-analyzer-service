"""验收：Journal 归档导入 → 分析任务 → 结论入库 → 诊断读回，以及 Journal-only 时间线。

覆盖 issue 验收标准中的三项：

- Journal-only 闭环能在统一时间轴上回放；
- Systemd 生命周期与 Invocation ID 关联正确；
- 内核 OOM 与 systemd-oomd 诊断正确，结论能追溯到原始 Journal 记录。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from system_analyzer.analyzers.oom import (
    ANNOTATION_TYPE_KERNEL,
    ANNOTATION_TYPE_SYSTEMD_OOMD,
)
from system_analyzer.analyzers.oom import (
    PLUGIN_ID as OOM_PLUGIN,
)
from system_analyzer.analyzers.systemd_lifecycle import PLUGIN_ID as LIFECYCLE_PLUGIN
from system_analyzer.core.config import Settings
from system_analyzer.diagnostics import ServiceDiagnosticsEngine
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore
from system_analyzer.timeline import TimelineEngine, TimelineEvent
from system_analyzer.worker.analysis_jobs import analysis_handlers
from system_analyzer.worker.import_jobs import import_handlers
from system_analyzer.worker.runner import Worker

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

FIXTURES = Path(__file__).parent / "fixtures"
JOURNAL_GZ = FIXTURES / "journal_archive" / "boot.journal.gz"
OOM_EXPORT = FIXTURES / "oom" / "journal.export"
LIFECYCLE_EXPORT = FIXTURES / "systemd_lifecycle" / "boot.export"

OOM_MACHINE = "machine:sas-oom"
OOM_BOOT = "9f2c4b7d-1e6a-4c8f-a1b2-c3d4e5f60718"
LIFECYCLE_MACHINE = "machine:uos-PC"
LIFECYCLE_BOOT = "7f2c5d4e-9a1b-4c3d-8e2f-1a2b3c4d5e6f"
JOURNAL_MACHINE = "machine:uos-PC"
JOURNAL_BOOT = "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
NGINX_INVOCATION = "2f9c6a0d1b3e4f5a8c7d9e0f1a2b3c4d"

TABLES = ("records", "entities", "collections", "boots", "machines", "jobs")


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url)
    async with engine.begin() as connection:
        for table in TABLES:
            await connection.execute(text(f"DELETE FROM {table}"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def import_journal(
    engine: AsyncEngine, database_url: str, path: Path, *, plugin_id: str = "journal.archive"
) -> tuple[DiagnosticStore, str, UUID]:
    """导入一份 Journal 归档并执行自动排上的分析任务，返回存储、采集号与分析任务号。"""
    settings = Settings(database_url=database_url)
    registry = register_builtins(PluginRegistry())
    queue = JobQueue(engine)
    worker = Worker(
        queue,
        {**import_handlers(settings, registry), **analysis_handlers(settings, registry)},
        worker_id="acceptance-journal-worker",
        poll_interval_seconds=0,
    )
    job_id = await queue.enqueue(
        SOURCE_IMPORT_JOB_KIND,
        {
            "plugin_id": plugin_id,
            "upload_id": path.stem,
            "upload_path": str(path),
            "filename": path.name,
        },
    )
    assert await worker.run_once() is True
    record = await queue.get(job_id)
    assert record is not None and record.status.value == "succeeded", record.last_error
    result = record.payload["result"]
    analysis_id = UUID(result["analysis_job_id"])
    assert await worker.run_once() is True
    analysis = await queue.get(analysis_id)
    assert analysis is not None and analysis.status.value == "succeeded", analysis.last_error
    return DiagnosticStore(engine), result["collection_id"], analysis_id


async def test_journal_only_archive_replays_without_resource_tracks(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store, _, _ = await import_journal(engine, test_database_url, JOURNAL_GZ)
    tracks = await TimelineEngine(store).tracks(JOURNAL_MACHINE, JOURNAL_BOOT)

    assert tracks.resources.available is False
    assert tracks.resources.reasons
    assert [track for track in tracks.tracks if track.kind is RecordKind.METRIC] == []
    assert any(isinstance(item, TimelineEvent) for item in tracks.items)
    positions = [item.monotonic_ns for item in tracks.items if item.monotonic_ns is not None]
    assert positions == sorted(positions)


async def test_oom_analysis_reaches_the_diagnostics_view(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store, collection_id, _ = await import_journal(engine, test_database_url, OOM_EXPORT)
    records = await store.list_records_in_boot(OOM_MACHINE, OOM_BOOT)
    events = {record.record_id: record for record in records if record.kind is RecordKind.EVENT}
    annotations = [
        record
        for record in records
        if record.kind is RecordKind.ANNOTATION and record.source_plugin == OOM_PLUGIN
    ]
    assert {record.payload["annotation_type"] for record in annotations} == {
        ANNOTATION_TYPE_KERNEL,
        ANNOTATION_TYPE_SYSTEMD_OOMD,
    }
    for annotation in annotations:
        assert set(annotation.payload["evidence_refs"]) <= set(events)
        assert annotation.collection_id == collection_id

    firefox = await ServiceDiagnosticsEngine(store).service_at(
        OOM_MACHINE, OOM_BOOT, "service:firefox.service", at_ns=240_600_000_000
    )
    tracker = await ServiceDiagnosticsEngine(store).service_at(
        OOM_MACHINE, OOM_BOOT, "service:tracker-extract-3.service", at_ns=240_600_000_000
    )
    kernel_items = [
        item for item in firefox.annotations if item.annotation_type == ANNOTATION_TYPE_KERNEL
    ]
    oomd_items = [
        item for item in tracker.annotations if item.annotation_type == ANNOTATION_TYPE_SYSTEMD_OOMD
    ]
    assert kernel_items and oomd_items
    for item in (*kernel_items, *oomd_items):
        assert item.evidence and not item.evidence_missing
        assert all(evidence.record_id in events for evidence in item.evidence)


async def test_systemd_lifecycle_spans_keep_the_invocation_id(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store, _, _ = await import_journal(engine, test_database_url, LIFECYCLE_EXPORT)
    records = await store.list_records_in_boot(LIFECYCLE_MACHINE, LIFECYCLE_BOOT)
    spans = [
        record
        for record in records
        if record.kind is RecordKind.SPAN and record.source_plugin == LIFECYCLE_PLUGIN
    ]
    assert spans
    nginx = [
        record for record in spans if record.payload["attributes"].get("unit") == "nginx.service"
    ]
    assert nginx
    assert {record.payload["attributes"]["invocation_id"] for record in nginx} == {NGINX_INVOCATION}

    annotations = [
        record
        for record in records
        if record.kind is RecordKind.ANNOTATION and record.source_plugin == LIFECYCLE_PLUGIN
    ]
    assert annotations
    event_ids = {record.record_id for record in records if record.kind is RecordKind.EVENT}
    for annotation in annotations:
        assert annotation.payload["evidence_refs"]
        assert set(annotation.payload["evidence_refs"]) <= event_ids

    oom_annotations = [
        record
        for record in records
        if record.kind is RecordKind.ANNOTATION and record.source_plugin == OOM_PLUGIN
    ]
    assert oom_annotations == []
