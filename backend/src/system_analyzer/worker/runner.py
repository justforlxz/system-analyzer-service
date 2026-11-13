"""Worker 主循环：从任务队列领取任务，交给任务处理器执行。

采集（SSH）、导入与后台分析任务都跑在这个进程里；API 进程只负责把任务写进队列。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from system_analyzer.jobs.queue import Job, JobQueueLike

logger = logging.getLogger(__name__)

JobHandler = Callable[[Job], Awaitable[None]]


class Worker:
    """单进程任务执行器。"""

    def __init__(
        self,
        queue: JobQueueLike,
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers)
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds

    async def run_once(self) -> bool:
        """领取并执行一个任务。执行了任务返回 ``True``，队列为空返回 ``False``。"""
        job = await self._queue.claim(self._worker_id)
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        if handler is None:
            status = await self._queue.fail(job.id, f"未注册的任务类型: {job.kind}")
            logger.error("任务 %s 类型未知: %s，转入 %s", job.id, job.kind, status.value)
            return True
        try:
            await handler(job)
        except Exception as exc:
            status = await self._queue.fail(job.id, f"{type(exc).__name__}: {exc}")
            logger.exception("任务 %s 执行失败，转入 %s", job.id, status.value)
        else:
            await self._queue.complete(job.id)
            logger.info("任务 %s 执行成功", job.id)
        return True

    async def run_forever(self) -> None:
        """循环执行任务，队列为空时按配置的间隔等待。

        队列本身出错（数据库暂时不可用、连接被重置）时只记日志并等下一轮：单次抖动不能让
        整个 Worker 进程退出——进程退出后没有任何东西会把它拉起来，采集、导入与分析任务会
        一直积压在队列里。任务处理器内部的异常已经在 ``run_once`` 里处理，不会走到这里。
        """
        logger.info("Worker %s 启动", self._worker_id)
        while True:
            try:
                handled = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Worker %s 本轮执行失败，%.1f 秒后重试",
                    self._worker_id,
                    self._poll_interval,
                )
                handled = False
            if not handled:
                await asyncio.sleep(self._poll_interval)
