"""Worker 主循环测试：用假队列验证成功、失败、未知类型与队列抖动时的处理。"""

import asyncio
from uuid import UUID, uuid4

import pytest

from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import Job
from system_analyzer.worker import main as worker_main
from system_analyzer.worker.runner import Worker


class FakeQueue:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = list(jobs)
        self.completed: list[UUID] = []
        self.failed: list[tuple[UUID, str]] = []

    async def claim(self, worker_id: str) -> Job | None:
        return self._jobs.pop(0) if self._jobs else None

    async def complete(self, job_id: UUID) -> None:
        self.completed.append(job_id)

    async def fail(self, job_id: UUID, error: str) -> JobStatus:
        self.failed.append((job_id, error))
        return JobStatus.FAILED


def make_job(kind: str = "demo") -> Job:
    return Job(id=uuid4(), kind=kind, payload={}, attempts=1)


async def test_successful_handler_marks_complete() -> None:
    job = make_job()
    queue = FakeQueue([job])
    handled: list[UUID] = []

    async def handler(received: Job) -> None:
        handled.append(received.id)

    worker = Worker(queue, {"demo": handler}, worker_id="worker-test")

    assert await worker.run_once() is True
    assert handled == [job.id]
    assert queue.completed == [job.id]
    assert queue.failed == []


async def test_failing_handler_marks_failed_with_error() -> None:
    job = make_job()
    queue = FakeQueue([job])

    async def handler(received: Job) -> None:
        raise RuntimeError("采集失败")

    worker = Worker(queue, {"demo": handler}, worker_id="worker-test")

    assert await worker.run_once() is True
    assert queue.completed == []
    assert queue.failed == [(job.id, "RuntimeError: 采集失败")]


async def test_unknown_kind_is_rejected() -> None:
    job = make_job(kind="unknown")
    queue = FakeQueue([job])
    worker = Worker(queue, {}, worker_id="worker-test")

    assert await worker.run_once() is True
    assert queue.failed == [(job.id, "未注册的任务类型: unknown")]


async def test_empty_queue_returns_false() -> None:
    worker = Worker(FakeQueue([]), {}, worker_id="worker-test")

    assert await worker.run_once() is False


async def test_run_forever_survives_a_queue_error() -> None:
    """数据库短暂不可用时主循环只重试，不退出。"""

    class FlakyQueue(FakeQueue):
        def __init__(self) -> None:
            super().__init__([])
            self.claims = 0
            self.failures = 0

        async def claim(self, worker_id: str) -> Job | None:
            self.claims += 1
            if self.claims == 1:
                self.failures += 1
                raise ConnectionError("数据库连接被重置")
            return None

    queue = FlakyQueue()
    worker = Worker(queue, {}, worker_id="worker-test", poll_interval_seconds=0)
    running = asyncio.create_task(worker.run_forever())
    try:
        for _ in range(200):
            if queue.claims >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    assert queue.failures == 1
    assert queue.claims >= 3


def test_main_unloads_plugins_when_the_process_exits(
    monkeypatch,
) -> None:
    """进程退出时执行插件的 ``on_unload``：加载发生在任务调用插件时，卸载接在退出时。"""
    unloaded: list[str] = []

    class SpyRegistry:
        def unload_all(self) -> None:
            unloaded.append("unload_all")

    async def fake_run_worker(_settings: object) -> None:
        return None

    monkeypatch.setattr(worker_main, "register_builtins", SpyRegistry)
    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)

    worker_main.main()

    assert unloaded == ["unload_all"]
