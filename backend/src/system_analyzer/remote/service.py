"""目标机器的服务层：增删改查 + 连接测试。

把三样东西接在一起：

- ``SshTargetRepository``：目标机器记录（数据库）。
- ``EnvironmentPasswordResolver``：口令引用 → 运行环境里的口令。
- ``open_ssh_client``：真正的 SSH 连接（测试里换成假客户端）。

``connect`` 是一次采集的连接入口：3.4 起的 Collector 插件从这里拿一条已经认证好的连接，
用完自动断开。``test_connection`` 在它之上跑一次只读的系统基本信息探测。

失败信号不吞：连接失败、认证失败、超时、命令失败都以对应的异常上抛，调用方（Worker 的任务
处理器）让异常冒出来，任务就会被记为失败——``tests/test_ssh_jobs.py`` 覆盖了这条链路。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from system_analyzer.core.config import Settings
from system_analyzer.remote.client import (
    SshClient,
    SshConnectionConfig,
    close_client,
    open_ssh_client,
)
from system_analyzer.remote.credentials import EnvironmentPasswordResolver
from system_analyzer.remote.errors import TargetNotFound
from system_analyzer.remote.probe import SystemInfo, probe_system_info
from system_analyzer.remote.targets import SshTarget, SshTargetDraft, SshTargetRepository

ClientFactory = Callable[[SshConnectionConfig], Awaitable[SshClient]]
"""建立连接的工厂。默认 ``open_ssh_client``；测试注入假客户端或假服务端。"""


class SshTargetService:
    """目标机器的业务入口。仓储与凭据来源都可以替换，便于测试。"""

    def __init__(
        self,
        repository: SshTargetRepository,
        *,
        settings: Settings,
        client_factory: ClientFactory = open_ssh_client,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._client_factory = client_factory

    # ---- 增删改查 ---------------------------------------------------------

    async def create(self, draft: SshTargetDraft) -> SshTarget:
        """新增一台目标机器。校验在 ``SshTargetDraft`` 里，非法输入直接拒绝。"""
        return await self._repository.create(draft)

    async def update(self, target_id: str, draft: SshTargetDraft) -> SshTarget:
        """整体替换一台目标机器；不存在时抛 ``TargetNotFound``。"""
        return await self._repository.update(target_id, draft)

    async def delete(self, target_id: str) -> None:
        """删除一台目标机器；不存在时抛 ``TargetNotFound``。"""
        await self._repository.delete(target_id)

    async def list_targets(self) -> list[SshTarget]:
        """列出全部目标机器。"""
        return await self._repository.list()

    async def get(self, target_id: str) -> SshTarget:
        """按 id 取一台目标机器；不存在时抛 ``TargetNotFound``。"""
        target = await self._repository.get(target_id)
        if target is None:
            raise TargetNotFound(f"目标机器不存在：{target_id}")
        return target

    # ---- 连接与连接测试 ---------------------------------------------------

    def connection_config(self, target: SshTarget) -> SshConnectionConfig:
        """把目标机器记录 + 运行环境里的口令拼成连接参数。

        口令在进入 ``SshConnectionConfig`` 之前不会出现在任何日志或异常里；对象本身也带
        ``repr=False``，打印配置不会泄口令。
        """
        password = EnvironmentPasswordResolver(self._settings).resolve(target.password_env_var)
        return SshConnectionConfig(
            host=target.host,
            port=target.port,
            username=target.username,
            password=password,
        )

    @asynccontextmanager
    async def connect(self, target_or_id: SshTarget | str) -> AsyncIterator[SshClient]:
        """打开一条连接并在退出时关闭。这是 3.4 起采集插件拿连接的地方（异步形态）。"""
        target = await self.get(target_or_id) if isinstance(target_or_id, str) else target_or_id
        client = await self._client_factory(self.connection_config(target))
        try:
            yield client
        finally:
            await close_client(client)

    async def test_connection(self, target_id: str) -> SystemInfo:
        """对目标机器做一次连接测试，返回系统基本信息。任何失败都上抛，不返回空结果。"""
        async with self.connect(target_id) as client:
            return await probe_system_info(client)
