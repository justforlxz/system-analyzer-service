"""采集任务在真实 PostgreSQL 上的集成测试。

单元测试用替身队列验证处理器写了什么；这里验证「写进 ``jobs`` 表之后还读得回来」：
任务状态、``payload.result.collection`` 里的阶段记录、取消标记、重试次数与任务锁续租。

运行方式见 ``docs/implementation-baseline.md``：先起库并 ``alembic upgrade head``，
再执行 ``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from collection_fakes import FakeConnector, RecordingSshClient
from system_analyzer.collection.jobs import (
    COLLECTION_JOB_KIND,
    collection_handlers,
    job_cancel_requested,
)
from system_analyzer.collection.record import CollectionRecord
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus
from system_analyzer.core.config import Settings
from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.worker.runner import Worker

pytestmark = pytest.mark.integration

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例从空的 jobs 表开始。"""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM jobs"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def queue(engine: AsyncEngine) -> JobQueue:
    """重试延后为 0，失败的用例可以立刻再领一次。"""
    return JobQueue(engine, retry_delay_seconds=0)


def make_connector(config: bytes = ORIGINAL_CONFIG) -> FakeConnector:
    return FakeConnector({TARGET: {CONFIG_PATH: config}})


def make_handlers(test_database_url: str, connector, tmp_path: Path, **kwargs):
    return collection_handlers(
        Settings(database_url=test_database_url),
        connector,
        scratch_root=tmp_path,
        lock_renew_interval=kwargs.pop("lock_renew_interval", 0),
        **kwargs,
    )


async def collection_record(engine: AsyncEngine, job_id: UUID) -> dict[str, Any]:
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT payload FROM jobs WHERE id = :id"), {"id": job_id})
        ).one()
    return row.payload["result"]["collection"]


async def job_status(engine: AsyncEngine, job_id: UUID) -> tuple[str, int, str | None]:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT status, attempts, last_error FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
    return row.status, row.attempts, row.last_error


class BrokenConnector:
    """连不上目标机器。"""

    @asynccontextmanager
    async def connect(self, target_id: str):
        raise ConnectionError(f"{target_id} 不可达")
        yield  # pragma: no cover


class SlowConnector:
    """连接前先等一会儿，用来观察任务锁在执行期间有没有被续租。"""

    def __init__(self, inner: FakeConnector, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    @asynccontextmanager
    async def connect(self, target_id: str) -> AsyncIterator[RecordingSshClient]:
        await asyncio.sleep(self._delay)
        async with self._inner.connect(target_id) as client:
            yield client


async def test_job_runs_and_records_each_phase(
    engine: AsyncEngine, queue: JobQueue, test_database_url: str, tmp_path: Path
) -> None:
    """一次成功的采集：任务成功，三个阶段的状态、起止时间与操作记录都能从任务行里查回来。"""
    connector = make_connector()
    handlers = make_handlers(test_database_url, connector, tmp_path)
    job_id = await queue.enqueue(
        COLLECTION_JOB_KIND, {"target_id": TARGET, "options": {"backup_paths": [CONFIG_PATH]}}
    )

    worker = Worker(queue, handlers, worker_id="worker-integration")
    assert await worker.run_once() is True

    status, attempts, last_error = await job_status(engine, job_id)
    assert (status, attempts, last_error) == (JobStatus.SUCCEEDED.value, 1, None)

    collection = await collection_record(engine, job_id)
    assert collection["run_id"] == str(job_id)
    assert collection["target_id"] == TARGET
    assert len(collection["attempts"]) == 1
    attempt = collection["attempts"][0]
    assert attempt["status"] == AttemptStatus.SUCCEEDED.value
    phases = {phase["phase"]: phase for phase in attempt["phases"]}
    for name in (CollectionPhase.PRE, CollectionPhase.IN_PROGRESS, CollectionPhase.POST):
        assert phases[name.value]["status"] == PhaseStatus.SUCCEEDED.value
        assert phases[name.value]["started_at"] is not None
        assert phases[name.value]["finished_at"] is not None
    steps = [event for event in attempt["events"] if event["kind"] == "step_finished"]
    assert "检查目标环境" in [event["step"] for event in steps]
    assert "恢复环境" in [event["step"] for event in steps]
    assert attempt["data"]["restored"] == [CONFIG_PATH]
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG


async def test_job_cancel_requested_reads_payload_flag(
    engine: AsyncEngine, queue: JobQueue
) -> None:
    """取消标记写在任务行的 payload 上，处理器按行读它。"""
    job_id = await queue.enqueue(COLLECTION_JOB_KIND, {"target_id": TARGET})

    assert await job_cancel_requested(engine, job_id) is False

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET payload = jsonb_set(payload, '{cancel_requested}', 'true'::jsonb)"
                " WHERE id = :id"
            ),
            {"id": job_id},
        )

    assert await job_cancel_requested(engine, job_id) is True


