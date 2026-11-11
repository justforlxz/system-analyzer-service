"""连接测试的解析与命令拼接（不需要网络）。"""

from __future__ import annotations

import pytest

from ssh_fakes import FakeSshClient
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import RemoteProbeError, SshConnectError
from system_analyzer.remote.probe import (
    SYSTEM_INFO_COMMAND,
    SystemInfo,
    parse_system_info,
    probe_system_info,
)

UOS_OUTPUT = """hostname=uos-PC
os_release=UOS Desktop 25 Professional
kernel_version=6.6.0-amd64-desktop
architecture=x86_64
cpu_count=16
memory_total_bytes=16732954624
boot_id=3f0b3e0e-2a55-4e1e-9d3f-6f0f0e5a1b2c
"""


def test_parses_all_fields() -> None:
    info = parse_system_info(UOS_OUTPUT)

    assert info == SystemInfo(
        hostname="uos-PC",
        os_release="UOS Desktop 25 Professional",
        kernel_version="6.6.0-amd64-desktop",
        architecture="x86_64",
        cpu_count=16,
        memory_total_bytes=16732954624,
        boot_id="3f0b3e0e-2a55-4e1e-9d3f-6f0f0e5a1b2c",
    )


def test_values_containing_equals_are_kept_whole() -> None:
    info = parse_system_info("hostname=h\nos_release=UOS=a=b\n")

    assert info.os_release == "UOS=a=b"


def test_missing_fields_stay_null_instead_of_zero() -> None:
    """取不到就是 ``None``，不能写成 0：缺失与真实的 0 是两回事。"""
    info = parse_system_info("hostname=h\ncpu_count=\nmemory_total_bytes=\n")

    assert info.hostname == "h"
    assert info.cpu_count is None
    assert info.memory_total_bytes is None
    assert info.os_release is None


def test_unparsable_numbers_stay_null() -> None:
    info = parse_system_info("hostname=h\ncpu_count=many\n")

    assert info.cpu_count is None


def test_unknown_keys_are_ignored() -> None:
    info = parse_system_info("hostname=h\nfuture_field=whatever\n")

    assert info.hostname == "h"


def test_missing_hostname_is_an_error() -> None:
    """连上了但拿不到 hostname，说明这次连接测试没有结论，不能当成成功。"""
    with pytest.raises(RemoteProbeError) as caught:
        parse_system_info("cpu_count=16\n")

    assert "hostname" in str(caught.value)


def test_empty_output_is_an_error() -> None:
    with pytest.raises(RemoteProbeError):
        parse_system_info("")


def test_command_reads_only_system_files() -> None:
    """连接测试只做只读操作：命令里不出现写文件、装包、改配置的动作。"""
    for forbidden in ("sudo", "tee ", "> /", "apt ", "systemctl", "mkdir", "rm "):
        assert forbidden not in SYSTEM_INFO_COMMAND


def test_command_covers_every_reported_field() -> None:
    for field_name in (
        "hostname",
        "os_release",
        "kernel_version",
        "architecture",
        "cpu_count",
        "memory_total_bytes",
        "boot_id",
    ):
        assert field_name in SYSTEM_INFO_COMMAND


def test_as_dict_keeps_all_fields() -> None:
    info = parse_system_info(UOS_OUTPUT)

    assert set(info.as_dict()) == {
        "hostname",
        "os_release",
        "kernel_version",
        "architecture",
        "cpu_count",
        "memory_total_bytes",
        "boot_id",
    }


class ScriptedClient(FakeSshClient):
    """按固定脚本回复的客户端：把命令记下来，返回预设的 stdout。"""

    def __init__(self, stdout: str) -> None:
        super().__init__()
        self.stdout = stdout

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.commands.append(command)
        return CommandResult(command=command, stdout=self.stdout, stderr="", exit_status=0)


async def test_probe_runs_the_script_once_and_parses_it() -> None:
    client = ScriptedClient(UOS_OUTPUT)

    info = await probe_system_info(client)

    assert client.commands == [SYSTEM_INFO_COMMAND]
    assert info.hostname == "uos-PC"


async def test_probe_does_not_swallow_ssh_errors() -> None:
    """SSH 层失败必须冒出来，不能被包成「返回空结果」。"""
    client = FakeSshClient(run_error=SshConnectError("连不上"))

    with pytest.raises(SshConnectError):
        await probe_system_info(client)
