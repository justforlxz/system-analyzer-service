"""采集任务在真实 PostgreSQL 上的列表、筛选与取消标记。

单元测试用内存替身，SQL 这段（JSONB 里取 ``payload.target_id``、只对排队中/执行中的行置
取消标记）只能靠真实库验证。运行方式见 ``docs/implementation-baseline.md``：先起库并执行
``alembic upgrade head``，再执行 ``uv run pytest -m integration``。
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from system_analyzer.collection.jobs import COLLECTION_JOB_KIND
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND, JobStatus
from system_analyzer.jobs.queue import JobQueue

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例都从干净的 ``jobs`` 表开始，避免上一条用例残留的任务混进列表。"""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def queue(engine: AsyncEngine) -> JobQueue:
    return JobQueue(engine, retry_delay_seconds=0)


async def enqueue_collection(
    queue: JobQueue,
    engine: AsyncEngine,
    target_id: str | None,
    *,
    status: JobStatus | None = None,
) -> UUID:
    """写一条采集任务；``status`` 有值时直接改状态，模拟 Worker 领走或跑完。"""
    payload = {} if target_id is None else {"target_id": target_id, "run_id": str(uuid4())}
    job_id = await queue.enqueue(COLLECTION_JOB_KIND, payload)
    if status is not None:
        await _set_status(engine, job_id, status)
    return job_id


async def _set_status(engine: AsyncEngine, job_id: UUID, status: JobStatus) -> None:
    """直接改状态列，绕开领取与重试流程——这些用例只关心状态筛选。"""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET status = :status WHERE id = :id"),
            {"status": status.value, "id": job_id},
        )


async def test_list_by_kind_returns_only_collection_tasks(
    queue: JobQueue, engine: AsyncEngine
) -> None:
    await enqueue_collection(queue, engine, "target:1")
    await enqueue_collection(queue, engine, "target:1")
    await enqueue_collection(queue, engine, "target:2")
    await queue.enqueue(SOURCE_IMPORT_JOB_KIND, {"filename": "a.journal.gz"})

    records, total = await queue.list_by_kind(COLLECTION_JOB_KIND)

    assert total == 3
    assert len(records) == 3
    assert {record.kind for record in records} == {COLLECTION_JOB_KIND}
    assert [record.created_at for record in records] == sorted(
        (record.created_at for record in records), reverse=True
    )


async def test_list_by_kind_filters_by_target(queue: JobQueue, engine: AsyncEngine) -> None:
    await enqueue_collection(queue, engine, "target:1")
    await enqueue_collection(queue, engine, "target:2")
    await enqueue_collection(queue, engine, None)

    first, first_total = await queue.list_by_kind(COLLECTION_JOB_KIND, target_id="target:1")
    missing, missing_total = await queue.list_by_kind(COLLECTION_JOB_KIND, target_id="target:9")

    assert first_total == 1
    assert [record.payload["target_id"] for record in first] == ["target:1"]
    assert missing == []
    assert missing_total == 0


async def test_list_by_kind_filters_by_status_and_pages(
    queue: JobQueue, engine: AsyncEngine
) -> None:
    for _ in range(3):
        await enqueue_collection(queue, engine, "target:1")
    finished = await enqueue_collection(queue, engine, "target:1", status=JobStatus.SUCCEEDED)

    queued, queued_total = await queue.list_by_kind(COLLECTION_JOB_KIND, status=JobStatus.QUEUED)
    done, done_total = await queue.list_by_kind(COLLECTION_JOB_KIND, status=JobStatus.SUCCEEDED)
    first_page, page_total = await queue.list_by_kind(COLLECTION_JOB_KIND, limit=2, offset=0)
    second_page, _ = await queue.list_by_kind(COLLECTION_JOB_KIND, limit=2, offset=2)

    assert queued_total == 3
    assert len(queued) == 3
    assert done_total == 1
    assert [record.id for record in done] == [finished]
    assert page_total == 4
    # 两页不重复、合起来正好是全部四条：created_at 相同的任务由 id 兜底排序，翻页因此稳定
    first_ids = {record.id for record in first_page}
    second_ids = {record.id for record in second_page}
    assert len(first_page) == 2
    assert len(second_page) == 2
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {record.id for record in queued} | {finished}


async def test_request_cancel_marks_a_queued_task_without_changing_status(
    queue: JobQueue, engine: AsyncEngine
) -> None:
    job_id = await enqueue_collection(queue, engine, "target:1")

    updated = await queue.request_cancel(job_id, kind=COLLECTION_JOB_KIND)

    assert updated is not None
    assert updated.payload["cancel_requested"] is True
    assert updated.payload["target_id"] == "target:1"
    assert updated.status is JobStatus.QUEUED
    stored = await queue.get(job_id)
    assert stored is not None
    assert stored.payload["cancel_requested"] is True


async def test_request_cancel_marks_a_running_task(queue: JobQueue, engine: AsyncEngine) -> None:
    job_id = await enqueue_collection(queue, engine, "target:1", status=JobStatus.RUNNING)

    updated = await queue.request_cancel(job_id, kind=COLLECTION_JOB_KIND)

    assert updated is not None
    assert updated.status is JobStatus.RUNNING
    assert updated.payload["cancel_requested"] is True


@pytest.mark.parametrize("status", [JobStatus.SUCCEEDED, JobStatus.FAILED])
async def test_request_cancel_ignores_finished_tasks(
    queue: JobQueue, engine: AsyncEngine, status: JobStatus
) -> None:
    """已经结束的任务不置标记：改了也没人读，却会让调用方以为取消成功。"""
    job_id = await enqueue_collection(queue, engine, "target:1", status=status)

    assert await queue.request_cancel(job_id, kind=COLLECTION_JOB_KIND) is None
    stored = await queue.get(job_id)
    assert stored is not None
    assert "cancel_requested" not in stored.payload


async def test_request_cancel_ignores_other_kinds_and_missing_tasks(
    queue: JobQueue, engine: AsyncEngine
) -> None:
    import_id = await queue.enqueue(SOURCE_IMPORT_JOB_KIND, {"filename": "a.journal.gz"})

    assert await queue.request_cancel(import_id, kind=COLLECTION_JOB_KIND) is None
    assert await queue.request_cancel(uuid4(), kind=COLLECTION_JOB_KIND) is None
