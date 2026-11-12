"""采集前提检查与临时记账的纯逻辑：解析、结论、要打开哪些开关。

不连网络：解析用固定输出，记账开关的判断用构造出来的 ``EnvironmentReport``。
「提权探测成功 / 失败」用假客户端走真实代码。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from collection_fakes import ENVIRONMENT_STDOUT
from ssh_fakes import FakeSshClient
from system_analyzer.collection.accounting import (
    ACCOUNTING_DROP_IN,
    accounting_settings,
    drop_in_content,
)
from system_analyzer.collection.environment import (
    ENVIRONMENT_COMMAND,
    AccountingState,
    CollectionEnvironmentError,
    EnvironmentReport,
    check_privilege,
    ensure_supported,
    inspect_environment,
    parse_environment,
)
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import SshCommandError, SshConnectError


def test_parses_every_field_of_the_verification_machine() -> None:
    """验证机（UOS 25 / systemd 255 / cgroup v2）的实测输出能被完整解析。"""
    report = parse_environment(ENVIRONMENT_STDOUT)

    assert report.systemd_version == "255"
    assert report.cgroup_filesystem == "cgroup2fs"
    assert report.cgroup_version == "2"
    assert report.cgroup_controllers == (
        "cpu",
        "cpuset",
        "hugetlb",
        "io",
        "memory",
        "misc",
        "pids",
        "rdma",
    )
    assert report.accounting == AccountingState(cpu=True, memory=True, io=False, tasks=True)


def test_missing_fields_stay_unknown_instead_of_guessed() -> None:
    """取不到就是「没读到」，不能当成「没开启」，也不能当成 0。"""
    report = parse_environment("hostname=uos-PC\n")

    assert report.systemd_version is None
    assert report.cgroup_filesystem is None
    assert report.cgroup_version is None
    assert report.cgroup_controllers == ()
    assert report.accounting.as_dict() == {
        "cpu": None,
        "memory": None,
        "io": None,
        "tasks": None,
    }


def test_unknown_keys_and_flag_spellings() -> None:
    report = parse_environment(
        "systemd_version=systemd 252 (252-1)\n"
        "default_io_accounting=YES\n"
        "default_memory_accounting=no\n"
        "future_field=whatever\n"
    )

    assert report.systemd_version == "252"
    assert report.accounting.io is True
    assert report.accounting.memory is False
    assert report.accounting.cpu is None


def test_only_explicitly_disabled_switches_are_turned_on() -> None:
    """只打开明确关着的开关：已经开着的不动，没读到的也不动。"""
    report = parse_environment(ENVIRONMENT_STDOUT)

    assert accounting_settings(report) == {"DefaultIOAccounting": "yes"}


def test_nothing_to_enable_when_accounting_is_already_on() -> None:
    report = parse_environment(
        "default_cpu_accounting=yes\ndefault_memory_accounting=yes\n"
        "default_io_accounting=yes\ndefault_tasks_accounting=yes\n"
    )

    assert accounting_settings(report) == {}


def test_drop_in_content_is_a_complete_manager_section() -> None:
    content = drop_in_content({"DefaultIOAccounting": "yes", "DefaultCPUAccounting": "yes"})

    assert content.endswith("\n")
    assert "[Manager]" in content
    assert "DefaultCPUAccounting=yes" in content
    assert "DefaultIOAccounting=yes" in content
    # 开关按名字排序，改动前后的 diff 才稳定
    assert content.index("DefaultCPUAccounting") < content.index("DefaultIOAccounting")


def test_report_survives_a_round_trip_through_the_record() -> None:
    report = replace(parse_environment(ENVIRONMENT_STDOUT), privileged=True)
    restored = EnvironmentReport.from_mapping(report.as_dict())

    assert restored == report
    assert restored.problems() == []


def test_supported_machine_has_no_complaint() -> None:
    report = EnvironmentReport(
        systemd_version="255",
        cgroup_filesystem="cgroup2fs",
        cgroup_controllers=("cpu", "io", "memory"),
        accounting=AccountingState(cpu=True, memory=True, io=False, tasks=True),
        privileged=True,
    )

    ensure_supported(report)


def test_unsupported_machine_reports_every_reason_in_readable_words() -> None:
    """不满足时逐条写清原因，而不是只说「环境不支持」。"""
    report = EnvironmentReport(
        systemd_version=None,
        cgroup_filesystem="tmpfs",
        accounting=AccountingState(),
        privileged=False,
    )

    with pytest.raises(CollectionEnvironmentError) as caught:
        ensure_supported(report)

    message = str(caught.value)
    assert "systemd" in message
    assert "cgroup v2" in message
    assert "sudo" in message
    assert "记账" in message
    assert message.count("；") == 3


async def test_privilege_check_accepts_a_working_sudo() -> None:
    client = FakeSshClient()

    assert await check_privilege(client) is True
    assert client.privileged_commands == ["true"]


async def test_privilege_check_reports_a_refusing_sudo() -> None:
    """``sudo`` 拒绝（口令不对、没有授权）时是「不能提权」，不是「连不上」。"""
    refusal = SshCommandError(
        "远端命令失败", command="sudo -S -p '' true", exit_status=1, stderr="需要密码"
    )
    client = FakeSshClient(run_error=refusal)

    assert await check_privilege(client) is False


async def test_privilege_check_does_not_swallow_connection_failures() -> None:
    """连接层面的失败要冒出来，不能被记成「这台机器不给 sudo」。"""
    client = FakeSshClient(run_error=SshConnectError("连不上"))

    with pytest.raises(SshConnectError):
        await check_privilege(client)


async def test_inspect_environment_combines_probe_and_privilege() -> None:
    """一次能力检查：一条只读命令取字段，一条提权命令探 sudo，不多连、不多跑。"""
    client = FakeSshClient(
        run_result=CommandResult(
            command=ENVIRONMENT_COMMAND,
            stdout=ENVIRONMENT_STDOUT,
            stderr="",
            exit_status=0,
        )
    )

    report = await inspect_environment(client)

    assert client.commands == [ENVIRONMENT_COMMAND]
    assert client.privileged_commands == ["true"]
    assert report.privileged is True
    assert report.cgroup_version == "2"


def test_accounting_drop_in_path_sits_beside_distro_snippets() -> None:
    """片段放在 /etc/systemd/system.conf.d 下、以 99 开头，恢复时按同一个路径删掉。"""
    assert ACCOUNTING_DROP_IN.startswith("/etc/systemd/system.conf.d/")
    assert ACCOUNTING_DROP_IN.rsplit("/", 1)[-1].startswith("99-")
