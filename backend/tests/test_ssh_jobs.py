"""SSH 失败必须让采集任务变成「失败」，不能被记成成功。

这条链路是验收标准里最容易漏掉的一条：``Worker`` 只在处理器抛异常时才把任务标成失败，
所以 SSH 层的三类失败必须是异常，且一路冒到处理器外面，不能被 ``except`` 吞成返回值。
这里用真实的 ``Worker`` + 真实连接（连接被拒绝 / 本地假服务端）验证，不用替身猜行为。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ssh_fakes import closed_port, fake_ssh_server
from system_analyzer.core.config import Settings
from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import Job
from system_analyzer.remote.client import AsyncSshClient, SshConnectionConfig
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.targets import SshTarget
from system_analyzer.worker.runner import Worker

DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"
PASSWORD = "".join(("unit", "test", "-pw"))
TARGET_ID = "target-under-test"


class FakeQueue:
    """与 ``tests/test_worker.py`` 同形状的假队列：记录成功与失败。"""

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


class OneTargetRepository:
    """固定返回一台目标机器的仓储替身，让任务处理器只关心 SSH 那一半。"""

    def __init__(self, target: SshTarget) -> None:
        self._target = target

    async def get(self, target_id: str) -> SshTarget | None:
        return self._target if target_id == self._target.target_id else None


def make_target(port: int) -> SshTarget:
    now = datetime(2026, 11, 11, tzinfo=UTC)
    return SshTarget(
        target_id=TARGET_ID,
        host="127.0.0.1",
        port=port,
        username="uos",
        password_env_var="SAS_SSH_PASSWORD",
        created_at=now,
        updated_at=now,
    )


def make_service(port: int, *, client_factory=None) -> SshTargetService:
    repository = OneTargetRepository(make_target(port))
    settings = Settings(database_url=DATABASE_URL, ssh_password=PASSWORD)
    if client_factory is None:
        return SshTargetService(repository, settings=settings)
    return SshTargetService(repository, settings=settings, client_factory=client_factory)


def make_job() -> Job:
    return Job(id=uuid4(), kind="collect.ssh", payload={"target_id": TARGET_ID}, attempts=1)


async def test_connection_refused_marks_the_job_failed() -> None:
    """端口没人监听：处理器抛 ``SshConnectError``，任务必须是失败而不是成功。"""
    service = make_service(closed_port())

    async def handler(job: Job) -> None:
        await service.test_connection(job.payload["target_id"])

    queue = FakeQueue([make_job()])
    worker = Worker(queue, {"collect.ssh": handler}, worker_id="worker-test")

    assert await worker.run_once() is True

    assert queue.completed == []
    assert len(queue.failed) == 1
    _job_id, error = queue.failed[0]
    assert "SshConnectError" in error
    assert "建立 SSH 连接失败" in error


async def test_auth_failure_marks_the_job_failed(tmp_path: Path) -> None:
    async with fake_ssh_server(tmp_path) as server:
        wrong = SshConnectionConfig(
            host="127.0.0.1",
            port=server.port,
            username="uos",
            password="".join(("unit", "test", "-wrong-pw")),
        )

        async def factory(_config: SshConnectionConfig) -> AsyncSshClient:
            return await AsyncSshClient.connect(wrong)

        service = make_service(server.port, client_factory=factory)

        async def handler(job: Job) -> None:
            await service.test_connection(job.payload["target_id"])

        queue = FakeQueue([make_job()])
        worker = Worker(queue, {"collect.ssh": handler}, worker_id="worker-test")

        assert await worker.run_once() is True

    assert queue.completed == []
    _job_id, error = queue.failed[0]
    assert "SshAuthError" in error
    assert "认证失败" in error


async def test_command_timeout_marks_the_job_failed(tmp_path: Path) -> None:
    import asyncio

    async def slow(command: str) -> tuple[str, str, int]:
        await asyncio.sleep(10)
        return "", "", 0

    async with fake_ssh_server(tmp_path, handler=slow) as server:

        async def factory(config: SshConnectionConfig) -> AsyncSshClient:
            fast = SshConnectionConfig(
                host=config.host,
                port=config.port,
                username=config.username,
                password=config.password,
                command_timeout=0.2,
            )
            return await AsyncSshClient.connect(fast)

        service = make_service(server.port, client_factory=factory)

        async def handler(job: Job) -> None:
            await service.test_connection(job.payload["target_id"])

        queue = FakeQueue([make_job()])
        worker = Worker(queue, {"collect.ssh": handler}, worker_id="worker-test")

        assert await worker.run_once() is True

    assert queue.completed == []
    _job_id, error = queue.failed[0]
    assert "SshTimeoutError" in error
    assert "超时" in error


async def test_failure_keeps_the_password_out_of_logs_and_job_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """认证失败时最容易泄口令：日志、任务错误、异常消息里都不能出现口令。"""
    expected = "".join(("unit", "test", "-other-pw"))
    async with fake_ssh_server(tmp_path, password=expected) as server:
        service = make_service(server.port)

        async def handler(job: Job) -> None:
            await service.test_connection(job.payload["target_id"])

        queue = FakeQueue([make_job()])
        worker = Worker(queue, {"collect.ssh": handler}, worker_id="worker-test")

        assert await worker.run_once() is True

    _job_id, error = queue.failed[0]
    assert "认证失败" in error
    assert PASSWORD not in error
    assert PASSWORD not in caplog.text


async def test_successful_connection_marks_the_job_complete(tmp_path: Path) -> None:
    """成功的对照组：同一条链路连通时任务必须成功，避免「永远失败」的假安全。"""
    stdout = "hostname=uos-PC\ncpu_count=16\n"

    async def answer(command: str) -> tuple[str, str, int]:
        return stdout, "", 0

    async with fake_ssh_server(tmp_path, handler=answer) as server:
        service = make_service(server.port)

        async def handler(job: Job) -> None:
            info = await service.test_connection(job.payload["target_id"])
            assert info.hostname == "uos-PC"

        job = make_job()
        queue = FakeQueue([job])
        worker = Worker(queue, {"collect.ssh": handler}, worker_id="worker-test")

        assert await worker.run_once() is True

    assert queue.failed == []
    assert queue.completed == [job.id]
