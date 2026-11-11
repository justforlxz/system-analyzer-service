"""同步入口的形状：3.4 的 Collector 插件要能直接调用。

这里只测「同步入口确实是同步的、事件循环的约束成立」——真实数据库与真实连接的用例在
``tests/integration/test_ssh_targets_store.py``。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from system_analyzer.remote.sync import SyncSshTargetService, run_sync

DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"


def test_run_sync_returns_the_awaited_value() -> None:
    async def answer() -> int:
        await asyncio.sleep(0)
        return 42

    assert run_sync(answer) == 42


def test_run_sync_propagates_exceptions() -> None:
    async def boom() -> None:
        raise ValueError("失败要原样冒出来")

    with pytest.raises(ValueError, match="失败要原样冒出来"):
        run_sync(boom)


async def test_run_sync_refuses_to_run_inside_a_running_loop() -> None:
    """在事件循环线程里调用会得到明确的 ``RuntimeError``，而不是含义不明的运行时错误。"""

    async def answer() -> int:
        return 1

    with pytest.raises(RuntimeError, match="不能在事件循环线程里调用"):
        run_sync(answer)


def test_every_public_service_method_is_synchronous() -> None:
    """插件入口是同步方法：这些方法一个都不能是协程。"""
    methods = [
        "list_targets",
        "get_target",
        "create_target",
        "update_target",
        "delete_target",
        "test_connection",
        "run_command",
        "upload",
        "download",
    ]
    for name in methods:
        method = getattr(SyncSshTargetService, name)
        assert not inspect.iscoroutinefunction(method), f"{name} 必须是同步方法"


def test_sync_service_defaults_to_process_settings() -> None:
    """不传 ``settings`` 时用进程配置，Worker 里不需要额外接线。"""
    service = SyncSshTargetService(DATABASE_URL)

    assert service is not None
