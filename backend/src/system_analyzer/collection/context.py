"""阶段步骤能拿到的东西：目标机器、临时目录、阶段之间传递的数据，以及一条 SSH 连接。

步骤是普通协程函数，签名统一为 ``async def step(ctx: PhaseContext) -> None``，
拿到的东西全在这里，不自己读配置、不自己连库。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system_analyzer.collection.connector import TargetConnector
from system_analyzer.remote.client import SshClient


@dataclass(frozen=True, slots=True)
class PhaseContext:
    """一次尝试里所有步骤共用的上下文。

    ``data`` 与 ``AttemptRecord.data`` 是同一个字典：步骤写进去的中间产物
    （例如 Pre 的备份清单）会随记录一起持久化，重试时 Post 还能拿到它把远端恢复回去。
    """

    target_id: str
    run_id: str
    attempt: int
    options: Mapping[str, Any]
    scratch_dir: Path
    data: dict[str, Any]
    connector: TargetConnector

    def connect(self) -> AbstractAsyncContextManager[SshClient]:
        """打开一条到本次采集目标机器的连接，用 ``async with`` 关闭。"""
        return self.connector.connect(self.target_id)
