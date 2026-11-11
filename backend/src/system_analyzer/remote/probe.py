"""连接测试：在目标机器上取一次系统基本信息。

一次连接测试只做只读操作：读 ``/etc/os-release``、``uname``、``/proc`` 与内核暴露的启动标识，
不改任何配置、不装任何东西、不写临时文件。命令与解析分开，解析是纯函数，可以用固定输出测。

字段与 ``docs/data-model.md`` 第 5.1 节 Machine 实体同名（``hostname``、``os_release``、
``kernel_version``、``architecture``），3.4 组装采集数据时直接取用；``cpu_count``、
``memory_total_bytes`` 与 ``boot_id`` 供后续采集与 ``docs/time-base.md`` 的时间基准使用。

取不到的值一律留空（``None``），不用默认值或 0 顶替：``docs/data-model.md`` 的缺失语义是
「未采集到就是 null」，写成 0 会让下游把「没读到」当成「真的是 0」。
"""

from __future__ import annotations

from dataclasses import dataclass

from system_analyzer.remote.client import SshClient
from system_analyzer.remote.errors import RemoteProbeError


def _print(name: str, script: str) -> str:
    return f"printf '%s=%s\\n' {name} \"$({script} 2>/dev/null)\""


SYSTEM_INFO_COMMAND = " ; ".join(
    (
        _print("hostname", "hostname"),
        _print("os_release", ". /etc/os-release && printf '%s' \"$PRETTY_NAME\""),
        _print("kernel_version", "uname -r"),
        _print("architecture", "uname -m"),
        _print("cpu_count", "nproc"),
        _print("memory_total_bytes", "awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo"),
        _print("boot_id", "cat /proc/sys/kernel/random/boot_id"),
    )
)
"""取系统基本信息的 shell 脚本。

合并成一条 ``ssh`` 命令而不是七条：一次连接测试只开一个会话，往返次数从七次降到一次。
每项都用 ``$(...) 2>/dev/null`` 包住，取不到时输出空值而不是让整条命令失败。
"""


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """目标机器在一次连接测试里报出的系统基本信息。取不到的项为 ``None``。"""

    hostname: str
    os_release: str | None = None
    kernel_version: str | None = None
    architecture: str | None = None
    cpu_count: int | None = None
    memory_total_bytes: int | None = None
    boot_id: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        """按字段顺序摊平成字典，用于写进日志、任务结果或 HTTP 响应。"""
        return {
            "hostname": self.hostname,
            "os_release": self.os_release,
            "kernel_version": self.kernel_version,
            "architecture": self.architecture,
            "cpu_count": self.cpu_count,
            "memory_total_bytes": self.memory_total_bytes,
            "boot_id": self.boot_id,
        }


def parse_system_info(stdout: str) -> SystemInfo:
    """解析 ``SYSTEM_INFO_COMMAND`` 的输出。``hostname`` 缺失或为空即视为失败。

    只认 ``key=value`` 形式的行，不认识的键忽略（老版本脚本多打的字段不会让解析炸掉）。
    值里的 ``=`` 属于值本身，只在第一个 ``=`` 处切分。
    """
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip()

    hostname = values.get("hostname", "")
    if not hostname:
        raise RemoteProbeError(
            "目标机器没有返回 hostname，无法确认连接测试结果；"
            f"实际输出：{stdout.strip()[:200] or '（空）'}"
        )

    return SystemInfo(
        hostname=hostname,
        os_release=_text(values.get("os_release")),
        kernel_version=_text(values.get("kernel_version")),
        architecture=_text(values.get("architecture")),
        cpu_count=_integer(values.get("cpu_count")),
        memory_total_bytes=_integer(values.get("memory_total_bytes")),
        boot_id=_text(values.get("boot_id")),
    )


def _text(value: str | None) -> str | None:
    """空字符串按「没取到」处理，与 ``docs/data-model.md`` 的缺失语义一致。"""
    return value or None


def _integer(value: str | None) -> int | None:
    """解析不了的数值按「没取到」处理，不猜一个值出来。"""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def probe_system_info(
    client: SshClient,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> SystemInfo:
    """对一条已经建立的连接做连接测试，返回系统基本信息。

    SSH 层面的失败（连接、认证、超时、命令失败）原样上抛 ``SshError`` 子类，不吞、不兜底。
    """
    result = await client.run(SYSTEM_INFO_COMMAND, timeout=timeout)
    return parse_system_info(result.stdout)
