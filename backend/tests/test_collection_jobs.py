"""Worker 任务处理器：任务类型注册、记录写回任务行、失败与取消的去向、以及按记录续跑。

不连数据库：处理器需要的队列用只记结果的替身。真实 PostgreSQL 上的行为见
``tests/integration/test_collection_jobs_db.py``。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from collection_fakes import FakeConnector
from system_analyzer.collection.environment import ENVIRONMENT_COMMAND
from system_analyzer.collection.jobs import (
    COLLECTION_JOB_KIND,
    CollectionJobFailed,
    collection_handlers,
    renew_job_lock,
    run_collection_job,
)
from system_analyzer.collection.record import CollectionRecord
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus
from system_analyzer.core.config import Settings
from system_analyzer.jobs.queue import Job
from system_analyzer.remote.errors import SshConnectError
from system_analyzer.remote.probe import SYSTEM_INFO_COMMAND
from system_analyzer.worker import main as worker_main

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"
DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"

PRE = CollectionPhase.PRE
IN_PROGRESS = CollectionPhase.IN_PROGRESS
POST = CollectionPhase.POST


class RecordingResultQueue:
    """只记 ``record_result`` 的队列替身，用来看处理器往任务行里写了什么。"""

    def __init__(self) -> None:
        self.records: list[tuple[UUID, dict[str, Any]]] = []

    async def record_result(self, job_id: UUID, result: dict[str, Any]) -> None:
        self.records.append((job_id, dict(result)))

    @property
    def last_collection(self) -> dict[str, Any]:
        return self.records[-1][1]["collection"]


class BrokenConnector:
    """连不上目标机器：用来制造 Pre 阶段的失败。"""

    @asynccontextmanager
    async def connect(self, target_id: str) -> AsyncIterator[Any]:
        raise SshConnectError(f"建立 SSH 连接失败：{target_id} 不可达", host=target_id, port=22)
        yield  # pragma: no cover


def make_connector() -> FakeConnector:
    return FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})


def make_job(payload: dict[str, Any] | None = None) -> Job:
    return Job(
        id=uuid4(),
        kind=COLLECTION_JOB_KIND,
        payload={"target_id": TARGET, **(payload or {})},
        attempts=1,
    )


def settings() -> Settings:
    return Settings(database_url=DATABASE_URL)


def test_default_handlers_registers_import_and_collection_tasks() -> None:
    """三类任务同时注册：导入、采集与分析都不能被彼此顶掉。"""
    assert set(worker_main.default_handlers()) == {
        "import.source",
        COLLECTION_JOB_KIND,
        "analysis.run",
    }


def test_collection_handlers_registers_the_collection_job_kind(tmp_path: Path) -> None:
    handlers = collection_handlers(settings(), make_connector(), scratch_root=tmp_path)

    assert set(handlers) == {COLLECTION_JOB_KIND}


async def test_job_writes_record_to_task_result(tmp_path: Path) -> None:
    """采集记录写回任务行的 ``result.collection``：每次尝试的三个阶段都带开始与结束时间。"""
    job = make_job({"options": {"backup_paths": [CONFIG_PATH]}})
    queue = RecordingResultQueue()

    outcome = await run_collection_job(
        job, connector=make_connector(), queue=queue, scratch_root=tmp_path
    )

    assert outcome.status is AttemptStatus.SUCCEEDED
    collection = queue.last_collection
    assert collection["run_id"] == str(job.id)
    assert collection["target_id"] == TARGET
    attempt = collection["attempts"][0]
    assert attempt["status"] == "succeeded"
    statuses = {phase["phase"]: phase["status"] for phase in attempt["phases"]}
    assert statuses == {"pre": "succeeded", "in_progress": "succeeded", "post": "succeeded"}
    for phase in attempt["phases"]:
        assert phase["started_at"] is not None
        assert phase["finished_at"] is not None
    kinds = {event["kind"] for event in attempt["events"]}
    assert {"phase_started", "step_started", "step_finished", "phase_finished"} <= kinds


async def test_failed_job_raises_and_records_the_failed_phase(tmp_path: Path) -> None:
    """连不上目标机器：任务失败上抛，记录里 Pre 是 failed、Post 仍然执行过。"""
    job = make_job()
    queue = RecordingResultQueue()

    with pytest.raises(CollectionJobFailed, match="检查目标环境"):
        await run_collection_job(
            job, connector=BrokenConnector(), queue=queue, scratch_root=tmp_path
        )

    attempt = queue.last_collection["attempts"][0]
    statuses = {phase["phase"]: phase for phase in attempt["phases"]}
    assert attempt["status"] == "failed"
    assert statuses["pre"]["status"] == "failed"
    assert "SshConnectError" in statuses["pre"]["detail"]
    assert statuses["post"]["status"] == "succeeded"


async def test_cancelled_job_completes_without_raising(tmp_path: Path) -> None:
    """取消是调用方要求的结果：任务记为成功结束，采集结果写 cancelled。"""
    job = make_job()

    async def cancelled() -> bool:
        return True

    queue = RecordingResultQueue()
    outcome = await run_collection_job(
        job,
        connector=make_connector(),
        queue=queue,
        scratch_root=tmp_path,
        cancelled=cancelled,
    )

    assert outcome.status is AttemptStatus.CANCELLED
    attempt = queue.last_collection["attempts"][0]
    assert attempt["status"] == "cancelled"
    statuses = {phase["phase"]: phase["status"] for phase in attempt["phases"]}
    assert statuses["pre"] == "cancelled"
    assert statuses["post"] == "succeeded"
    failed_events = [event for event in attempt["events"] if event["kind"] == "phase_failed"]
    assert [event["phase"] for event in failed_events] == ["pre"]
    assert failed_events[0]["detail"] is not None


async def test_retry_resumes_from_the_record_in_the_job_payload(tmp_path: Path) -> None:
    """任务行里带着上一次尝试的记录时，Pre 被复用、Post 仍然按备份清单恢复远端。"""
    job = make_job()
    run_id = str(job.id)
    backup_dir = tmp_path / run_id / "backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "0.bak").write_bytes(ORIGINAL_CONFIG)
    job.payload["result"] = {
        "collection": previous_attempt_with_succeeded_pre(
            run_id=run_id, backup_files={CONFIG_PATH: "0.bak"}
        )
    }

    connector = make_connector()
    connector.files[TARGET][CONFIG_PATH] = b"[Seat:*]\n# left over from the crashed run\n"
    queue = RecordingResultQueue()

    outcome = await run_collection_job(job, connector=connector, queue=queue, scratch_root=tmp_path)

    assert outcome.status is AttemptStatus.SUCCEEDED
    assert outcome.attempt.attempt == 2
    assert outcome.attempt.resumed_from == 1
    assert outcome.attempt.phase(PRE).status is PhaseStatus.REUSED
    assert outcome.attempt.phase(POST).status is PhaseStatus.SUCCEEDED
    # 复用的 Pre 没有重新检查环境，也没有重新下载备份；本次尝试只剩 Post 的恢复与清理
    pre_commands = {SYSTEM_INFO_COMMAND, ENVIRONMENT_COMMAND}
    assert not [command for command in connector.commands_of(TARGET) if command in pre_commands]
    assert connector.downloads_of(TARGET) == []
    # 上一次尝试留下改动，由本次的 Post 按备份恢复
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG


async def test_phase_timeout_option_is_applied(tmp_path: Path) -> None:
    """``options.phase_timeouts`` 按阶段覆盖时间上限：上限为 0 时 In Progress 立刻超时。"""
    job = make_job({"options": {"phase_timeouts": {"in_progress": 0}}})
    queue = RecordingResultQueue()

    with pytest.raises(CollectionJobFailed, match="时间上限"):
        await run_collection_job(
            job, connector=make_connector(), queue=queue, scratch_root=tmp_path
        )

    attempt = queue.last_collection["attempts"][0]
    statuses = {phase["phase"]: phase["status"] for phase in attempt["phases"]}
    assert statuses["in_progress"] == "timed_out"
    assert statuses["post"] == "succeeded"


async def test_scratch_dir_is_removed_after_a_successful_run(tmp_path: Path) -> None:
    """成功或取消后再不会重跑，临时目录随之删掉；失败时保留给下一次尝试恢复用。"""
    job = make_job()
    await run_collection_job(
        job, connector=make_connector(), queue=RecordingResultQueue(), scratch_root=tmp_path
    )
    assert not (tmp_path / str(job.id)).exists()

    failed_job = make_job()
    with pytest.raises(CollectionJobFailed):
        await run_collection_job(
            failed_job,
            connector=BrokenConnector(),
            queue=RecordingResultQueue(),
            scratch_root=tmp_path,
        )
    assert (tmp_path / str(failed_job.id)).is_dir()


def previous_attempt_with_succeeded_pre(
    *, run_id: str, backup_files: dict[str, str]
) -> dict[str, Any]:
    """构造一条「Pre 成功、In Progress 失败、Post 成功」的上一次尝试记录。"""
    record = CollectionRecord(run_id=run_id, target_id=TARGET)
    attempt = record.start_attempt()
    attempt.move(AttemptStatus.RUNNING)
    attempt.started_at = "2026-11-12T06:00:00+00:00"
    for phase, statuses in (
        (PRE, (PhaseStatus.RUNNING, PhaseStatus.SUCCEEDED)),
        (IN_PROGRESS, (PhaseStatus.RUNNING, PhaseStatus.FAILED)),
        (POST, (PhaseStatus.RUNNING, PhaseStatus.SUCCEEDED)),
    ):
        for status in statuses:
            attempt.phase(phase).move(status)
    attempt.phase(IN_PROGRESS).detail = "步骤「启动采样」失败：RuntimeError: 采样起不来"
    attempt.move(AttemptStatus.FAILED)
    attempt.finished_at = "2026-11-12T06:00:12+00:00"
    attempt.data["backup"] = {"paths": list(backup_files), "files": dict(backup_files)}
    return record.as_mapping()


class _NoopConnection:
    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class FlakyLockEngine:
    """续租用到的引擎替身：前 ``failures`` 次 ``begin()`` 直接失败。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_NoopConnection]:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("数据库不可用")
        yield _NoopConnection()


