"""SSH 连接的实现测试：真跑一遍协议栈（本地假服务端），不连任何真实机器。

覆盖：口令认证成功与失败、命令正常/非 0 退出/超时、SFTP 上传下载、
连接被拒绝、连接超时——后两者是验收标准要求的三类失败中的两类，认证失败是第三类。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ssh_fakes import (
    TEST_PASSWORD,
    FakeSshServer,
    closed_port,
    fake_ssh_server,
    silent_tcp_server,
)
from system_analyzer.remote.client import AsyncSshClient, SshConnectionConfig
from system_analyzer.remote.errors import (
    RemoteError,
    SshAuthError,
    SshCommandError,
    SshConnectError,
    SshTimeoutError,
)


def config(port: int, *, password: str = TEST_PASSWORD, command_timeout: float = 5.0):
    return SshConnectionConfig(
        host="127.0.0.1",
        port=port,
        username="uos",
        password=password,
        connect_timeout=5.0,
        command_timeout=command_timeout,
    )


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------


async def test_run_returns_output_and_exit_status(tmp_path: Path) -> None:
    async with fake_ssh_server(tmp_path) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            result = await client.run("hostname")

    assert result.stdout == "echo:hostname\n"
    assert result.exit_status == 0
    assert result.ok is True
    assert result.command == "hostname"


async def test_non_zero_exit_raises_command_error_by_default(tmp_path: Path) -> None:
    async def failing(command: str) -> tuple[str, str, int]:
        return "", "没法执行\n", 3

    async with fake_ssh_server(tmp_path, handler=failing) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            with pytest.raises(SshCommandError) as caught:
                await client.run("false")

    assert caught.value.exit_status == 3
    assert "没法执行" in str(caught.value)


async def test_non_zero_exit_can_be_inspected_without_raising(tmp_path: Path) -> None:
    async def failing(command: str) -> tuple[str, str, int]:
        return "", "stderr", 7

    async with fake_ssh_server(tmp_path, handler=failing) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            result = await client.run("false", check=False)

    assert result.exit_status == 7
    assert result.ok is False


async def test_command_timeout_is_a_timeout_error(tmp_path: Path) -> None:
    async def slow(command: str) -> tuple[str, str, int]:
        await asyncio.sleep(10)
        return "", "", 0

    async with fake_ssh_server(tmp_path, handler=slow) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            with pytest.raises(SshTimeoutError) as caught:
                await client.run("slow", timeout=0.2)

    assert "执行远端命令超时" in str(caught.value)


# ---------------------------------------------------------------------------
# 文件传输
# ---------------------------------------------------------------------------


async def test_upload_puts_a_file_on_the_target(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    local = tmp_path / "local.txt"
    local.write_text("payload")

    async with fake_ssh_server(remote_root) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            await client.upload(local, "/uploaded.txt")

    assert (remote_root / "uploaded.txt").read_text() == "payload"


async def test_download_gets_a_file_from_the_target(tmp_path: Path) -> None:
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    (remote_root / "remote.txt").write_text("from-target")
    local = tmp_path / "local.txt"

    async with fake_ssh_server(remote_root) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            await client.download("/remote.txt", local)

    assert local.read_text() == "from-target"


async def test_download_follows_a_remote_symlink(tmp_path: Path) -> None:
    """目标机器上 /etc/os-release 这类符号链接要能取到内容，而不是在本地重建链接。"""
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    (remote_root / "real.txt").write_text("followed")
    (remote_root / "link.txt").symlink_to("real.txt")
    local = tmp_path / "local.txt"

    async with fake_ssh_server(remote_root) as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            await client.download("/link.txt", local)

    assert local.read_text() == "followed"
    assert not local.is_symlink()


async def test_upload_rejects_a_missing_local_file(tmp_path: Path) -> None:
    """本地文件不存在是调用方错误，不能被误报成「连不上」。"""
    async with fake_ssh_server(tmp_path / "remote") as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            with pytest.raises(RemoteError) as caught:
                await client.upload(tmp_path / "missing.txt", "/x.txt")

    assert not isinstance(caught.value, SshConnectError)


async def test_download_rejects_a_missing_local_directory(tmp_path: Path) -> None:
    async with fake_ssh_server(tmp_path / "remote") as server:
        async with await AsyncSshClient.connect(config(server.port)) as client:
            with pytest.raises(RemoteError):
                await client.download("/remote.txt", tmp_path / "no-such-dir" / "x.txt")


# ---------------------------------------------------------------------------
# 三类失败
# ---------------------------------------------------------------------------


async def test_wrong_password_is_an_auth_failure(tmp_path: Path) -> None:
    async with fake_ssh_server(tmp_path) as server:
        with pytest.raises(SshAuthError) as caught:
            await AsyncSshClient.connect(config(server.port, password="wrong-password"))

    assert "认证失败" in str(caught.value)
    assert "wrong-password" not in str(caught.value)


async def test_auth_uses_the_configured_password_not_ambient_keys(tmp_path: Path) -> None:
    """只允许口令认证：Worker 主机上的私钥不能顶替运维配置的凭据。

    假服务端放行公钥认证、拒绝口令；如果客户端还会去试本机默认私钥，这里就会连上，
    于是把「口令错」误记成「连接成功」。
    """
    async with fake_ssh_server(tmp_path, accept_public_key=True) as server:
        with pytest.raises(SshAuthError):
            await AsyncSshClient.connect(
                config(server.port, password="".join(("unit", "test", "-wrong-pw")))
            )


async def test_closed_port_is_a_connect_failure() -> None:
    with pytest.raises(SshConnectError) as caught:
        await AsyncSshClient.connect(config(closed_port()))

    assert not isinstance(caught.value, (SshAuthError, SshTimeoutError))


async def test_silent_server_is_a_connect_timeout() -> None:
    """对端接受连接但从不发 SSH 版本串：连接阶段必须超时，而不是一直挂着。"""
    async with silent_tcp_server() as port:
        slow = SshConnectionConfig(
            host="127.0.0.1",
            port=port,
            username="uos",
            password=TEST_PASSWORD,
            connect_timeout=0.3,
        )
        with pytest.raises(SshTimeoutError) as caught:
            await AsyncSshClient.connect(slow)

    assert "建立 SSH 连接超时" in str(caught.value)


# ---------------------------------------------------------------------------
# 口令不泄
# ---------------------------------------------------------------------------


async def test_password_never_appears_in_errors_or_connection_repr(
    tmp_path: Path,
) -> None:
    secret = "".join(("unit", "test", "-wrong-pw"))
    async with fake_ssh_server(tmp_path, password="another-one") as server:
        connection = config(server.port, password=secret)
        with pytest.raises(SshAuthError) as caught:
            await AsyncSshClient.connect(connection)

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in repr(connection)


async def test_closing_twice_is_safe(tmp_path: Path) -> None:
    async with fake_ssh_server(tmp_path) as server:
        client = await AsyncSshClient.connect(config(server.port))
        await client.aclose()
        await client.aclose()


async def test_server_fixture_exposes_its_port(tmp_path: Path) -> None:
    async with fake_ssh_server(tmp_path) as server:
        assert isinstance(server, FakeSshServer)
        assert server.port > 0
