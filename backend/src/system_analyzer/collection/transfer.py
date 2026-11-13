"""``.bootrec`` 的可靠回传：失败可重试、远端数据在导入成功前不删。

回传只做两件事：把远端文件取回本地，以及在中心端确认导入成功后删除远端副本。它不负责
打包，也不负责导入，因此这三件事可以分别测试：

1. **下载失败不删远端。** 下载抛 ``RemoteError`` / ``OSError`` 时，本模块只记录这次失败，
   远端文件保持原样；下次恢复网络后从同一个路径重新下载即可。
2. **导入成功后才清理。** ``cleanup_after_import`` 只有 ``imported=True`` 才执行
   ``rm -f``；导入失败时远端保留，供重新下载与重试。
3. **重试有上限。** 每次尝试都留下一条记录（第几次、是否成功、失败原因），避免无限重试与
   「静默卡住」。
"""

from __future__ import annotations

import asyncio
import inspect
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import RemoteError

DEFAULT_DOWNLOAD_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 0.0


class RemoteFileClient(Protocol):
    """回传需要的最小远端能力：取文件、执行命令。"""

    async def download(self, remote_path: str, local_path: Path | str) -> None: ...

    async def upload(self, local_path: Path | str, remote_path: str) -> None: ...

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
        check: bool = True,
    ) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class TransferAttempt:
    """一次下载尝试的结果。"""

    number: int
    succeeded: bool
    error: str | None = None

    def as_mapping(self) -> dict[str, object]:
        return {"number": self.number, "succeeded": self.succeeded, "error": self.error}


@dataclass(frozen=True, slots=True)
class TransferReport:
    """一次回传的完整结果。``remote_retained`` 为真表示远端文件还在。"""

    remote_path: str
    local_path: Path
    attempts: tuple[TransferAttempt, ...]
    succeeded: bool
    remote_retained: bool
    error: str | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "remote_path": self.remote_path,
            "local_path": str(self.local_path),
            "attempts": [attempt.as_mapping() for attempt in self.attempts],
            "succeeded": self.succeeded,
            "remote_retained": self.remote_retained,
            "error": self.error,
        }


async def pull_with_retry(
    client: RemoteFileClient,
    remote_path: str,
    local_path: str | Path,
    *,
    attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
    delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> TransferReport:
    """从远端取一份文件，失败后按次数重试。任何失败都不删除远端文件。

    下载先写到与目标同目录的 ``.part`` 文件，完整拿到后才原子替换目标文件。这样重试期间
    本地已有的包不会被半截下载覆盖：下载失败时删掉残留的 ``.part``，原文件保持原样。
    """
    if attempts < 1:
        raise ValueError(f"下载尝试次数必须是正整数，实际是 {attempts}")
    if delay_seconds < 0:
        raise ValueError(f"重试间隔不能为负数，实际是 {delay_seconds}")
    target = Path(local_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    history: list[TransferAttempt] = []
    last_error: str | None = None
    for number in range(1, attempts + 1):
        try:
            result = client.download(remote_path, partial)
            if inspect.isawaitable(result):
                await result
            if not partial.is_file():
                raise RemoteError(f"下载后本地文件不存在：{partial}")
            if partial.stat().st_size == 0:
                raise RemoteError(f"下载到的文件是空的：{partial}")
            partial.replace(target)
        except (RemoteError, OSError) as error:
            partial.unlink(missing_ok=True)
            last_error = f"{type(error).__name__}: {error}"
            history.append(TransferAttempt(number=number, succeeded=False, error=last_error))
            if number < attempts and delay_seconds:
                await sleep(delay_seconds)
            continue
        history.append(TransferAttempt(number=number, succeeded=True))
        return TransferReport(
            remote_path=remote_path,
            local_path=target,
            attempts=tuple(history),
            succeeded=True,
            remote_retained=True,
        )
    return TransferReport(
        remote_path=remote_path,
        local_path=target,
        attempts=tuple(history),
        succeeded=False,
        remote_retained=True,
        error=last_error,
    )


async def remove_remote_file(
    client: RemoteFileClient,
    remote_path: str,
    *,
    imported: bool,
    timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
) -> bool:
    """导入成功后才删除远端文件。返回 ``True`` 表示确认已删除。

    ``imported=False`` 直接返回 ``False``，连命令都不发：导入没有成功时，远端数据必须留着。
    删除失败同样返回 ``False``，由调用方把「清理没成功」告诉读结果的人，而不是当成成功。
    """
    if not imported:
        return False
    try:
        result = client.run(f"rm -f -- {shlex.quote(remote_path)}", timeout=timeout, check=False)
        if inspect.isawaitable(result):
            result = await result
    except (RemoteError, OSError):
        return False
    return bool(getattr(result, "ok", False))


async def stage_remote_file(
    client: RemoteFileClient,
    local_path: str | Path,
    remote_path: str,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
) -> None:
    """把本地文件放到远端暂存路径，供回传链路取回（真机验证与重试测试用）。"""
    remote_dir = str(Path(remote_path).parent)
    result = client.run(f"mkdir -p -- {shlex.quote(remote_dir)}", timeout=timeout, check=True)
    if inspect.isawaitable(result):
        await result
    upload = client.upload(local_path, remote_path)
    if inspect.isawaitable(upload):
        await upload


__all__ = [
    "DEFAULT_DOWNLOAD_ATTEMPTS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "RemoteFileClient",
    "TransferAttempt",
    "TransferReport",
    "pull_with_retry",
    "remove_remote_file",
    "stage_remote_file",
]
