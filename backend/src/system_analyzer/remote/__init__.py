"""远程目标机器管理与 SSH 访问（任务 3.1）。

这是 3.2–3.7 全部远程采集能力的地基，只做四件事：

- 目标机器的增删改查与持久化（``targets.py``，表 ``ssh_targets``）。
- 连接、命令执行、文件传输（``client.py``，基于 ``asyncssh``）。
- 连接测试与系统基本信息（``probe.py``）。
- 三类可区分的失败：连接失败、认证失败、超时（``errors.py``）。

凭据只有引用：``ssh_targets.password_env_var`` 存环境变量名，值由 ``credentials.py`` 从
``Settings``（即环境变量或未提交的 ``backend/.env``）取。库里、日志里、异常消息里都没有明文口令。

模块边界：本包不实现采集插件（3.4 起在 ``collectors/``）、不写采集数据（``storage/``）、
不加 HTTP 路由（4.1 的机器管理界面需要时再接）。它与 ``analyzers/`` 等工作目录不重叠。

3.4 的接缝：插件入口 ``CollectorPlugin.collect`` 是同步方法，本包是异步实现，
中间用 ``sync.SyncSshTargetService`` 桥接（``client.py`` 的模块注释里有完整说明）。
"""

from system_analyzer.remote.client import (
    AsyncSshClient,
    CommandResult,
    SshClient,
    SshConnectionConfig,
    open_ssh_client,
)
from system_analyzer.remote.credentials import PASSWORD_ENV_VAR, EnvironmentPasswordResolver
from system_analyzer.remote.errors import (
    PasswordNotConfigured,
    RemoteError,
    RemoteProbeError,
    SshAuthError,
    SshCommandError,
    SshConnectError,
    SshError,
    SshTimeoutError,
    TargetNotFound,
    classify_ssh_error,
)
from system_analyzer.remote.probe import SystemInfo, parse_system_info, probe_system_info
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.sync import SyncSshTargetService, run_sync
from system_analyzer.remote.targets import SshTarget, SshTargetDraft, SshTargetRepository

__all__ = [
    "PASSWORD_ENV_VAR",
    "AsyncSshClient",
    "CommandResult",
    "EnvironmentPasswordResolver",
    "PasswordNotConfigured",
    "RemoteError",
    "RemoteProbeError",
    "SshAuthError",
    "SshClient",
    "SshCommandError",
    "SshConnectError",
    "SshConnectionConfig",
    "SshError",
    "SshTarget",
    "SshTargetDraft",
    "SshTargetRepository",
    "SshTargetService",
    "SshTimeoutError",
    "SyncSshTargetService",
    "SystemInfo",
    "TargetNotFound",
    "classify_ssh_error",
    "open_ssh_client",
    "parse_system_info",
    "probe_system_info",
    "run_sync",
]
