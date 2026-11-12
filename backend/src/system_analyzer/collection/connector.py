"""采集编排与 ``remote/`` 之间的连接接缝。

编排只关心「拿到一条已经认证好的 SSH 连接」，不关心凭据从哪来、连的是哪台机器：
生产实现是 ``DatabaseTargetConnector``（读 ``ssh_targets`` 表 + 环境变量里的口令，
连接逻辑全部复用任务 3.1 的 ``remote/``），测试实现是测试文件里的假连接器。

这样 3.4–3.6 的采集动作可以直接写在 ``collection.plan`` 里，不必自己管数据库与凭据；
测试也能用假 ``SshClient`` 走完整个状态机，不需要真机。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.core.config import Settings
from system_analyzer.remote.client import SshClient
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.targets import SshTargetRepository


class TargetConnector(Protocol):
    """按目标机器 id 建立一条 SSH 连接。退出上下文时连接关闭。"""

    def connect(self, target_id: str) -> AbstractAsyncContextManager[SshClient]:
        """打开一条到 ``target_id`` 的连接。连不上、认证失败、超时时抛 ``SshError`` 子类。"""


class DatabaseTargetConnector:
    """生产实现：从库里取目标机器与凭据，走 ``remote/`` 的连接逻辑。

    与 ``remote/sync.py`` 的做法一致，每次连接用一条 ``NullPool`` 引擎、用完即关：采集编排运行在
    Worker 的进程里，而 Worker 的 ``run_worker`` 里那条引擎没有传给任务处理器，短命引擎的连接
    开销相对于一次 SSH 采集可以忽略。
    """

    def __init__(self, database_url: str, settings: Settings) -> None:
        self._database_url = database_url
        self._settings = settings

    @asynccontextmanager
    async def connect(self, target_id: str) -> AsyncIterator[SshClient]:
        """建立连接并在退出时关闭。"""
        engine = create_async_engine(self._database_url, poolclass=NullPool)
        try:
            service = SshTargetService(SshTargetRepository(engine), settings=self._settings)
            async with service.connect(target_id) as client:
                yield client
        finally:
            await engine.dispose()