async def test_lock_renewal_keeps_going_after_failed_updates() -> None:
    """续租语句失败不让续租循环死掉，也不把异常抛给调用方。"""
    engine = FlakyLockEngine(failures=2)

    async with renew_job_lock(engine, uuid4(), interval=0.01):
        await asyncio.sleep(0.12)

    assert engine.calls > engine.failures + 2, f"两次失败后仍然继续续租，实际 {engine.calls} 次"


async def test_lock_renewal_is_skipped_for_non_positive_interval() -> None:
    """间隔为 0 时不启动续租任务（集成测试用这个开关关掉续租）。"""
    engine = FlakyLockEngine(failures=0)

    async with renew_job_lock(engine, uuid4(), interval=0):
        await asyncio.sleep(0.05)

    assert engine.calls == 0


async def test_run_id_must_be_a_uuid(tmp_path: Path) -> None:
    """``run_id`` 会拼进本地临时目录名，非 UUID 一律拒绝，不能逃出临时目录根。"""
    job = make_job({"run_id": "../escape"})

    with pytest.raises(ValueError, match="必须是 UUID"):
        await run_collection_job(
            job, connector=make_connector(), queue=RecordingResultQueue(), scratch_root=tmp_path
        )

    assert not (tmp_path.parent / "escape").exists()


async def test_run_id_from_payload_is_used_for_the_record(tmp_path: Path) -> None:
    """调用方给了 UUID 形式的 ``run_id`` 时，记录与临时目录都用它。"""
    run_id = str(uuid4())
    job = make_job({"run_id": run_id})
    queue = RecordingResultQueue()

    outcome = await run_collection_job(
        job, connector=make_connector(), queue=queue, scratch_root=tmp_path
    )

    assert outcome.record.run_id == run_id
    assert queue.last_collection["run_id"] == run_id
