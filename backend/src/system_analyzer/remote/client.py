"""SSH 连接、命令执行与文件传输。

选型：``asyncssh``（``uv.lock`` 里固定版本）。理由与被排除的候选见任务评论；要点是它原生支持
协程、口令认证与 SFTP，不需要为异步代码再造一层线程池。用系统 ``ssh``/``scp`` 配合 ``sshpass``
被排除：口令会出现在进程命令行里，``ps`` 能看到。

本模块只做「连上、跑命令、传文件」，不认识目标机器记录、不读配置、不碰数据库：
``service.py`` 负责把目标机器与凭据接到这里。调用方拿到的永远是上抛的异常，
不存在「返回空结果冒充成功」的分支。

3.4 起 Collector 插件怎么用（``docs/plugin-contracts.md`` 第 4 节要求 ``collect`` 是同步方法）
=========================================================================================

本模块的入口都是协程，插件入口是同步方法，所以中间需要一次「异步转同步」。接缝在
``remote/sync.py``：``SyncSshTargetService`` 在 Worker 的工作线程里用一条短命事件循环调用
``SshTargetService``，与 ``api/sync_store.py`` 处理导入插件的方式一致（同样的 ``NullPool``
与「不能在事件循环线程里调用」的检查）。Collector 插件的写法就是：

```python
def collect(self, request, context):                        # 同步方法，跑在 Worker 的工作线程里
    remote = SyncSshTargetService(settings.database_url)
    info = remote.test_connection(request.target_id)         # 内部在短命事件循环上执行
    remote.download(request.target_id, "/proc/cpuinfo", local_path)
    ...
```

插件模块不导入本模块的任何异步类型，只导入 ``remote.sync`` 的同步入口；这样插件保持同步契约，
异步连接逻辑仍然只有这一份实现。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import asyncssh

from system_analyzer.remote.errors import (
    RemoteError,
    SshCommandError,
    classify_ssh_error,
)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0
STDERR_IN_MESSAGE_LIMIT = 500
"""``SshCommandError`` 消息里最多带多少字符的 stderr：足够定位，又不至于把整段日志塞进异常。"""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """一次远端命令的执行结果。``exit_status`` 为 ``None`` 表示通道提前关闭，没有退出码。"""

    command: str
    stdout: str
    stderr: str
    exit_status: int | None

    @property
    def ok(self) -> bool:
        """退出码为 0 才算成功；没有退出码按失败处理。"""
        return self.exit_status == 0


@dataclass(frozen=True, slots=True)
class SshConnectionConfig:
    """连接一台目标机器所需的全部信息。

    ``password`` 带 ``repr=False``：这个对象的 ``repr``、日志与异常里都不会出现口令。
    这里是口令唯一以明文存在的内存位置，字符串本身来自环境变量，不落库、不进日志。
    """

    host: str
    username: str
    password: str = field(repr=False)
    port: int = 22
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS

    @property
    def endpoint(self) -> str:
        """``host:port``，只用于消息与日志。"""
        return f"{self.host}:{self.port}"


@runtime_checkable
class SshClient(Protocol):
    """SSH 客户端的最小接口。测试用假客户端实现它，不必起真实服务。"""

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，内部用 asyncio.wait_for 落实
        check: bool = True,
    ) -> CommandResult:
        """执行一条远端命令。``check`` 为真时非 0 退出码抛 ``SshCommandError``。"""

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        """把本地文件传到目标机器。"""

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        """把目标机器上的文件取回本地。"""


class AsyncSshClient:
    """基于 ``asyncssh`` 的实现：一条连接上跑命令、开 SFTP 传文件。

    用 ``async with`` 或 ``await client.aclose()`` 释放连接。连接建立与每次操作都会把底层异常
    经 ``classify_ssh_error`` 归到可区分的类型，调用方捕获 ``SshError`` 即可。

    已知限制：1.0 不校验目标机器的主机密钥（``known_hosts=None``），因为目标机器记录里还没有
    主机密钥字段，任务 3.3 之后再加。在没有主机密钥校验的前提下，口令认证仍可能被中间人截获，
    这是 1.0 明确接受的取舍，不是遗漏。
    """

    def __init__(
        self, connection: asyncssh.SSHClientConnection, *, config: SshConnectionConfig
    ) -> None:
        self._connection = connection
        self._config = config

    @classmethod
    async def connect(cls, config: SshConnectionConfig) -> AsyncSshClient:
        """建立连接。超时、认证失败、不可达分别抛出对应的 ``SshError`` 子类。"""
        where = "建立 SSH 连接"
        try:
            connection = await asyncio.wait_for(
                asyncssh.connect(
                    config.host,
                    port=config.port,
                    username=config.username,
                    password=config.password,
                    # 只允许口令认证：不然 Worker 主机上恰好存在的 SSH 私钥（或 ssh-agent 里的
                    # 密钥）会顶替运维配置的凭据连上目标机器，既与「凭据从环境变量取」不符，
                    # 也会让「口令错」被记成「连接成功」。
                    preferred_auth="password",
                    client_keys=[],
                    agent_path=None,
                    known_hosts=None,
                    login_timeout=config.connect_timeout,
                ),
                timeout=config.connect_timeout,
            )
        except Exception as exc:
            raise classify_ssh_error(
                exc,
                host=config.host,
                port=config.port,
                where=where,
                timeout=config.connect_timeout,
            ) from exc
        return cls(connection, config=config)

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，内部用 asyncio.wait_for 落实
        check: bool = True,
    ) -> CommandResult:
        """执行一条远端命令。超时抛 ``SshTimeoutError``，非 0 退出（``check`` 为真）抛
        ``SshCommandError``；连接层面的失败按三类归并。"""
        limit = timeout if timeout is not None else self._config.command_timeout
        where = "执行远端命令"
        try:
            completed = await asyncio.wait_for(self._connection.run(command, check=False), limit)
        except Exception as exc:
            raise classify_ssh_error(
                exc,
                host=self._config.host,
                port=self._config.port,
                where=where,
                timeout=limit,
            ) from exc

        result = CommandResult(
            command=command,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            exit_status=completed.exit_status,
        )
        if check and not result.ok:
            detail = result.stderr.strip().replace("\n", " ")[:STDERR_IN_MESSAGE_LIMIT]
            raise SshCommandError(
                f"远端命令失败：{self._config.endpoint} 退出码 {result.exit_status}"
                f"（{detail or '没有 stderr 输出'}）",
                command=command,
                exit_status=result.exit_status,
                stderr=result.stderr,
            )
        return result

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        """上传本地文件。本地文件不存在属于调用方错误，抛 ``RemoteError``，不混进连接失败。"""
        source = Path(local_path)
        if not source.is_file():  # noqa: ASYNC240  只做一次 stat，微秒级，不值得为它开线程
            raise RemoteError(f"要上传的本地文件不存在：{source}")
        await self._transfer("put", source, remote_path)

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        """下载到本地文件。本地目录不存在属于调用方错误，抛 ``RemoteError``。

        ``follow_symlinks=True``：目标机器上 ``/etc/os-release`` 这类文件常常是指向
        ``/usr/lib/...`` 的符号链接，默认行为会在本地重建符号链接、并因目标在下载目录之外而失败。
        这里要的是文件内容，所以跟随链接下载。

        已知限制：``/proc``、``/sys`` 里的虚拟文件大小为 0，SFTP 读不到内容（会得到一个 0 字节
        的本地文件）。需要这类内容时用 ``run("cat /proc/...")`` 走命令通道，不要用 ``download``。
        """
        target = Path(local_path)
        if not target.parent.is_dir():
            raise RemoteError(f"要写入的本地目录不存在：{target.parent}")
        await self._transfer("get", remote_path, target)

    async def _transfer(
        self,
        operation: str,
        first: Path | str,
        second: Path | str,
    ) -> None:
        where = f"传输文件（{operation}）"
        try:
            async with self._connection.start_sftp_client() as sftp:
                if operation == "put":
                    await sftp.put(str(first), str(second))
                else:
                    await sftp.get(str(first), str(second), follow_symlinks=True)
        except FileNotFoundError as exc:
            raise RemoteError(f"目标机器上不存在文件：{first}") from exc
        except Exception as exc:
            raise classify_ssh_error(
                exc,
                host=self._config.host,
                port=self._config.port,
                where=where,
            ) from exc

    async def aclose(self) -> None:
        """关闭连接。重复调用是安全的。"""
        self._connection.close()
        await self._connection.wait_closed()

    async def __aenter__(self) -> AsyncSshClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()


async def open_ssh_client(config: SshConnectionConfig) -> SshClient:
    """建立一条连接，返回可用的客户端。服务层默认用它，测试可以换成假客户端。"""
    return await AsyncSshClient.connect(config)


async def close_client(client: SshClient) -> None:
    """断开客户端；假客户端可能没有 ``aclose``，那时什么都不做。"""
    close = getattr(client, "aclose", None)
    if close is not None:
        await close()
