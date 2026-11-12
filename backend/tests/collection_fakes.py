"""采集编排测试用的替身：假 SSH 客户端与假连接器。

采集编排只通过 ``TargetConnector`` 拿连接，所以这里用假客户端就能把整个状态机走完，
既不需要真机，也不需要在测试里起 SSH 服务端。与 ``tests/ssh_fakes.py`` 里那个真协议栈的
假服务端分工不同：那一个验证 SSH 客户端本身，这一个验证编排对连接做了什么。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from system_analyzer.remote.client import CommandResult

PROBE_STDOUT_TEMPLATE = "hostname={hostname}\nos_release=UOS 25\nkernel_version=6.1.0\n"


class RecordingSshClient:
    """记录命令、上传、下载的假 ``SshClient``。

    ``files`` 是这台目标机器上的文件内容，直接引用调用方给的字典（不复制）：上传因此会
    改到调用方看得见的那一份，「远端文件被恢复成原样」可以直接断言内容。
    """

    def __init__(self, files: dict[str, bytes], *, hostname: str) -> None:
        self.files = files
        self.hostname = hostname
        self.commands: list[str] = []
        self.downloads: list[str] = []
        self.uploads: list[str] = []
        self.closed = False

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            command=command,
            stdout=PROBE_STDOUT_TEMPLATE.format(hostname=self.hostname),
            stderr="",
            exit_status=0,
        )

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        self.uploads.append(remote_path)
        self.files[remote_path] = Path(local_path).read_bytes()  # noqa: ASYNC240  测试里只读小文件

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        if remote_path not in self.files:
            raise FileNotFoundError(f"目标机器上不存在文件：{remote_path}")
        self.downloads.append(remote_path)
        Path(local_path).write_bytes(self.files[remote_path])  # noqa: ASYNC240  测试里只写小文件

    async def aclose(self) -> None:
        self.closed = True


class FakeConnector:
    """按目标机器 id 返回各自的假客户端，并记录每台机器被连了几次。"""

    def __init__(self, machines: dict[str, dict[str, bytes]]) -> None:
        self.files = {target_id: dict(files) for target_id, files in machines.items()}
        self.clients: dict[str, list[RecordingSshClient]] = {key: [] for key in machines}
        self.connected: list[str] = []

    @asynccontextmanager
    async def connect(self, target_id: str) -> AsyncIterator[RecordingSshClient]:
        self.connected.append(target_id)
        client = RecordingSshClient(self.files[target_id], hostname=target_id)
        self.clients[target_id].append(client)
        try:
            yield client
        finally:
            await client.aclose()

    def commands_of(self, target_id: str) -> list[str]:
        """这台机器上执行过的全部命令，按发生顺序。"""
        return [command for client in self.clients[target_id] for command in client.commands]

    def downloads_of(self, target_id: str) -> list[str]:
        return [path for client in self.clients[target_id] for path in client.downloads]

    def uploads_of(self, target_id: str) -> list[str]:
        return [path for client in self.clients[target_id] for path in client.uploads]
