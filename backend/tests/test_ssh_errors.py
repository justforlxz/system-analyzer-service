"""三类 SSH 失败的分辨与错误消息的脱敏。"""

from __future__ import annotations

import asyncssh
import pytest

from system_analyzer.remote.client import SshConnectionConfig
from system_analyzer.remote.errors import (
    RemoteError,
    SshAuthError,
    SshCommandError,
    SshConnectError,
    SshError,
    SshTimeoutError,
    classify_ssh_error,
)

PASSWORD = "".join(("unit", "test", "-pw"))


def classify(exc: BaseException, *, timeout: float | None = None) -> SshError:
    return classify_ssh_error(exc, host="10.0.0.1", port=22, where="建立 SSH 连接", timeout=timeout)


def test_permission_denied_is_an_auth_failure() -> None:
    error = classify(asyncssh.PermissionDenied("auth failed"))

    assert isinstance(error, SshAuthError)
    assert "认证失败" in str(error)
    assert "10.0.0.1:22" in str(error)


def test_connection_refused_is_a_connect_failure() -> None:
    error = classify(ConnectionRefusedError(111, "Connection refused"))

    assert isinstance(error, SshConnectError)
    assert not isinstance(error, (SshAuthError, SshTimeoutError))
    assert "建立 SSH 连接失败" in str(error)


def test_dns_failure_is_a_connect_failure() -> None:
    """``socket.gaierror`` 是 ``OSError`` 的子类：域名解析不出来也是连不上，不是超时。"""
    import socket

    error = classify(socket.gaierror(-2, "Name or service not known"))

    assert isinstance(error, SshConnectError)


def test_timeout_is_a_timeout_failure_with_the_limit() -> None:
    error = classify(TimeoutError(), timeout=2.5)

    assert isinstance(error, SshTimeoutError)
    assert "2.5 秒" in str(error)


def test_timeout_carries_no_limit_when_unknown() -> None:
    error = classify(TimeoutError())

    assert isinstance(error, SshTimeoutError)
    assert "限定时间" in str(error)


def test_disconnect_is_a_connect_failure() -> None:
    error = classify(asyncssh.DisconnectError(2, "protocol error"))

    assert isinstance(error, SshConnectError)
    assert "code=2" in str(error)


def test_unknown_error_still_fails_as_ssh_error() -> None:
    """底层库换了异常类型也必须给出 ``SshError``，不能变成「调用成功、其实没连上」。"""
    error = classify(RuntimeError("boom"))

    assert isinstance(error, SshError)
    assert isinstance(error, RemoteError)


def test_already_classified_error_passes_through() -> None:
    original = SshAuthError("已经分好类", host="h", port=1)

    assert classify(original) is original


def test_classified_errors_are_three_distinguishable_types() -> None:
    """三类互不继承：``except`` 一个不会误捕另一个。"""
    assert not issubclass(SshAuthError, (SshConnectError, SshTimeoutError))
    assert not issubclass(SshConnectError, (SshAuthError, SshTimeoutError))
    assert not issubclass(SshTimeoutError, (SshAuthError, SshConnectError))
    assert issubclass(SshCommandError, SshError)


def test_connection_config_repr_hides_the_password() -> None:
    config = SshConnectionConfig(host="10.0.0.1", username="uos", password=PASSWORD)

    assert PASSWORD not in repr(config)
    assert PASSWORD not in str(config)
    assert "password" not in repr(config)


def test_error_message_never_contains_the_password() -> None:
    """错误消息由主机、端口与底层原因拼成，不经过口令。"""
    config = SshConnectionConfig(host="10.0.0.1", username="uos", password=PASSWORD)

    error = classify(
        asyncssh.PermissionDenied("auth failed"),
        timeout=None,
    )

    assert PASSWORD not in str(error)
    assert config.host in str(error)


def test_command_error_keeps_command_and_exit_status() -> None:
    error = SshCommandError(
        "远端命令失败",
        command="false",
        exit_status=1,
        stderr="boom\n",
    )

    assert error.exit_status == 1
    assert error.command == "false"
    assert error.stderr == "boom\n"


@pytest.mark.parametrize(
    "error",
    [
        SshConnectError("a"),
        SshAuthError("b"),
        SshTimeoutError("c"),
        SshCommandError("d", command="x", exit_status=1),
    ],
)
def test_all_ssh_errors_are_remote_errors(error: SshError) -> None:
    """调用方捕获 ``RemoteError`` 一定能拿到全部 SSH 失败。"""
    assert isinstance(error, RemoteError)
