"""PostgreSQL 支撑的后台任务队列。

选型理由见 ``docs/implementation-baseline.md``：不引入 Redis、RabbitMQ 等计划外中间件，
任务状态与任务本体都存在 PostgreSQL 的 ``jobs`` 表里，Worker 用
``SELECT ... FOR UPDATE SKIP LOCKED`` 领取任务，多个 Worker 可以安全并行。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from system_analyzer.jobs.models import JobStatus, status_after_failure

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Job:
    """一个被领取到的任务。"""

    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class JobRecord:
    """按任务 id 读回的一条任务记录，供任务状态查询使用。

    只读：``payload`` 是提交任务时写入的内容，任务执行方可以用 ``record_result`` 往里补
    一段 ``result``；状态查询只暴露其中约定的字段，不把整个 payload 交给调用方。
    """

    id: UUID
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class JobResultStore(Protocol):
    """任务执行方补充任务结果的入口，是 ``JobQueue`` 的一个子集。

    单独成一个协议，是为了让任务处理器只依赖「把结果写回任务」这一个能力，
    不必持有完整的队列。
    """

    async def record_result(self, job_id: UUID, result: Mapping[str, Any]) -> None: ...


class JobQueueLike(Protocol):
    """Worker 依赖的最小任务队列接口，便于用假队列做单元测试。"""

    async def claim(self, worker_id: str) -> Job | None: ...

    async def complete(self, job_id: UUID) -> None: ...

    async def fail(self, job_id: UUID, error: str) -> JobStatus: ...


class JobQueue:
    """基于 PostgreSQL 的任务队列实现。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
        lock_timeout_seconds: int = 900,
    ) -> None:
        self._engine = engine
        self._retry_delay_seconds = retry_delay_seconds
        self._lock_timeout_seconds = lock_timeout_seconds

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        run_after: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> UUID:
        """写入一个待执行任务，返回任务 id。

        ``max_attempts`` 写进任务行的同名列，失败后是否重试由任务行自己的值决定。
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    INSERT INTO jobs (kind, payload, status, run_after, max_attempts)
                    VALUES (
                        :kind,
                        CAST(:payload AS jsonb),
                        'queued',
                        COALESCE(:run_after, now()),
                        :max_attempts
                    )
                    RETURNING id
                    """
                ),
                {
                    "kind": kind,
                    "payload": "{}" if payload is None else json.dumps(payload, ensure_ascii=False),
                    "run_after": run_after,
                    "max_attempts": max_attempts,
                },
            )
            return result.scalar_one()

    async def claim(self, worker_id: str) -> Job | None:
        """领取一个到期的任务；没有任务时返回 ``None``。

        领取前先把锁超时的任务放回队列，避免 Worker 异常退出后任务永远卡在 running。
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET status = 'queued', locked_by = NULL, locked_at = NULL, updated_at = now()
                     WHERE status = 'running'
                       AND locked_at < now() - make_interval(secs => :timeout)
                    """
                ),
                {"timeout": self._lock_timeout_seconds},
            )
            result = await conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET status = 'running',
                           attempts = attempts + 1,
                           locked_by = :worker_id,
                           locked_at = now(),
                           updated_at = now()
                     WHERE id = (
                             SELECT id FROM jobs
                              WHERE status = 'queued' AND run_after <= now()
                              ORDER BY run_after, created_at
                              FOR UPDATE SKIP LOCKED
                              LIMIT 1
                           )
                    RETURNING id, kind, payload, attempts
                    """
                ),
                {"worker_id": worker_id},
            )
            row = result.one_or_none()
        if row is None:
            return None
        return Job(id=row.id, kind=row.kind, payload=row.payload, attempts=row.attempts)

    async def complete(self, job_id: UUID) -> None:
        """把任务标记为成功。"""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET status = 'succeeded',
                           locked_by = NULL,
                           locked_at = NULL,
                           updated_at = now()
                     WHERE id = :job_id AND status = 'running'
                    """
                ),
                {"job_id": job_id},
            )

    async def fail(self, job_id: UUID, error: str) -> JobStatus:
        """记录失败；还有重试次数就回到队列，否则进入失败终态。"""
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT attempts, max_attempts FROM jobs
                         WHERE id = :job_id AND status = 'running'
                         FOR UPDATE
                        """
                    ),
                    {"job_id": job_id},
                )
            ).one_or_none()
            if row is None:
                return JobStatus.FAILED
            next_status = status_after_failure(row.attempts, row.max_attempts)
            await conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET status = :status,
                           last_error = :error,
                           locked_by = NULL,
                           locked_at = NULL,
                           run_after = now() + make_interval(secs => :delay),
                           updated_at = now()
                     WHERE id = :job_id
                    """
                ),
                {
                    "status": next_status.value,
                    "error": error[:4000],
                    "delay": self._retry_delay_seconds,
                    "job_id": job_id,
                },
            )
        return next_status

    async def get(self, job_id: UUID) -> JobRecord | None:
        """按任务 id 读回一条任务记录；任务不存在时返回 ``None``。

        只读一条，不锁行，不改状态：查询任务状态的地方（API）用它。
        """
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT id, kind, payload, status, attempts, max_attempts,
                               last_error, created_at, updated_at
                          FROM jobs
                         WHERE id = :job_id
                        """
                    ),
                    {"job_id": job_id},
                )
            ).one_or_none()
        if row is None:
            return None
        return JobRecord(
            id=row.id,
            kind=row.kind,
            payload=row.payload,
            status=JobStatus(row.status),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def record_result(self, job_id: UUID, result: Mapping[str, Any]) -> None:
        """把任务结果写回任务行的 ``payload.result``，不改变任务状态。

        结果与状态分开写：任务成不成功由 ``complete`` / ``fail`` 决定，结果只是执行方记录的
        产物（例如导入写了哪次采集）。只按任务 id 定位，任务行不存在时什么都不写。
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE jobs
                       SET payload = jsonb_set(
                               payload,
                               '{result}',
                               CAST(:result AS jsonb),
                               true
                           ),
                           updated_at = now()
                     WHERE id = :job_id
                    """
                ),
                {"job_id": job_id, "result": json.dumps(result, ensure_ascii=False)},
            )
