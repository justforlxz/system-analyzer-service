"""Worker 主循环测试：用假队列验证成功、失败与未知类型的处理。"""

from uuid import UUID, uuid4

from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import Job
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
