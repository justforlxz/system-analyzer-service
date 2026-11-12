"""采集前的能力检查：systemd、cgroup 与提权通道。

任务 3.3（MKACG-776）。采集要动目标机器的配置（临时 systemd drop-in、LightDM 片段），
所以动手之前先确认三件事：目标机器有没有可用的 systemd、cgroup 是不是统一层级（v2）、
SSH 用户能不能用登录口令执行 ``sudo``。任何一条不满足都用可读的理由让阶段失败，
不继续往下改远端环境。

检查本身只读：一条合并的 ``run()`` 命令取回全部字段；唯一的副作用是提权探测
（``sudo -S -p '' true``，不改任何配置）。

字段与解析规则同 ``docs/collection-environment-prep.md`` 第 2 节。解析是纯函数，
用固定输出测，不需要目标机器。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from system_analyzer.remote.client import SshClient
from system_analyzer.remote.errors import RemoteError, SshCommandError

CGROUP_V2_FILESYSTEM = "cgroup2fs"
"""cgroup v2 统一层级的文件系统标识（``stat -c %T -f /sys/fs/cgroup`` 的输出）。"""


def _print(name: str, script: str) -> str:
    return f"printf '%s=%s\\n' {name} \"$({script} 2>/dev/null)\""


ENVIRONMENT_COMMAND = " ; ".join(
    (
        _print("systemd_version", "systemctl --version | head -1"),
        _print("cgroup_filesystem", "stat -c %T -f /sys/fs/cgroup"),
        _print("cgroup_controllers", "cat /sys/fs/cgroup/cgroup.controllers"),
        _print("default_cpu_accounting", "systemctl show -p DefaultCPUAccounting --value"),
        _print("default_memory_accounting", "systemctl show -p DefaultMemoryAccounting --value"),
        _print("default_io_accounting", "systemctl show -p DefaultIOAccounting --value"),
        _print("default_tasks_accounting", "systemctl show -p DefaultTasksAccounting --value"),
    )
)
"""取能力检查所需信息的 shell 脚本。

