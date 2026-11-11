"""远程目标机器与 SSH 访问的错误类型。

三类 SSH 失败必须可区分，调用方据此决定「换台机器试」「让用户改凭据」还是「稍后重试」：

| 异常 | 触发条件 | 调用方合理的反应 |
| --- | --- | --- |
| ``SshConnectError`` | 主机不可达、端口拒绝、DNS 失败、连接被重置 | 检查网络与目标机器是否在线 |
| ``SshAuthError`` | 用户名或口令不对、认证方式被拒绝 | 检查凭据，不要重试同一个口令 |
| ``SshTimeoutError`` | 连接超时、命令执行超时 | 稍后重试或延长超时 |

``SshCommandError`` 是第四类：SSH 通道本身正常，远端命令以非 0 退出码结束。它与上面三类
分开，因为「连不上」和「连上了但命令失败」需要完全不同的处理。

所有异常消息里只有主机、端口、阶段与底层原因，不包含口令或私钥内容；口令从进入
``SshConnectionConfig`` 起就带 ``repr=False``，不会随异常、日志或对象打印一起出现。
"""

from __future__ import annotations

import asyncssh


class RemoteError(Exception):
    """远程目标机器相关错误的基类。SSH 之外的问题（凭据未配置、目标不存在）也从它派生。"""


class SshError(RemoteError):
    """SSH 操作失败的基类。``host`` 与 ``port`` 用于日志定位，不含任何凭据。"""

    def __init__(self, message: str, *, host: str | None = None, port: int | None = None) -> None:
        super().__init__(message)
        self.host = host
        self.port = port


class SshConnectError(SshError):
    """连不上目标机器：不可达、端口拒绝、DNS 解析失败、连接被重置或断开。"""


class SshAuthError(SshError):
    """认证失败：用户名或口令不对，或目标机器拒绝了口令认证方式。"""


class SshTimeoutError(SshError):
    """超时：连接在限定时间内没有建立，或远端命令在限定时间内没有结束。"""


class SshCommandError(SshError):
    """远端命令以非 0 退出码结束。

    ``exit_status`` 为 ``None`` 表示进程没有报告退出码（通道提前关闭），此时同样按失败处理：
    状态未知不等于成功。
    """

    def __init__(
        self,
        message: str,
        *,
        command: str,
        exit_status: int | None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.command = command
        self.exit_status = exit_status
        self.stderr = stderr


class RemoteProbeError(RemoteError):
    """连接成功，但目标机器返回的系统基本信息不完整或无法解析。"""


class TargetNotFound(RemoteError):
    """目标机器记录不存在。"""


class PasswordNotConfigured(RemoteError):
    """目标机器引用的凭据在运行环境里没有配置。"""


def classify_ssh_error(
    exc: BaseException,
    *,
    host: str,
    port: int,
    where: str,
    timeout: float | None = None,
) -> SshError:
    """把底层异常归到三类可区分失败之一。

    ``where`` 是这个动作的中文说明（``建立 SSH 连接``、``执行远端命令``、``传输文件``），
    直接进错误消息，便于读日志的人定位是哪一步失败。``timeout`` 只在超时分支出现在消息里。

    不在已知分类里的异常归到 ``SshError`` 基类而不是原样上抛：调用方只要捕获 ``SshError``
    就一定能拿到失败信号，不会因为底层库换了异常类型就把失败当成成功。
    """
    if isinstance(exc, SshError):
        return exc
    if isinstance(exc, TimeoutError):
        limited = f"{timeout:g} 秒内没有完成" if timeout is not None else "没有在限定时间内完成"
        return SshTimeoutError(
            f"{where}超时：{host}:{port} {limited}",
            host=host,
            port=port,
        )
    if isinstance(exc, asyncssh.PermissionDenied):
        return SshAuthError(
            f"{where}认证失败：{host}:{port} 拒绝了用户名或口令"
            f"（目标机器不允许口令认证时也会走到这里）",
            host=host,
            port=port,
        )
    if isinstance(exc, OSError):
        return SshConnectError(
            f"{where}失败：{host}:{port} 不可达（{type(exc).__name__}: {exc}）",
            host=host,
            port=port,
        )
    if isinstance(exc, (asyncssh.ChannelOpenError, asyncssh.ConnectionLost)):
        return SshConnectError(
            f"{where}失败：{host}:{port} 的连接已断开（{exc}）",
            host=host,
            port=port,
        )
    if isinstance(exc, asyncssh.DisconnectError):
        return SshConnectError(
            f"{where}失败：{host}:{port} 被远端断开（code={exc.code}，{exc.reason}）",
            host=host,
            port=port,
        )
    if isinstance(exc, asyncssh.Error):
        return SshError(
            f"{where}失败：{host}:{port}（{type(exc).__name__}: {exc}）",
            host=host,
            port=port,
        )
    return SshError(
        f"{where}失败：{host}:{port}（{type(exc).__name__}: {exc}）",
        host=host,
        port=port,
    )