async def test_cancel_flag_stops_the_run_and_still_cleans_up(
    engine: AsyncEngine, queue: JobQueue, test_database_url: str, tmp_path: Path
) -> None:
    """任务行上的取消标记生效：采集记为 cancelled，任务正常结束，清理阶段仍然执行。"""
    connector = make_connector(b"[Seat:*]\n# changed by a previous run\n")
    handlers = make_handlers(test_database_url, connector, tmp_path)
    job_id = await queue.enqueue(COLLECTION_JOB_KIND, {"target_id": TARGET})
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET payload = jsonb_set(payload, '{cancel_requested}', 'true'::jsonb)"
                " WHERE id = :id"
            ),
            {"id": job_id},
        )

    worker = Worker(queue, handlers, worker_id="worker-integration")
    assert await worker.run_once() is True

    status, attempts, last_error = await job_status(engine, job_id)
    assert (status, attempts, last_error) == (JobStatus.SUCCEEDED.value, 1, None)

    attempt = (await collection_record(engine, job_id))["attempts"][0]
    assert attempt["status"] == AttemptStatus.CANCELLED.value
    phases = {phase["phase"]: phase["status"] for phase in attempt["phases"]}
    assert phases[CollectionPhase.PRE.value] == PhaseStatus.CANCELLED.value
    assert phases[CollectionPhase.POST.value] == PhaseStatus.SUCCEEDED.value
    # 取消在第一个步骤之前生效，目标机器上什么都没执行
    assert connector.commands_of(TARGET) == []


async def test_failed_attempt_is_recorded_and_retried(
    engine: AsyncEngine, queue: JobQueue, test_database_url: str, tmp_path: Path
) -> None:
    """第一次尝试连不上目标机器：任务回到队列，记录里留下失败的阶段；第二次接着跑。"""
    job_id = await queue.enqueue(
        COLLECTION_JOB_KIND,
        {"target_id": TARGET, "options": {"backup_paths": [CONFIG_PATH]}},
        max_attempts=2,
    )
    failing_worker = Worker(
        queue,
        make_handlers(test_database_url, BrokenConnector(), tmp_path),
        worker_id="worker-integration",
    )

    assert await failing_worker.run_once() is True

    status, attempts, last_error = await job_status(engine, job_id)
    assert status == JobStatus.QUEUED.value, "还有重试次数时任务回到队列"
    assert attempts == 1
    assert last_error is not None and "检查目标环境" in last_error

    collection = await collection_record(engine, job_id)
    first = collection["attempts"][-1]
    assert first["status"] == AttemptStatus.FAILED.value
    phases = {phase["phase"]: phase for phase in first["phases"]}
    assert phases[CollectionPhase.PRE.value]["status"] == PhaseStatus.FAILED.value
    assert phases[CollectionPhase.POST.value]["status"] == PhaseStatus.SUCCEEDED.value

    connector = make_connector()
    retrying_worker = Worker(
        queue,
        make_handlers(test_database_url, connector, tmp_path),
        worker_id="worker-integration",
    )
    assert await retrying_worker.run_once() is True

    status, attempts, last_error = await job_status(engine, job_id)
    assert (status, attempts) == (JobStatus.SUCCEEDED.value, 2)
    # jobs.complete 不清 last_error，上一次尝试的失败原因仍留在任务行上（既有队列行为）
    assert last_error is not None and "检查目标环境" in last_error

    collection = await collection_record(engine, job_id)
    assert len(collection["attempts"]) == 2
    second = collection["attempts"][-1]
    assert second["attempt"] == 2
    assert second["resumed_from"] == 1
    assert second["status"] == AttemptStatus.SUCCEEDED.value
    # 第一次尝试的 Pre 没成功，这次重新执行
    assert {phase["phase"]: phase["status"] for phase in second["phases"]}[
        CollectionPhase.PRE.value
    ] == PhaseStatus.SUCCEEDED.value


