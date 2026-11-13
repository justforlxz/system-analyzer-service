"""采集任务的 Worker 处理器：把任务行接到编排上。

任务由 API 写进 ``jobs`` 表（``kind = "collect.target"``），Worker 领取后：

1. 从任务行读出上一次尝试写回的采集记录，交给编排接着跑（重试与中断恢复都靠它）；
2. 执行三阶段编排，每次状态变化把记录写回 ``payload.result.collection``；
3. 按结果决定任务成败：失败与超时抛异常让 ``Worker`` 记成失败并按重试策略重跑；
   取消是「按用户要求停下」，不算失败（见 ``run_collection_job`` 的说明）。

任务 payload：

```json
{
  "target_id": "目标机器 id",
  "run_id": "采集任务 id（可选，UUID，默认取任务 id）",
  "options": {
    "backup_paths": ["/etc/systemd/system.conf"],
    "phase_timeouts": {"in_progress": 60}
  },
  "cancel_requested": true
}
```

``cancel_requested`` 由取消采集的调用方写进同一行任务（任务 4.1 的采集管理界面负责写它）；
处理器在两个步骤之间读它一次，读到就把阶段记为 ``cancelled`` 并执行清理阶段。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from system_analyzer.collection.connector import DatabaseTargetConnector, TargetConnector
from system_analyzer.collection.orchestrator import (
    CancelCheck,
    CollectionOutcome,
    run_collection,
)
from system_analyzer.collection.record import CollectionRecord
from system_analyzer.collection.states import AttemptStatus, CollectionPhase
from system_analyzer.core.config import Settings, get_settings
from system_analyzer.core.db import create_engine
from system_analyzer.domain.time_base import normalize_boot_id
from system_analyzer.jobs.models import ANALYSIS_JOB_KIND
from system_analyzer.jobs.queue import Job, JobQueue, JobResultStore
from system_analyzer.worker.runner import JobHandler

logger = logging.getLogger(__name__)

COLLECTION_JOB_KIND = "collect.target"
"""采集任务的类型名，写进 ``jobs.kind``，由 ``worker.main.default_handlers()`` 分发。"""

DEFAULT_SCRATCH_ROOT = Path("var/collections")
"""采集临时目录的根。与 ``Settings.upload_dir`` 一样按进程工作目录解析，可用参数覆盖。"""

LOCK_RENEW_INTERVAL_SECONDS = 60.0
"""执行期间刷新任务锁的间隔。远小于默认的 900 秒锁超时，一次抖动不会让任务被重复领取。"""


class CollectionJobPayload(BaseModel):
    """``jobs.payload`` 里采集任务自己的字段。"""

    model_config = ConfigDict(extra="ignore")

    target_id: str = Field(min_length=1)
    run_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class CollectionJobFailed(RuntimeError):
    """采集没有成功（失败或超时）。由 ``Worker`` 记成任务失败并按重试策略重跑。"""


async def run_collection_job(
    job: Job,
    *,
    connector: TargetConnector,
    queue: JobResultStore,
    scratch_root: Path,
    cancelled: CancelCheck | None = None,
    enqueue_analysis: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
) -> CollectionOutcome:
    """执行一次采集尝试，并把记录写回任务行。

    失败与超时会抛 ``CollectionJobFailed``：``Worker`` 因此把任务记为失败，按任务行自己的
    ``max_attempts`` 决定是否重跑，下一次领取时从记录里接着跑。

    取消**不**抛异常：取消是调用方要求的结果，重试一个已取消的采集只会违背要求。任务因此记为
    成功结束，采集本身的结果写在记录的 ``attempts[-1].status = "cancelled"`` 里——采集管理界面
    读记录判断采集结果，不读任务状态（与 ``import.source`` 的做法一致）。

    例外是「取消且清理失败」：远端没被恢复回去，编排把本次尝试记成 ``failed``，这里照常抛
    ``CollectionJobFailed``，任务会重试、临时目录与备份也保留，由下一次尝试的 Post 再恢复。
    """
    payload = CollectionJobPayload.model_validate(job.payload)
    run_id = _run_id(payload.run_id, job.id)
    previous = CollectionRecord.from_mapping((job.payload.get("result") or {}).get("collection"))
    scratch_dir = scratch_root / run_id
    scratch_dir.mkdir(parents=True, exist_ok=True)

    async def save(record: CollectionRecord) -> None:
        await queue.record_result(job.id, {"collection": record.as_mapping()})

    outcome = await run_collection(
        target_id=payload.target_id,
        run_id=run_id,
        connector=connector,
        scratch_dir=scratch_dir,
        previous=previous,
        options=payload.options,
        phase_timeouts=_phase_timeouts(payload.options),
        cancelled=cancelled,
        save=save,
    )
    analysis_patch = await _enqueue_analysis_after_delivery(outcome, enqueue_analysis)
    if analysis_patch:
        await queue.record_result(
            job.id,
            {"collection": outcome.record.as_mapping(), **analysis_patch},
        )
    _discard_scratch(outcome, scratch_dir)
    if outcome.status in (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT):
        raise CollectionJobFailed(outcome.failure or f"采集任务未成功（{outcome.status}）")
    logger.info(
        "采集任务 %s 结束：%s（目标机器 %s，第 %s 次尝试）",
        job.id,
        outcome.status,
        payload.target_id,
        outcome.attempt.attempt,
    )
    return outcome


async def _enqueue_analysis_after_delivery(
    outcome: CollectionOutcome,
    enqueue: Callable[[dict[str, Any]], Awaitable[Any]] | None,
) -> dict[str, Any]:
    """采集成功导入后入队一次分析任务，与 ``POST /api/imports`` 导入后的行为一致。

    只在本次确实导入了数据时入队：``imported`` 是首次导入成功，``already_imported`` 表示这次
    重试没有重新导入。两种状态都可能缺少分析任务（例如上一次导入后 Worker 退出），所以都尝试
    入队；真正的去重由调用方按 ``collection_id`` 检查已有任务完成。

    入队失败只记录到任务结果里，不把已经成功的采集判成失败：数据已经在库里，标成失败会触发
    重跑采集，代价远大于补一条分析任务。
    """
    if enqueue is None:
        return {}
    delivery = outcome.attempt.data.get("delivery")
    if not isinstance(delivery, Mapping):
        return {}
    if delivery.get("status") not in ("imported", "already_imported"):
        return {}
    collection_id = delivery.get("collection_id")
    if not isinstance(collection_id, str) or not collection_id:
        return {}
    system = outcome.attempt.data.get("system_info")
    system = system if isinstance(system, Mapping) else {}
    hostname = _optional_text(system.get("hostname"))
    boot_raw = _optional_text(system.get("boot_id"))
    if hostname is None or boot_raw is None:
        return {"analysis_enqueue_error": "采集记录里缺少 hostname 或 boot_id，无法确定分析范围"}
    try:
        boot_id = normalize_boot_id(boot_raw)
    except ValueError as error:
        return {"analysis_enqueue_error": f"boot_id 不合法：{error}"}
    payload = {
        "machine_id": hostname if hostname.startswith("machine:") else f"machine:{hostname}",
        "boot_id": boot_id,
        "collection_id": collection_id,
    }
    try:
        job_id = await enqueue(payload)
    except Exception as error:
        logger.exception("采集导入完成后没能入队分析任务")
        return {"analysis_enqueue_error": f"{type(error).__name__}: {error}"}
    if job_id is None:
        return {}
    return {"analysis_job_id": str(job_id)}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _run_id(raw: str | None, job_id: UUID) -> str:
    """采集任务 id。``run_id`` 会拼进 Worker 上的临时目录名，所以只接受 UUID。

    调用方不传时用任务 id（也是 UUID）。这里不接受任意字符串：``run_id`` 会在任务成功或取消后
    被 ``shutil.rmtree`` 递归删除，允许 ``..`` 之类的字符等于把删除范围交给调用方。
    """
    if raw is None:
        return str(job_id)
    try:
        return str(UUID(raw))
    except ValueError as exc:
        raise ValueError(f"payload.run_id 必须是 UUID，实际是 {raw!r}") from exc


def _phase_timeouts(options: Mapping[str, Any]) -> dict[CollectionPhase, float]:
    """从 payload 的 ``options.phase_timeouts`` 读出按阶段的超时覆盖值。"""
    raw = options.get("phase_timeouts") or {}
    return {CollectionPhase(phase): float(seconds) for phase, seconds in raw.items()}


def _discard_scratch(outcome: CollectionOutcome, scratch_dir: Path) -> None:
    """任务不会再重跑时删掉本次采集的临时目录，避免 Worker 上堆临时文件。

    失败与超时保留：下一次尝试的 Post 阶段可能还要用 Pre 备份的远端文件。
    """
    if outcome.status in (AttemptStatus.SUCCEEDED, AttemptStatus.CANCELLED):
        shutil.rmtree(scratch_dir, ignore_errors=True)


async def job_cancel_requested(engine: AsyncEngine, job_id: UUID) -> bool:
    """任务行上的取消标记是否已置上。取消采集的调用方把它写进同一行任务。"""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT COALESCE((payload ->> 'cancel_requested')::boolean, false) AS flag
                      FROM jobs
                     WHERE id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
        ).one_or_none()
    return bool(row.flag) if row is not None else False


@asynccontextmanager
async def renew_job_lock(
    engine: AsyncEngine, job_id: UUID, *, interval: float
) -> AsyncIterator[None]:
    """执行期间定期刷新 ``jobs.locked_at``，避免长任务被别的 Worker 当成失锁任务重新领取。

    ``docs/implementation-baseline.md`` 第 2.1 节把「执行期间不续租锁定字段」列为引入采集处理器
    之前必须处理的限制。续租只更新自己这一行的 ``locked_at``，且只在任务仍是 ``running`` 时更新；
    单次续租失败只记日志、不中断采集，也不把异常抛给调用方。
    """
    if interval <= 0:
        yield
        return

    async def loop() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            """
                            UPDATE jobs
                               SET locked_at = now()
                             WHERE id = :job_id AND status = 'running'
                            """
                        ),
                        {"job_id": job_id},
                    )
            except Exception:
                # 续租失败不该中断采集，也不该把已经成功的任务记成失败：记一次日志，下一轮再试。
                # 数据库长时间不可用时，后续写采集记录会失败并把任务记为失败，不会静默跑完。
                logger.warning("刷新任务锁失败，下一轮继续尝试", exc_info=True)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def collection_handlers(
    settings: Settings | None = None,
    connector: TargetConnector | None = None,
    *,
    scratch_root: Path | None = None,
    lock_renew_interval: float = LOCK_RENEW_INTERVAL_SECONDS,
) -> dict[str, JobHandler]:
    """采集任务的处理器清单，由 ``worker.main.default_handlers()`` 并入。

    与 ``worker.import_jobs.import_handlers`` 同样自己开一条引擎、一个队列，用完就关：
    ``default_handlers()`` 拿不到 ``run_worker`` 里的那个队列。
    """
    resolved_settings = settings or get_settings()
    resolved_scratch_root = scratch_root or DEFAULT_SCRATCH_ROOT
    resolved_connector = connector or DatabaseTargetConnector(
        resolved_settings.database_url, resolved_settings
    )

    async def handle_collect(job: Job) -> None:
        engine = create_engine(resolved_settings.database_url)
        try:
            queue = JobQueue(
                engine, lock_timeout_seconds=resolved_settings.worker_lock_timeout_seconds
            )
            async with renew_job_lock(engine, job.id, interval=lock_renew_interval):
                await run_collection_job(
                    job,
                    connector=resolved_connector,
                    queue=queue,
                    scratch_root=resolved_scratch_root,
                    cancelled=partial(job_cancel_requested, engine, job.id),
                    enqueue_analysis=partial(enqueue_analysis_job_once, engine, queue),
                )
        finally:
            await engine.dispose()

    return {COLLECTION_JOB_KIND: handle_collect}


async def enqueue_analysis_job_once(
    engine: AsyncEngine,
    queue: JobQueue,
    payload: dict[str, Any],
) -> UUID | None:
    """按 ``collection_id`` 去重后入队分析任务，返回已有或新建的任务 id。

    采集重试可能在导入成功之后再次走到这里。分析任务本身有重试机制，重复入队只会让同一份
    数据被分析多次；这里先查同一个采集是否已经排过分析任务，有就复用它的 id。
    """
    collection_id = payload.get("collection_id")
    if not isinstance(collection_id, str) or not collection_id:
        raise ValueError("分析任务 payload 缺少 collection_id")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                text(
                    """
                    SELECT id
                      FROM jobs
                     WHERE kind = :kind
                       AND payload ->> 'collection_id' = :collection_id
                     ORDER BY created_at
                     LIMIT 1
                    """
                ),
                {"kind": ANALYSIS_JOB_KIND, "collection_id": collection_id},
            )
        ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    return await queue.enqueue(ANALYSIS_JOB_KIND, payload)
