"""采集编排测试用的替身：假 SSH 客户端与假连接器。

采集编排只通过 ``TargetConnector`` 拿连接，所以这里用假客户端就能把整个状态机走完，
既不需要真机，也不需要在测试里起 SSH 服务端。与 ``tests/ssh_fakes.py`` 里那个真协议栈的
假服务端分工不同：那一个验证 SSH 客户端本身，这一个验证编排对连接做了什么。

这个假客户端要能表达三件事，否则「恢复成原样」没法断言：

- 目标机器上有哪些文件、内容是什么、权限与属主是多少；
- 提权安装：把暂存目录里的文件装到目标路径，连同权限与属主一起；
- 提权删除：目标路径从这台机器上消失。

它按命令内容分派，不再对任何命令都返回同一段输出：编排现在会 ``stat`` 目标路径、
``mkdir -p`` 暂存目录、``rm -rf`` 清理，返回同一段输出会让这些判断全部失真。
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from system_analyzer.collection.environment import ENVIRONMENT_COMMAND
from system_analyzer.collection.lightdm import (
    AUTOLOGIN_FRAGMENT,
    LIGHTDM_CONFIG,
    LIGHTDM_STATE_COMMAND,
    SESSIONS_COMMAND,
)
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.probe import SYSTEM_INFO_COMMAND

PROBE_STDOUT_TEMPLATE = "hostname={hostname}\nos_release=UOS 25\nkernel_version=6.1.0\n"

ENVIRONMENT_STDOUT = """systemd_version=systemd 255 (255.2-4deepin84)
cgroup_filesystem=cgroup2fs
cgroup_controllers=cpuset cpu io memory hugetlb pids rdma misc
default_cpu_accounting=yes
default_memory_accounting=yes
default_io_accounting=no
default_tasks_accounting=yes
"""
"""能力检查的固定输出：cgroup v2、能提权、只差 IO 记账。与验证机的实测结果一致。"""

SUDO_BANNER = "验证成功\n"
"""UOS 的 sudo 验证成功后打到 stdout 的那一行。假客户端照样打，逼着实现不去解析提权命令的输出。"""

DEFAULT_MODE = "644"
DEFAULT_OWNER = (0, 0)


class LightDmFakeState:
    """一台假目标机器上的 LightDM 与会话状态，跨连接共享。

    采集会在多个阶段分别建立连接（In Progress 装片段并重启，Post 删片段），
    「重启过几次」「重启后有没有建立桌面会话」必须跨连接保留，否则没法断言
    「恢复阶段没有再次重启 LightDM」。
    """

    def __init__(self) -> None:
        self.lightdm_active = True
        """LightDM 是否在运行。为假时重启前的状态检查就会失败。"""
        self.sessions: list[dict[str, str]] = [
            {
                "Id": "2",
                "User": "1000",
                "Name": "uos",
                "Seat": "seat0",
                "Type": "x11",
                "Class": "user",
                "Active": "yes",
                "State": "active",
                "Remote": "no",
                "TTY": "tty1",
                "Display": ":0",
            },
            {
                "Id": "677",
                "User": "1000",
                "Name": "uos",
                "Seat": "",
                "Type": "tty",
                "Class": "user",
                "Active": "yes",
                "State": "active",
                "Remote": "yes",
                "TTY": "pts/1",
                "Display": "",
            },
        ]
        """这台机器上的 logind 会话，结构与 ``loginctl show-session`` 的输出一致。"""
        self.desktop_session_ready = True
        """重启 LightDM 后新建的会话是否落在 seat0、处于 active。为假时模拟「登录了但没桌面」。"""
        self.autologin_effective = True
        """临时片段是否真的生效。为假时模拟「自动登录没有发生」。"""
        self.restart_stderr: str | None = None
        """非空时让 ``systemctl restart lightdm`` 以非 0 退出。"""
        self.restarts: list[dict[str, Any]] = []
        """每次重启 LightDM 时记下当时安装的自动登录片段，便于断言「只重启一次」。"""
        self.known_users: set[str] = {"uos"}
        self.session_seq = 2000


class RecordingSshClient:
    """记录命令、上传、下载的假 ``SshClient``，带一个最小的远端文件系统模型。

    ``files`` 是这台目标机器上的文件内容，直接引用调用方给的字典（不复制）：上传因此会
    改到调用方看得见的那一份，「远端文件被恢复成原样」可以直接断言内容。
    """

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        hostname: str,
        lightdm: LightDmFakeState | None = None,
    ) -> None:
        self.files = files
        self.hostname = hostname
        self.modes: dict[str, str] = {}
        self.owners: dict[str, tuple[int, int]] = {}
        self.directories: set[str] = set()
        self.commands: list[str] = []
        self.privileged_commands: list[str] = []
        self.downloads: list[str] = []
        self.uploads: list[str] = []
        self.unprivileged_stat_denied: set[str] = set()
        """普通身份 ``stat`` 读不到状态的路径，模拟父目录不可进入。"""
        self.stat_unavailable: set[str] = set()
        """任何身份都拿不到 ``stat`` 结果的路径，模拟目标机器缺少可用的 ``stat``。"""
        self.lightdm = lightdm or LightDmFakeState()
        self.closed = False

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.commands.append(command)
        return self._run(command, privileged=False)

    async def run_privileged(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.privileged_commands.append(command)
        return self._run(command, privileged=True)

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        self.uploads.append(remote_path)
        self.files[remote_path] = Path(local_path).read_bytes()  # noqa: ASYNC240  测试里只读小文件
        self.modes.setdefault(remote_path, "600")

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        if remote_path not in self.files:
            raise FileNotFoundError(f"目标机器上不存在文件：{remote_path}")
        self.downloads.append(remote_path)
        Path(local_path).write_bytes(self.files[remote_path])  # noqa: ASYNC240  测试里只写小文件

    async def aclose(self) -> None:
        self.closed = True

    # ---- 命令分派 ---------------------------------------------------------

    def _run(self, command: str, *, privileged: bool) -> CommandResult:
        """按命令内容模拟目标机器，返回与真实命令一致的结果。"""
        if command == SYSTEM_INFO_COMMAND:
            return self._result(command, PROBE_STDOUT_TEMPLATE.format(hostname=self.hostname))
        if command == ENVIRONMENT_COMMAND:
            return self._result(command, ENVIRONMENT_STDOUT)
        if command == LIGHTDM_STATE_COMMAND:
            return self._lightdm_state(command)
        if command == SESSIONS_COMMAND:
            return self._result(command, self._sessions_stdout())
        if command.startswith("id -u "):
            return self._user_lookup(command)
        if privileged:
            return self._privileged(command)
        if command.startswith("stat -c"):
            return self._stat(command, privileged=False)
        if command.startswith("cat "):
            return self._cat(command)
        if command.startswith("mv -f "):
            return self._move(command)
        if command.startswith("umask 077 && mkdir -p"):
            self.directories.update(shlex.split(command)[5:])
            return self._result(command)
        if command.startswith("rm -rf "):
            self._remove_tree(shlex.split(command)[2])
            return self._result(command)
        if command.startswith("chmod "):
            _, mode, path = shlex.split(command)
            if path not in self.files:
                return self._failure(command, 1, f"chmod: 无法访问 '{path}'")
            self.modes[path] = mode
            return self._result(command)
        return self._result(command)

    def _privileged(self, command: str) -> CommandResult:
        """提权命令：真机上 stdout 会多一行「验证成功」，这里照样加上。"""
        if command == "true":
            return self._result(command, SUDO_BANNER)
        if command == "systemctl daemon-reload":
            return self._result(command, SUDO_BANNER)
        if command == "systemctl restart lightdm":
            return self._restart_lightdm(command)
        parts = shlex.split(command)
        if parts[:2] == ["sh", "-c"]:
            return self._probe(command, parts[4])
        if parts[:1] == ["stat"]:
            return self._stat(command, privileged=True)
        if parts[:2] == ["test", "-e"]:
            if parts[2] in self.files:
                return self._result(command, SUDO_BANNER)
            return self._failure(command, 1, "")
        if parts[:1] == ["install"]:
            return self._install(command, parts)
        if parts[:2] == ["cp", "-f"]:
            source, target = parts[2], parts[3]
            return self._copy(command, source, target)
        if parts[:2] == ["rm", "-f"]:
            self._forget(parts[2])
            return self._result(command, SUDO_BANNER)
        return self._failure(command, 1, f"假客户端没有模拟这条提权命令：{command}")

    def _stat(self, command: str, *, privileged: bool) -> CommandResult:
        path = shlex.split(command)[3]
        if path not in self.files:
            return self._failure(command, 1, f"stat: 无法获取 '{path}' 的文件状态")
        if path in self.stat_unavailable:
            return self._failure(command, 1, f"stat: 无法获取 '{path}' 的文件状态")
        if not privileged and path in self.unprivileged_stat_denied:
            return self._failure(command, 1, f"stat: 权限不够 '{path}'")
        mode = self.modes.get(path, DEFAULT_MODE)
        uid, gid = self.owners.get(path, DEFAULT_OWNER)
        return self._result(command, f"{mode} {uid} {gid}\n")

    def _cat(self, command: str) -> CommandResult:
        path = shlex.split(command)[1]
        if path not in self.files:
            return self._failure(command, 1, f"cat: {path}: 没有那个文件或目录")
        return self._result(command, self.files[path].decode())

    def _probe(self, command: str, path: str) -> CommandResult:
        """提权探测：一次回答「不存在」或「属性是什么」，模拟 ``stat`` 不可用的空输出。"""
        if path in self.stat_unavailable:
            return self._result(command, SUDO_BANNER + "__sas_unknown__\n")
        if path not in self.files:
            return self._result(command, SUDO_BANNER + "__sas_absent__\n")
        mode = self.modes.get(path, DEFAULT_MODE)
        uid, gid = self.owners.get(path, DEFAULT_OWNER)
        return self._result(command, SUDO_BANNER + f"{mode} {uid} {gid}\n")

    def _move(self, command: str) -> CommandResult:
        """``mv -f <源> <目标>``：同一目录内的原子替换，用于改动记录落盘。"""
        _, _, source, target = shlex.split(command)
        if source not in self.files:
            return self._failure(command, 1, f"mv: 无法获取 '{source}' 的文件状态")
        self.files[target] = self.files.pop(source)
        if source in self.modes:
            self.modes[target] = self.modes.pop(source)
        if source in self.owners:
            self.owners[target] = self.owners.pop(source)
        return self._result(command)

    def _install(self, command: str, parts: list[str]) -> CommandResult:
        """``install -m <mode> -o <uid> -g <gid> <源> <目标>``。"""
        options = dict(zip(parts[1::2], parts[2::2], strict=False))
        source, target = parts[-2], parts[-1]
        if source not in self.files:
            return self._failure(command, 1, f"install: 无法获取 '{source}' 的文件状态")
        self.files[target] = self.files[source]
        self.modes[target] = options.get("-m", DEFAULT_MODE)
        self.owners[target] = (int(options.get("-o", "0")), int(options.get("-g", "0")))
        return self._result(command, SUDO_BANNER)

    def _copy(self, command: str, source: str, target: str) -> CommandResult:
        """``cp -f <源> <目标>``：只写内容，目标文件原有的权限与属主不变。"""
        if source not in self.files:
            return self._failure(command, 1, f"cp: 无法获取 '{source}' 的文件状态")
        self.files[target] = self.files[source]
        self.modes.setdefault(target, DEFAULT_MODE)
        self.owners.setdefault(target, DEFAULT_OWNER)
        return self._result(command, SUDO_BANNER)

    def _lightdm_state(self, command: str) -> CommandResult:
        """``systemctl is-active`` + 主配置校验和/权限 + 片段目录内容。"""
        config = self.files.get(LIGHTDM_CONFIG)
        checksum = hashlib.sha256(config).hexdigest() if config is not None else ""
        mode = self.modes.get(LIGHTDM_CONFIG, DEFAULT_MODE)
        uid, gid = self.owners.get(LIGHTDM_CONFIG, DEFAULT_OWNER)
        prefix = AUTOLOGIN_FRAGMENT.rsplit("/", 1)[0] + "/"
        fragments = sorted(path[len(prefix) :] for path in self.files if path.startswith(prefix))
        stdout = (
            f"lightdm_active={'active' if self.lightdm.lightdm_active else 'inactive'}\n"
            f"lightdm_conf_sha256={checksum}\n"
            f"lightdm_conf_mode={mode} {uid} {gid}\n"
            f"fragment_dir={' '.join(fragments)}\n"
        )
        return self._result(command, stdout)

    def _sessions_stdout(self) -> str:
        """按 ``SESSIONS_COMMAND`` 的形状输出会话：``session=<id>`` 加若干 ``Key=Value``。"""
        lines: list[str] = []
        for session in self.lightdm.sessions:
            lines.append(f"session={session.get('Id', '')}")
            for key in (
                "Id",
                "Name",
                "Seat",
                "Type",
                "Class",
                "Active",
                "State",
                "Remote",
                "TTY",
                "Display",
            ):
                lines.append(f"{key}={session.get(key, '')}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _user_lookup(self, command: str) -> CommandResult:
        user = shlex.split(command)[2]
        if user not in self.lightdm.known_users:
            return self._failure(command, 1, f"id: {user}：无此用户")
        return self._result(command, "1000\n")

    def _restart_lightdm(self, command: str) -> CommandResult:
        """重启 LightDM：按已安装的临时片段决定是否建立桌面会话。"""
        fragment = self.files.get(AUTOLOGIN_FRAGMENT, b"").decode()
        user = next(
            (
                line.partition("=")[2].strip()
                for line in fragment.splitlines()
                if line.startswith("autologin-user=")
            ),
            None,
        )
        self.lightdm.restarts.append({"user": user, "fragment": fragment})
        if self.lightdm.restart_stderr is not None:
            return self._failure(command, 1, self.lightdm.restart_stderr)
        if self.lightdm.autologin_effective and user:
            self.lightdm.session_seq += 1
            seat = "seat0" if self.lightdm.desktop_session_ready else ""
            self.lightdm.sessions.append(
                {
                    "Id": str(self.lightdm.session_seq),
                    "User": "1000",
                    "Name": user,
                    "Seat": seat,
                    "Type": "x11" if self.lightdm.desktop_session_ready else "tty",
                    "Class": "user",
                    "Active": "yes",
                    "State": "active",
                    "Remote": "no",
                    "TTY": "tty1",
                    "Display": ":0" if seat else "",
                }
            )
        return self._result(command, SUDO_BANNER)

    def _remove_tree(self, directory: str) -> None:
        prefix = directory.rstrip("/") + "/"
        for path in [item for item in self.files if item.startswith(prefix)]:
            self._forget(path)
        self.directories = {item for item in self.directories if not item.startswith(prefix)}
        self.directories.discard(directory)

    def _forget(self, path: str) -> None:
        self.files.pop(path, None)
        self.modes.pop(path, None)
        self.owners.pop(path, None)

    def _result(self, command: str, stdout: str = "") -> CommandResult:
        return CommandResult(command=command, stdout=stdout, stderr="", exit_status=0)

    def _failure(self, command: str, status: int, stderr: str) -> CommandResult:
        return CommandResult(command=command, stdout="", stderr=stderr, exit_status=status)


class FakeConnector:
    """按目标机器 id 返回各自的假客户端，并记录每台机器被连了几次。"""

    def __init__(self, machines: dict[str, dict[str, bytes]]) -> None:
        self.files = {target_id: dict(files) for target_id, files in machines.items()}
        self.clients: dict[str, list[RecordingSshClient]] = {key: [] for key in machines}
        self.connected: list[str] = []
        self.lightdm_state: dict[str, LightDmFakeState] = {
            key: LightDmFakeState() for key in machines
        }

    @asynccontextmanager
    async def connect(self, target_id: str) -> AsyncIterator[RecordingSshClient]:
        self.connected.append(target_id)
        client = RecordingSshClient(
            self.files[target_id], hostname=target_id, lightdm=self.lightdm_state[target_id]
        )
        self.clients[target_id].append(client)
        try:
            yield client
        finally:
            await client.aclose()

    @property
    def lightdm_restarts(self) -> list[dict[str, Any]]:
        """所有目标机器上的 LightDM 重启记录，单机测试直接读它。"""
        return [item for state in self.lightdm_state.values() for item in state.restarts]

    @property
    def autologin_effective(self) -> bool:
        return self._only_lightdm().autologin_effective

    @autologin_effective.setter
    def autologin_effective(self, value: bool) -> None:
        self._only_lightdm().autologin_effective = value

    @property
    def desktop_session_ready(self) -> bool:
        return self._only_lightdm().desktop_session_ready

    @desktop_session_ready.setter
    def desktop_session_ready(self, value: bool) -> None:
        self._only_lightdm().desktop_session_ready = value

    @property
    def lightdm_restart_stderr(self) -> str | None:
        return self._only_lightdm().restart_stderr

    @lightdm_restart_stderr.setter
    def lightdm_restart_stderr(self, value: str | None) -> None:
        self._only_lightdm().restart_stderr = value

    @property
    def known_users(self) -> set[str]:
        return self._only_lightdm().known_users

    @known_users.setter
    def known_users(self, value: set[str]) -> None:
        self._only_lightdm().known_users = value

    def _only_lightdm(self) -> LightDmFakeState:
        if len(self.lightdm_state) != 1:
            raise AssertionError("单机属性的便捷访问只适用于只有一个目标的连接器")
        return next(iter(self.lightdm_state.values()))

    def commands_of(self, target_id: str) -> list[str]:
        """这台机器上执行过的全部普通命令，按发生顺序。"""
        return [command for client in self.clients[target_id] for command in client.commands]

    def privileged_commands_of(self, target_id: str) -> list[str]:
        """这台机器上执行过的全部提权命令，按发生顺序。"""
        return [
            command for client in self.clients[target_id] for command in client.privileged_commands
        ]

    def downloads_of(self, target_id: str) -> list[str]:
        return [path for client in self.clients[target_id] for path in client.downloads]

    def uploads_of(self, target_id: str) -> list[str]:
        return [path for client in self.clients[target_id] for path in client.uploads]

    def modes_of(self, target_id: str) -> dict[str, str]:
        """这台机器上最后落成的文件权限。跨多次连接合并，后写的覆盖先写的。"""
        merged: dict[str, str] = {}
        for client in self.clients[target_id]:
            merged.update(client.modes)
        return merged
