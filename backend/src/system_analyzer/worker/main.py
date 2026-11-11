"""后台任务进程（Worker）入口，命令 ``uv run sas-worker``。"""

import asyncio
import logging

from system_analyzer.core.config import Settings, get_settings
from system_analyzer.core.db import create_engine
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.worker.runner import JobHandler, Worker


def default_handlers() -> dict[str, JobHandler]:
    """当前注册的任务处理器。

    采集、导入与分析任务由任务 1.3、1.4、2.1、2.2、3.x 逐步加入；
    本骨架阶段没有任务类型，Worker 启动后只会空转轮询。
    """
    return {}


async def run_worker(settings: Settings) -> None:
    """在同一个事件循环里执行任务循环，退出时释放连接池。"""
    engine = create_engine(settings.database_url)
    try:
        queue = JobQueue(engine, lock_timeout_seconds=settings.worker_lock_timeout_seconds)
        worker = Worker(
            queue,
            default_handlers(),
            worker_id=settings.worker_id,
            poll_interval_seconds=settings.worker_poll_interval_seconds,
        )
        await worker.run_forever()
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    registry = register_builtins()
    try:
        asyncio.run(run_worker(settings))
    finally:
        # 进程退出（Ctrl+C、未捕获异常都算）时执行插件的 on_unload，用的是加载它的那次上下文。
        registry.unload_all()


if __name__ == "__main__":
    main()