合并成一条命令而不是七条：一次往返，七个 ``systemctl`` 调用都在目标机器本地完成。
每项都用 ``$(...) 2>/dev/null`` 包住，取不到时输出空值而不是让整条命令失败——
「这个字段没取到」与「整台机器连不上」是两件事，前者交给调用方判断。
"""


@dataclass(frozen=True, slots=True)
class AccountingState:
    """systemd 的四个记账默认开关。``None`` 表示这条属性没读到。"""

    cpu: bool | None = None
    memory: bool | None = None
    io: bool | None = None
    tasks: bool | None = None

    def as_dict(self) -> dict[str, bool | None]:
        return {"cpu": self.cpu, "memory": self.memory, "io": self.io, "tasks": self.tasks}

    @classmethod
    def from_mapping(cls, raw: Any) -> AccountingState:
        source = raw if isinstance(raw, dict) else {}
        return cls(
            cpu=_flag(source.get("cpu")),
            memory=_flag(source.get("memory")),
            io=_flag(source.get("io")),
            tasks=_flag(source.get("tasks")),
        )

    def disabled(self) -> dict[str, str]:
        """当前明确关闭的开关，键是 systemd 的配置名，值是应该写进去的值。

        只包含「确实关着」的项：``None``（没读到）不猜，也不在这里打开。
        """
        pairs = (
            ("DefaultCPUAccounting", self.cpu),
            ("DefaultMemoryAccounting", self.memory),
            ("DefaultIOAccounting", self.io),
            ("DefaultTasksAccounting", self.tasks),
        )
        return {name: "yes" for name, value in pairs if value is False}


@dataclass(frozen=True, slots=True)
class EnvironmentReport:
    """目标机器能不能采集、能不能临时改配置的结论。取不到的项为 ``None``。"""

    systemd_version: str | None = None
    cgroup_filesystem: str | None = None
    cgroup_controllers: tuple[str, ...] = ()
    accounting: AccountingState = AccountingState()
    privileged: bool = False

    @property
    def cgroup_version(self) -> str | None:
        """``"2"`` 表示统一层级；文件系统认得但不是 ``cgroup2fs`` 时是 ``"1"``。"""
        if not self.cgroup_filesystem:
            return None
        return "2" if self.cgroup_filesystem == CGROUP_V2_FILESYSTEM else "1"

    def as_dict(self) -> dict[str, Any]:
        """摊平成可 JSON 序列化的字典，随采集记录一起持久化。"""
        return {
            "systemd_version": self.systemd_version,
            "cgroup_filesystem": self.cgroup_filesystem,
            "cgroup_version": self.cgroup_version,
            "cgroup_controllers": list(self.cgroup_controllers),
            "accounting": self.accounting.as_dict(),
            "privileged": self.privileged,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> EnvironmentReport:
        """从记录里读回上一次的结论（重试时 Pre 可能被复用，但 Post 仍要按它决定动作）。"""
        source = raw if isinstance(raw, dict) else {}
        controllers = source.get("cgroup_controllers") or ()
        known = {str(item) for item in controllers if isinstance(item, str)}
        return cls(
            systemd_version=_text(source.get("systemd_version")),
            cgroup_filesystem=_text(source.get("cgroup_filesystem")),
            cgroup_controllers=tuple(sorted(known)),
            accounting=AccountingState.from_mapping(source.get("accounting")),
            privileged=source.get("privileged") is True,
        )

    def problems(self) -> list[str]:
        """不满足的条件，按可读的中文写出来。空列表表示可以继续。"""
        complaints: list[str] = []
        if not self.systemd_version:
            complaints.append(
                "目标机器上没有可用的 systemd：`systemctl --version` 没有输出，"
                "无法采集 systemd 数据"
            )
        if self.cgroup_version != "2":
            shown = self.cgroup_filesystem or "未知"
            complaints.append(
                f"目标机器没有使用 cgroup v2 统一层级（`/sys/fs/cgroup` 的文件系统是 {shown}），"
                "采集依赖 cgroup v2 的 cpu.stat 与 io.stat"
            )
        if not self.privileged:
            complaints.append("目标机器不允许 SSH 用户用登录口令执行 sudo，无法临时修改配置")
        if all(value is None for value in self.accounting.as_dict().values()):
            complaints.append(
                "无法读取 systemd 的默认记账配置"
                "（`systemctl show -p Default*Accounting` 没有输出），不能判断需要临时开启什么"
            )
        return complaints


class CollectionEnvironmentError(RemoteError):
    """目标机器不满足采集前提。消息里是逐条的中文理由，调用方直接放进阶段失败原因。"""


def parse_environment(stdout: str) -> EnvironmentReport:
    """解析 ``ENVIRONMENT_COMMAND`` 的输出。不认识的键忽略，取不到的项留 ``None``。"""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip()

    controllers = tuple(
        sorted({item for item in values.get("cgroup_controllers", "").split() if item})
    )
    systemd_version = _systemd_version(values.get("systemd_version"))
    return EnvironmentReport(
        systemd_version=systemd_version,
        cgroup_filesystem=_text(values.get("cgroup_filesystem")),
        cgroup_controllers=controllers,
        accounting=AccountingState(
            cpu=_flag(values.get("default_cpu_accounting")),
            memory=_flag(values.get("default_memory_accounting")),
            io=_flag(values.get("default_io_accounting")),
            tasks=_flag(values.get("default_tasks_accounting")),
        ),
    )


async def inspect_environment(
    client: SshClient,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> EnvironmentReport:
    """对一条已经建立的连接做能力检查：只读信息一条命令取回，再单独探一次提权。

    连接层面的失败（连不上、超时）原样上抛；提权探测失败只记为「不能提权」，
    因为「这台机器不给 sudo」本身就是要报告的结论，不是基础设施故障。
    """
    result = await client.run(ENVIRONMENT_COMMAND, timeout=timeout)
    return replace(
        parse_environment(result.stdout),
        privileged=await check_privilege(client, timeout=timeout),
    )


async def check_privilege(
    client: SshClient,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run_privileged 落实
) -> bool:
    """SSH 用户能否用登录口令执行 sudo。

    只认「命令以 0 退出」：``sudo`` 拒绝时退出码非 0。连接层面的失败不在这里吞掉，
    否则「机器连不上」会被误报成「这台机器不给 sudo」。
    """
    try:
        await client.run_privileged("true", timeout=timeout)
    except SshCommandError:
        return False
    return True


def ensure_supported(report: EnvironmentReport) -> None:
    """不满足前提时抛 ``CollectionEnvironmentError``，消息里逐条写清原因。"""
    complaints = report.problems()
    if complaints:
        raise CollectionEnvironmentError("；".join(complaints))


def _flag(value: Any) -> bool | None:
    """systemd 的布尔属性只认 ``yes`` / ``no``，其余（含空值）算「没读到」。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "yes":
            return True
        if normalized == "no":
            return False
    return None


def _text(value: Any) -> str | None:
    """空字符串按「没取到」处理，与 ``remote/probe.py`` 的缺失语义一致。"""
    if isinstance(value, str):
        return value or None
    return None


def _systemd_version(value: Any) -> str | None:
    """``systemctl --version`` 的第一行形如 ``systemd 255 (255.2-4deepin84)``，只留 ``255``。"""
    text = _text(value)
    if text is None:
        return None
    parts = text.split()
    for part in parts:
        if part and part[0].isdigit():
            return part
    return text
