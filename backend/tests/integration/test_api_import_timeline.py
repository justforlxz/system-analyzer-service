"""导入与时间线接口的集成测试：真实 PostgreSQL 上的「上传 → 导入 → 查询」三步。

与 ``tests/test_api_imports.py`` 的分工：单元测试证明接口形状与错误结构；这里证明真的写进库——
Worker 经 ``PluginRegistry.invoke`` 调插件的 ``import_source``，插件经同步数据入口把整份采集
写进 PostgreSQL，再从 HTTP 接口把同一条启动的时间线读回来。

运行方式见 ``docs/implementation-baseline.md``：先 ``scripts/dev-db.sh start`` 与
``alembic upgrade head``，再 ``uv run pytest -m integration``。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sample_bootrec import load_sample
from system_analyzer.api.app import create_app
from system_analyzer.core.config import get_settings
from system_analyzer.domain.bootrec import MetricStreamEntity, RecordKind
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.storage import DiagnosticStore, ImportStatus
from system_analyzer.worker.analysis_jobs import analysis_handlers
from system_analyzer.worker.import_jobs import import_handlers
from system_analyzer.worker.runner import Worker

pytestmark = pytest.mark.integration

JOURNAL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "journal_archive" / "boot.journal.gz"
JOURNAL_MACHINE = "machine:uos-PC"
JOURNAL_BOOT = "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
JOURNAL_RECORDS = 4

SAMPLE_MACHINE = "machine:uos-PC"
SAMPLE_BOOT = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
TABLES = ("records", "entities", "collections", "boots", "machines", "jobs")


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例从空表开始。"""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def app(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, tmp_path: Path
) -> AsyncIterator[FastAPI]:
    monkeypatch.setenv("SAS_DATABASE_URL", test_database_url)
    monkeypatch.setenv("SAS_UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    application = create_app()
    try:
        yield application
    finally:
        # 接口层自己建的引擎挂在 app.state 上，用例结束时释放
        api_engine = getattr(application.state, "engine", None)
        if api_engine is not None:
            await api_engine.dispose()
        get_settings.cache_clear()


def client(app) -> httpx.AsyncClient:
    """测试客户端：读响应，不把应用抛出的异常再抛出来。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


async def run_one_job(engine: AsyncEngine, app, job_id: str) -> bool:
    """跑到指定导入任务结束；期间的自动分析任务也由同一个 Worker 处理。"""
    settings = get_settings()
    queue = JobQueue(engine)
    worker = Worker(
        queue,
        {
            **import_handlers(settings, app.state.plugins),
            **analysis_handlers(settings, app.state.plugins),
        },
        worker_id="integration-worker",
        poll_interval_seconds=0,
    )
    for _ in range(10):
        record = await queue.get(UUID(job_id))
        if record is not None and record.status.value in {"succeeded", "failed"}:
            return True
        if not await worker.run_once():
            return False
    return False


async def test_upload_import_and_query_timeline(engine: AsyncEngine, app) -> None:
    payload = await asyncio.to_thread(JOURNAL_FIXTURE.read_bytes)
    async with client(app) as http:
        # 第 1 步：上传
        uploaded = await http.post(
            "/api/imports",
            files={"file": ("boot.journal.gz", payload, "application/gzip")},
        )
        assert uploaded.status_code == 202, uploaded.text
        submitted = uploaded.json()
        assert submitted["plugin_id"] == "journal.archive"

        # 第 2 步：导入（Worker 领取任务并在后台执行）
        assert await run_one_job(engine, app, submitted["job_id"]) is True

        status = await http.get(f"/api/imports/{submitted['job_id']}")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["job"]["status"] == "succeeded"
        assert body["job"]["result"]["records_written"] == JOURNAL_RECORDS
        collection = body["collection"]
        assert collection["import_status"] == "imported"
        assert collection["machine_id"] == JOURNAL_MACHINE
        assert collection["boot_id"] == JOURNAL_BOOT
        collection_id = collection["collection_id"]

        # 第 3 步：查询时间线
        tracks = await http.get(
            f"/api/machines/{JOURNAL_MACHINE}/boots/{JOURNAL_BOOT}/timeline/tracks"
        )
        assert tracks.status_code == 200, tracks.text
        timeline = tracks.json()
        assert len(timeline["items"]) == JOURNAL_RECORDS
        assert {item["type"] for item in timeline["items"]} == {"event"}
        assert timeline["resources"]["available"] is False
        assert timeline["tracks"], "轨道仍然要列出来，不能因为整体没有资源数据就返回空"
        assert timeline["axis_start_ns"] is not None

        state = await http.get(
            f"/api/machines/{JOURNAL_MACHINE}/boots/{JOURNAL_BOOT}/timeline/state",
            params={"monotonic_ns": timeline["items"][0]["monotonic_ns"]},
        )
        assert state.status_code == 200, state.text
        assert state.json()["tracks"]

        cursor = await http.get(
            f"/api/machines/{JOURNAL_MACHINE}/boots/{JOURNAL_BOOT}/timeline/cursor"
        )
        assert cursor.status_code == 200, cursor.text
        assert cursor.json()["position_ns"] == timeline["axis_start_ns"]

        records = await http.get(f"/api/collections/{collection_id}/records")
        assert records.status_code == 200, records.text
        assert records.json()["collection_total"] == JOURNAL_RECORDS

    # 真的写进库了：不是只返回一个对象
    store = DiagnosticStore(engine)
    stored = await store.get_collection(collection_id)
    assert stored is not None
    assert stored.import_status is ImportStatus.IMPORTED
    assert await store.count_records(collection_id) == JOURNAL_RECORDS
    assert await store.get_machine(JOURNAL_MACHINE) is not None
    assert await store.get_boot(JOURNAL_MACHINE, JOURNAL_BOOT) is not None


async def test_uploading_the_same_archive_twice_is_idempotent(engine: AsyncEngine, app) -> None:
    payload = await asyncio.to_thread(JOURNAL_FIXTURE.read_bytes)
    async with client(app) as http:
        first = await http.post(
            "/api/imports", files={"file": ("boot.journal.gz", payload, "application/gzip")}
        )
        assert first.status_code == 202
        assert await run_one_job(engine, app, first.json()["job_id"]) is True
        first_status = (await http.get(f"/api/imports/{first.json()['job_id']}")).json()
        collection_id = first_status["collection"]["collection_id"]

        second = await http.post(
            "/api/imports", files={"file": ("boot.journal.gz", payload, "application/gzip")}
        )
        assert second.status_code == 202
        assert await run_one_job(engine, app, second.json()["job_id"]) is True
        second_status = (await http.get(f"/api/imports/{second.json()['job_id']}")).json()

    assert second_status["job"]["status"] == "succeeded"
    assert second_status["job"]["result"]["records_written"] == 0
    assert second_status["job"]["result"]["skipped_reason"]
    assert second_status["collection"]["collection_id"] == collection_id
    # 分析任务会在同一个采集里追加区间与结论记录；这里只核对导入的 Journal 事件没有被重复写入。
    store = DiagnosticStore(engine)
    events = await store.list_records_in_collection(collection_id, kinds=[RecordKind.EVENT])
    assert len(events) == JOURNAL_RECORDS
    assert await store.count_records(collection_id) > JOURNAL_RECORDS


async def test_timeline_distinguishes_unavailable_from_no_data(engine: AsyncEngine, app) -> None:
    """一条「声明不可用」的流与一条「声明可用但没采样」的流，在响应里必须区分开。"""
    sample = load_sample("full.bootrec")
    entities = []
    for entity in sample.entities:
        if isinstance(entity, MetricStreamEntity) and entity.entity_id == "metric:memory.used":
            entity = MetricStreamEntity(
                entity_type="metric_stream",
                entity_id=entity.entity_id,
                machine_id=entity.machine_id,
                boot_id=entity.boot_id,
                name=entity.name,
                unit=entity.unit,
                available=False,
                unavailable_reason="/proc/meminfo 在采集窗口内读取失败，本流没有数据",
            )
        entities.append(entity)
    # 只留事件、区间与 cpu 的采样点：memory 变成「声明不可用」，disk 变成「声明可用但没有采样」
    record_lines = [
        line
        for line in sample.record_lines
        if line["kind"] in ("event", "span") or line.get("entity") == "metric:cpu.total.utilization"
    ]
    store = DiagnosticStore(engine)
    outcome = await store.import_collection(
        sample.collection,
        entities,
        record_lines,
        source_sha256=None,
        resources=sample.resources,
    )
    assert outcome.records_written == len(record_lines)

    async with client(app) as http:
        response = await http.get(
            f"/api/machines/{SAMPLE_MACHINE}/boots/{SAMPLE_BOOT}/timeline/tracks"
        )

    assert response.status_code == 200, response.text
    tracks = {
        track["entity"]: track for track in response.json()["tracks"] if track["kind"] == "metric"
    }

    cpu = tracks["metric:cpu.total.utilization"]
    assert cpu["status"] == "available"
    assert cpu["items"], "有采样点的轨道必须带着数据返回"
    assert cpu["unavailable_reason"] is None

    memory = tracks["metric:memory.used"]
    assert memory["status"] == "unavailable"
    assert memory["unavailable_reason"] == "/proc/meminfo 在采集窗口内读取失败，本流没有数据"
    assert memory["items"] == []

    disk = tracks["metric:disk.read_bytes_per_second"]
    assert disk["status"] == "no_data"
    assert disk["unavailable_reason"] is None
    assert disk["items"] == []
