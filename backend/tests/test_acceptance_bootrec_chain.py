"""验收：固定 ``.bootrec`` 样例经真实 Worker 导入后能按统一时间线回放。

这一组用例对应用户可见的验收条件：

- 四份仓库样例都能通过 Worker 的导入路径写进 PostgreSQL；
- 时间线条目的顺序与实体关联符合 0.2 / 0.3：按 ``(monotonic_ns, collection_id, record_id)``
  升序，条目引用的实体、采集都在库里；
- 资源缺失的三种表达（可用 / 不可用 / 无数据）都能从仓库材料复现；
- Journal-only 样例没有资源轨道，整次启动声明 ``resources.available = false``。

运行方式：``scripts/acceptance.sh`` 或
``cd backend && uv run pytest -m acceptance``
（需要测试库，见 ``docs/implementation-baseline.md``）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sample_bootrec import SAMPLE_DIR, load_sample
from system_analyzer.core.config import Settings
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore
from system_analyzer.timeline import TimelineEngine, TrackStatus
from system_analyzer.worker.analysis_jobs import analysis_handlers
from system_analyzer.worker.import_jobs import import_handlers
from system_analyzer.worker.runner import Worker

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

TABLES = ("records", "entities", "collections", "boots", "machines", "jobs")
MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
SAMPLE_NAMES = (
    "full.bootrec",
    "journal-only.bootrec",
    "resources-missing.bootrec",
    "no-data.bootrec",
)


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


async def import_through_worker(
    engine: AsyncEngine, database_url: str, path: Path
) -> tuple[DiagnosticStore, str, UUID]:
    """按 Worker 的真实接法导入一份样例，并把随后排上的分析任务也执行完。"""
    settings = Settings(database_url=database_url)
    registry = register_builtins(PluginRegistry())
    queue = JobQueue(engine)
    worker = Worker(
        queue,
        {**import_handlers(settings, registry), **analysis_handlers(settings, registry)},
        worker_id="acceptance-worker",
        poll_interval_seconds=0,
    )
    job_id = await queue.enqueue(
        SOURCE_IMPORT_JOB_KIND,
        {
            "plugin_id": "bootrec.importer",
            "upload_id": path.stem,
            "upload_path": str(path),
            "filename": path.name,
        },
    )
    assert await worker.run_once() is True
    record = await queue.get(job_id)
    assert record is not None and record.status.value == "succeeded", record.last_error

    result = record.payload["result"]
    collection_id = result["collection_id"]
    assert result["records_written"] > 0

    analysis_id = UUID(result["analysis_job_id"])
    assert await worker.run_once() is True
    analysis = await queue.get(analysis_id)
    assert analysis is not None and analysis.status.value == "succeeded", analysis.last_error
    return DiagnosticStore(engine), collection_id, analysis_id


async def test_every_repository_sample_imports_and_replays(
    engine: AsyncEngine, test_database_url: str
) -> None:
    """四份样例都能导入，时间线按时间升序返回且实体引用都能落到库里。"""
    store = DiagnosticStore(engine)
    for name in SAMPLE_NAMES:
        async with engine.begin() as connection:
            for table in TABLES:
                await connection.execute(text(f"DELETE FROM {table}"))

        sample = load_sample(name)
        store, collection_id, _ = await import_through_worker(
            engine, test_database_url, SAMPLE_DIR / name
        )
        state = await store.get_collection(collection_id)
        assert state is not None and state.import_status.value == "imported"

        stored = await store.list_records_in_collection(collection_id)
        stored_ids = {record.record_id for record in stored}
        assert {record.record_id for record in sample.records} <= stored_ids

        tracks = await TimelineEngine(store).tracks(
            sample.collection.machine_id, sample.collection.boot_id
        )
        assert tracks.axis_start_ns is not None and tracks.axis_end_ns is not None
        assert tracks.axis_start_ns <= tracks.axis_end_ns
        positions = [item.monotonic_ns for item in tracks.items if item.monotonic_ns is not None]
        assert positions == sorted(positions)

        entities = await store.list_entities(
            sample.collection.machine_id, sample.collection.boot_id
        )
        entity_ids = {entity.entity_id for entity in entities if hasattr(entity, "entity_id")}
        for item in tracks.items:
            entity = item.entity if hasattr(item, "entity") else None
            if isinstance(entity, str) and entity.startswith(("service:", "metric:")):
                assert entity in entity_ids, f"{name} 的时间线条目引用了不存在的实体 {entity}"


async def test_samples_distinguish_available_unavailable_and_no_data(
    engine: AsyncEngine, test_database_url: str
) -> None:
    """三种资源状态都能用仓库材料复现，缺失不写成 0。"""
    store, _, _ = await import_through_worker(
        engine, test_database_url, SAMPLE_DIR / "resources-missing.bootrec"
    )
    tracks = await TimelineEngine(store).tracks(MACHINE_ID, BOOT_ID)
    by_entity = {track.entity: track for track in tracks.tracks if track.kind is RecordKind.METRIC}
    assert by_entity["metric:cpu.total.utilization"].status is TrackStatus.AVAILABLE
    for entity in ("metric:memory.used", "metric:disk.read_bytes_per_second"):
        track = by_entity[entity]
        assert track.status is TrackStatus.UNAVAILABLE
        assert track.unavailable_reason
        assert track.items == ()

    async with engine.begin() as connection:
        for table in TABLES:
            await connection.execute(text(f"DELETE FROM {table}"))

    store, _, _ = await import_through_worker(
        engine, test_database_url, SAMPLE_DIR / "no-data.bootrec"
    )
    tracks = await TimelineEngine(store).tracks(MACHINE_ID, BOOT_ID)
    by_entity = {track.entity: track for track in tracks.tracks if track.kind is RecordKind.METRIC}
    load_track = by_entity["metric:load.1min"]
    assert load_track.status is TrackStatus.NO_DATA
    assert load_track.unavailable_reason is None
    assert load_track.items == ()
    assert by_entity["metric:cpu.total.utilization"].status is TrackStatus.AVAILABLE


async def test_journal_only_sample_has_no_resource_tracks(
    engine: AsyncEngine, test_database_url: str
) -> None:
    """Journal-only：没有 metric_stream 实体，整次启动声明资源不可用并写明原因。"""
    store, _, _ = await import_through_worker(
        engine, test_database_url, SAMPLE_DIR / "journal-only.bootrec"
    )
    tracks = await TimelineEngine(store).tracks(MACHINE_ID, BOOT_ID)
    assert tracks.resources.available is False
    assert tracks.resources.reasons
    assert [track for track in tracks.tracks if track.kind is RecordKind.METRIC] == []
    assert any(track.kind is RecordKind.EVENT for track in tracks.tracks)
