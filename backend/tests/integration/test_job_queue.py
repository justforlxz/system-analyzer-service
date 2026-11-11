"""任务队列的集成测试，需要真实 PostgreSQL。

运行方式见 ``docs/implementation-baseline.md``：先起库并执行 ``alembic upgrade head``，
再执行 ``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``
（环境变量或 ``backend/.env`` 都可以），未配置时这个命令直接报错而不是静默跳过。
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import JobQueue

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例使用干净的 jobs 表，避免上一条用例残留的任务被误领取。"""
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


async def job_status(engine: AsyncEngine, job_id: UUID) -> tuple[str, int, str | None, int]:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT status, attempts, last_error, max_attempts FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
    return row.status, row.attempts, row.last_error, row.max_attempts


async def test_enqueue_and_claim(queue: JobQueue) -> None:
    kind = f"test-{uuid4()}"

    job_id = await queue.enqueue(kind, {"hello": "world"})
    claimed = await queue.claim("worker-integration")

    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.kind == kind
    assert claimed.payload == {"hello": "world"}
    assert claimed.attempts == 1

    await queue.complete(job_id)


async def test_claim_returns_none_when_queue_is_empty(queue: JobQueue) -> None:
    assert await queue.claim("worker-integration") is None


async def test_enqueue_writes_max_attempts(queue: JobQueue, engine: AsyncEngine) -> None:
    job_id = await queue.enqueue(f"test-{uuid4()}", max_attempts=5)

    _, _, _, max_attempts = await job_status(engine, job_id)

    assert max_attempts == 5


async def test_failure_requeues_until_max_attempts(queue: JobQueue, engine: AsyncEngine) -> None:
    job_id = await queue.enqueue(f"test-{uuid4()}", max_attempts=2)

    await queue.claim("worker-integration")
    assert await queue.fail(job_id, "第一次失败") is JobStatus.QUEUED

    await queue.claim("worker-integration")
    assert await queue.fail(job_id, "第二次失败") is JobStatus.FAILED

    status, attempts, last_error, _ = await job_status(engine, job_id)
    assert status == JobStatus.FAILED.value
    assert attempts == 2
    assert last_error == "第二次失败"
