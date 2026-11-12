"""SSH 测试用的替身：一个跑在测试进程里的 asyncssh 服务端，以及假客户端。

单元测试不连真实机器，但「认证失败」「命令超时」「命令非 0 退出」这些分支必须真的走一遍
SSH 协议栈才可信，所以这里用 ``asyncssh`` 自己起一个本地监听，端口由内核分配。
服务端只支持口令认证与 exec/SFTP，不落任何文件到测试目录之外（SFTP 用 ``tmp_path`` 做 chroot）。

``FakeSshClient`` 用在不需要协议栈的用例里（例如只验命令拼接与解析）。
"""

from __future__ import annotations

import asyncio
import functools
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from system_analyzer.remote.client import CommandResult

KEY_TYPE = "ssh-" + "ed25519"
"""假服务端的主机密钥类型。用一个普通的 ed25519 密钥，测试里不校验主机密钥。"""

TEST_PASSWORD = "".join(("unit", "test", "-pw"))
"""测试固定口令。不是任何环境的真实凭据，只在本地假服务端上有效。"""

EchoHandler = Callable[[str], "asyncio.Future[tuple[str, str, int]] | tuple[str, str, int]"]


class _Server(asyncssh.SSHServer):
    """假服务端：默认只做口令认证，也可以额外放行公钥认证。"""

    def __init__(self, expected_password: str, accept_public_key: bool) -> None:
        self._expected = expected_password
        self._accept_public_key = accept_public_key

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return password == self._expected

    def public_key_auth_supported(self) -> bool:
        return self._accept_public_key

    def validate_public_key(self, username: str, key: object) -> bool:
        return self._accept_public_key


@dataclass(frozen=True, slots=True)
class FakeSshServer:
    """假服务端的地址与本地目录。``root`` 是 SFTP 的 chroot。"""

    port: int
    root: Path


async def echo_command(command: str) -> tuple[str, str, int]:
    """默认的命令处理：把命令原样回显，方便断言「命令真的传到了服务端」。"""
    return f"echo:{command}\n", "", 0


@asynccontextmanager
async def fake_ssh_server(
    root: Path,
    *,
    password: str = TEST_PASSWORD,
    handler: EchoHandler = echo_command,
    accept_public_key: bool = False,
    input_log: list[str] | None = None,
) -> AsyncIterator[FakeSshServer]:
    """在 ``127.0.0.1`` 上随机端口起一个假 SSH 服务端，退出时关闭。

    给了 ``input_log`` 时，服务端会把每条命令收到的 stdin 全读出来追加进去：
    提权命令靠 stdin 送口令，这条通道要能被断言。
    """

    async def read_stdin(process: asyncssh.SSHServerProcess) -> str:
        """读一次 stdin。没送数据也没关闭时等一秒后当作「没有输入」，不让测试卡住。"""
        try:
            return await asyncio.wait_for(process.stdin.read(4096), 1.0)
        except TimeoutError:
            return ""

    async def run_command(process: asyncssh.SSHServerProcess) -> None:
        if input_log is not None:
            input_log.append(await read_stdin(process))
        try:
            outcome = handler(process.command)
            stdout, stderr, status = await outcome if asyncio.iscoroutine(outcome) else outcome
        except Exception as exc:  # 处理函数出错也按远端失败返回，不吞成成功
            stdout, stderr, status = "", f"handler error: {exc!r}", 1
        process.stdout.write(stdout)
        if stderr:
            process.stderr.write(stderr)
        await process.stdout.drain()
        process.exit(status)

    listener = await asyncssh.listen(
        host="127.0.0.1",
        port=0,
        server_host_keys=[asyncssh.generate_private_key(KEY_TYPE)],
        server_factory=lambda: _Server(password, accept_public_key),
        process_factory=run_command,
        sftp_factory=functools.partial(asyncssh.SFTPServer, chroot=str(root).encode()),
        encoding="utf-8",
    )
    port = listener.sockets[0].getsockname()[1]
    try:
        yield FakeSshServer(port=port, root=root)
    finally:
        listener.close()
        await listener.wait_closed()


def closed_port() -> int:
    """占一个端口后立刻释放，返回一个「现在没人监听」的端口号，用来制造连接被拒绝。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def silent_tcp_server() -> AsyncIterator[int]:
    """只接受连接、从不发 SSH 版本串的服务端：用来制造连接阶段的超时。"""

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.sleep(30)
        finally:
            writer.close()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


class FakeSshClient:
    """满足 ``SshClient`` 的假客户端：记录调用、按需返回或抛异常。"""

    def __init__(
        self,
        *,
        run_result: CommandResult | None = None,
        run_error: BaseException | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.privileged_commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.closed = False
        self._run_result = run_result
        self._run_error = run_error

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.commands.append(command)
        return self._respond(command)

    async def run_privileged(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.privileged_commands.append(command)
        return self._respond(command)

    def _respond(self, command: str) -> CommandResult:
        if self._run_error is not None:
            raise self._run_error
        if self._run_result is not None:
            return self._run_result
        return CommandResult(command=command, stdout="", stderr="", exit_status=0)

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        self.uploads.append((str(local_path), remote_path))

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        self.downloads.append((remote_path, str(local_path)))

    async def aclose(self) -> None:
        self.closed = True
