"""把异步的 SSH 入口包成同步方法，供 3.4 起的 Collector 插件使用。

插件契约（``docs/plugin-contracts.md`` 第 4 节）把 ``CollectorPlugin.collect`` 定成同步方法，
而本任务的连接、命令执行、文件传输都是协程。桥接方式与 ``api/sync_store.py`` 处理导入插件时
一致：Worker 把插件调用放进工作线程，线程里的每次数据访问都开一条短命事件循环；连接池用
``NullPool``，连接用完就关，不跨事件循环复用 asyncpg 连接。

用法（3.4 的 Collector 插件）：

```python
class ResourceCollector:
    spec = PluginSpec(...)

    def collect(self, request: CollectRequest, context: PluginContext) -> CollectResult:
        remote = SyncSshTargetService(get_settings().database_url)
        info = remote.test_connection(request.target_id)
        remote.download(request.target_id, "/proc/meminfo", local_path)
        ...
```

在事件循环线程里调用这些方法会直接报错：那里 ``asyncio.run`` 无法执行，硬跑只会得到一个
含义不明的运行时异常。这条路径只该由 Worker 的工作线程走。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.core.config import Settings, get_settings
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.probe import SystemInfo
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.targets import SshTarget, SshTargetDraft, SshTargetRepository


def run_sync[T](call: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """在一条独立的事件循环里执行一次异步调用。

    只能在非事件循环线程里调用（Worker 的工作线程）；在事件循环线程里调用直接抛
    ``RuntimeError``，避免用 ``asyncio.run`` 造出难以理解的错误。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("同步 SSH 入口不能在事件循环线程里调用；请在 Worker 的工作线程里使用")
    return asyncio.run(call())


class SyncSshTargetService:
    """同步形态的目标机器入口：每个方法开一条短命事件循环与一条短命数据库连接。

    一次插件调用里的数据访问只有一两次（取目标机器、连一次），开销可以接受；
    换来的是插件永远只看到同步方法，不需要自己管事件循环。
    """

    def __init__(self, database_url: str, *, settings: Settings | None = None) -> None:
        self._database_url = database_url
        self._settings = settings or get_settings()

    # ---- 增删改查（同步形态，供 API 之类不方便直接用协程的调用方） ----------

    def list_targets(self) -> list[SshTarget]:
        """列出全部目标机器。"""
        return self._run(lambda service: service.list_targets())

    def get_target(self, target_id: str) -> SshTarget:
        """按 id 取一台目标机器；不存在时抛 ``TargetNotFound``。"""
        return self._run(lambda service: service.get(target_id))

    def create_target(self, draft: SshTargetDraft) -> SshTarget:
        """新增一台目标机器。"""
        return self._run(lambda service: service.create(draft))

    def update_target(self, target_id: str, draft: SshTargetDraft) -> SshTarget:
        """整体替换一台目标机器。"""
        return self._run(lambda service: service.update(target_id, draft))

    def delete_target(self, target_id: str) -> None:
        """删除一台目标机器。"""
        self._run(lambda service: service.delete(target_id))

    # ---- 连接与操作 -------------------------------------------------------

    def test_connection(self, target_id: str) -> SystemInfo:
        """连接测试：返回目标机器的系统基本信息，失败时抛出对应的 ``SshError``。"""
        return self._run(lambda service: service.test_connection(target_id))

    def run_command(
        self,
        target_id: str,
        command: str,
        *,
        timeout: float | None = None,
        check: bool = True,
    ) -> CommandResult:
        """在目标机器上执行一条命令。``check`` 为真时非 0 退出码抛 ``SshCommandError``。"""

        async def action(service: SshTargetService) -> CommandResult:
            async with service.connect(target_id) as client:
                return await client.run(command, timeout=timeout, check=check)

        return self._run(action)

    def upload(self, target_id: str, local_path: str, remote_path: str) -> None:
        """把本地文件传到目标机器。"""

        async def action(service: SshTargetService) -> None:
            async with service.connect(target_id) as client:
                await client.upload(local_path, remote_path)

        self._run(action)

    def download(self, target_id: str, remote_path: str, local_path: str) -> None:
        """把目标机器上的文件取回本地。"""

        async def action(service: SshTargetService) -> None:
            async with service.connect(target_id) as client:
                await client.download(remote_path, local_path)

        self._run(action)

    def _run[T](self, action: Callable[[SshTargetService], Awaitable[T]]) -> T:
        """开一条短命事件循环与一条短命数据库连接，执行一次 ``action``。"""

        async def body() -> T:
            async with _open_service(self._database_url, self._settings) as service:
                return await action(service)

        return run_sync(body)


@asynccontextmanager
async def _open_service(database_url: str, settings: Settings) -> AsyncIterator[SshTargetService]:
    """建一条 ``NullPool`` 连接（用完即关），组装出 ``SshTargetService``。"""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        yield SshTargetService(SshTargetRepository(engine), settings=settings)
    finally:
        await engine.dispose()