async def test_retry_reuses_succeeded_phase_from_stored_record(
    engine: AsyncEngine, queue: JobQueue, test_database_url: str, tmp_path: Path
) -> None:
    """任务行里存的记录说 Pre 已经成功时，重试直接复用，不再连去做环境检查。"""
    job_id = await queue.enqueue(
        COLLECTION_JOB_KIND,
        {"target_id": TARGET, "options": {"backup_paths": [CONFIG_PATH]}},
        max_attempts=2,
    )
    backup_dir = tmp_path / str(job_id) / "backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "0.bak").write_bytes(ORIGINAL_CONFIG)
    stored = collection_with_succeeded_pre(str(job_id))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET payload = jsonb_set(payload, '{result}', CAST(:result AS jsonb))"
                " WHERE id = :id"
            ),
            {"id": job_id, "result": _json({"collection": stored})},
        )

    connector = make_connector(b"[Seat:*]\n# left over from the crashed run\n")
    worker = Worker(
        queue,
        make_handlers(test_database_url, connector, tmp_path),
        worker_id="worker-integration",
    )
    assert await worker.run_once() is True

    collection = await collection_record(engine, job_id)
    second = collection["attempts"][-1]
    assert second["attempt"] == 2
    phases = {phase["phase"]: phase["status"] for phase in second["phases"]}
    assert phases[CollectionPhase.PRE.value] == PhaseStatus.REUSED.value
    assert phases[CollectionPhase.POST.value] == PhaseStatus.SUCCEEDED.value
    assert connector.commands_of(TARGET) == [], "复用的阶段不再执行环境检查"
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG


async def test_lock_is_renewed_while_job_runs(
    engine: AsyncEngine, queue: JobQueue, test_database_url: str, tmp_path: Path
) -> None:
    """执行期间任务锁被续租，长任务不会被别的 Worker 当成失锁任务再领一遍。"""
    connector = SlowConnector(make_connector(), delay=0.3)
    handlers = make_handlers(test_database_url, connector, tmp_path, lock_renew_interval=0.05)
    job_id = await queue.enqueue(COLLECTION_JOB_KIND, {"target_id": TARGET})
    claimed = await queue.claim("worker-integration")
    assert claimed is not None and claimed.id == job_id
    before = await locked_at(engine, job_id)

    await handlers[COLLECTION_JOB_KIND](claimed)

    after = await locked_at(engine, job_id)
    assert before is not None and after is not None
    assert (after - before).total_seconds() > 0.1, "执行 0.3 秒、每 0.05 秒续租一次，锁时间必须前移"


async def locked_at(engine: AsyncEngine, job_id: UUID):
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT locked_at FROM jobs WHERE id = :id"), {"id": job_id})
        ).one()
    return row.locked_at


def collection_with_succeeded_pre(run_id: str) -> dict[str, Any]:
    """一条「Pre 成功、In Progress 失败、Post 成功」的上一次尝试记录。"""
    record = CollectionRecord(run_id=run_id, target_id=TARGET)
    attempt = record.start_attempt()
    attempt.move(AttemptStatus.RUNNING)
    for phase, statuses in (
        (CollectionPhase.PRE, (PhaseStatus.RUNNING, PhaseStatus.SUCCEEDED)),
        (CollectionPhase.IN_PROGRESS, (PhaseStatus.RUNNING, PhaseStatus.FAILED)),
        (CollectionPhase.POST, (PhaseStatus.RUNNING, PhaseStatus.SUCCEEDED)),
    ):
        for status in statuses:
            attempt.phase(phase).move(status)
    attempt.move(AttemptStatus.FAILED)
    attempt.data["backup"] = {"paths": [CONFIG_PATH], "files": {CONFIG_PATH: "0.bak"}}
    return record.as_mapping()


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)
