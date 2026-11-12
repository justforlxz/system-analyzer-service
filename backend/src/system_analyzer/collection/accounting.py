"""采集期间临时打开 systemd 的资源记账。

任务 3.3（MKACG-776）。记账开关只影响「之后启动的 unit」，所以必须在采集窗口之前打开、
在采集结束后关回去，而且必须留下痕迹：改了什么、备份在哪、怎么回退。

**为什么用临时 drop-in，不用 `systemctl set-property`**

- ``set-property`` 是按 unit 逐个设置，要覆盖「采集窗口内新起的 unit」就得先知道有哪些 unit，
  采集期间还会有新 unit 起来，逐个点名既叫不全也补不上。
- drop-in 是一个文件：改动前后可以逐行 diff，备份与恢复都是文件级操作，路径与校验和可追溯。
- 恢复就是删掉这次新建的那一个文件（或写回原内容），不必逐个 unit 回退，也就不会漏。

代价是每改一次配置要执行一次 ``systemctl daemon-reload``。它只让 systemd 重读配置，
不重启任何服务、不影响已有 unit 的记账数据。

**放在这个文件里的内容**

只打开「当前明确关着」的开关，从不关闭任何东西：目标机器上原本开着的记账不因为一次采集
被改成关闭。读不到状态的开关（``None``）也不动，猜错方向的代价比少开一项大。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.environment import EnvironmentReport
from system_analyzer.collection.restore import read_changes, stage_and_install
from system_analyzer.remote.client import SshClient

ACCOUNTING_DROP_IN = "/etc/systemd/system.conf.d/99-system-analyzer-collection.conf"
"""临时 drop-in 的路径。文件名前缀 99 让它在读取顺序里排在发行版自带片段之后。"""

DROP_IN_BACKUP_NAME = "systemd-accounting.conf"
"""本地备份与远端暂存里用的文件名。"""

DROP_IN_HEADER = (
    "# 由 system-analyzer-service 在采集期间临时创建，采集结束后自动删除。\n"
    "# 目的：让采集窗口内启动的 unit 带上资源记账数据。\n"
)
"""drop-in 的注释。留一行说明来源，避免有人在这台机器上看到它时不知道是谁写的。"""


def drop_in_content(settings: Mapping[str, str]) -> str:
    """drop-in 的正文：``[Manager]`` 段加需要打开的开关。顺序固定，便于逐行 diff。"""
    lines = [DROP_IN_HEADER.rstrip("\n"), "", "[Manager]"]
    lines.extend(f"{name}={value}" for name, value in sorted(settings.items()))
    return "\n".join(lines) + "\n"


def accounting_settings(report: EnvironmentReport) -> dict[str, str]:
    """这次采集需要打开的开关。空字典表示目标机器已经够了，不需要改任何配置。"""
    return report.accounting.disabled()


async def enable_temporary_accounting(
    client: SshClient,
    ctx: PhaseContext,
    *,
    report: EnvironmentReport,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> dict[str, Any]:
    """按能力检查的结论临时打开记账，返回写进记录的那份说明。

    什么都不用打开时同样写一份记录：读记录的人要能分清「已经够了」与「忘了做」。
    """
    settings = accounting_settings(report)
    pending = next((item for item in read_changes(ctx) if item.path == ACCOUNTING_DROP_IN), None)
    record: dict[str, Any] = {
        "path": ACCOUNTING_DROP_IN,
        "settings": dict(settings),
        "applied": False,
        "detail": None,
    }
    if pending is not None:
        # 上一次尝试已经装过这个片段，但进程没来得及把采集记录落盘。改动记录里留着它，
        # 这里不能再按「当前开关已打开」判断成「不需要改动」，否则片段永远不会被删掉。
        record["applied"] = True
        record["detail"] = (
            "上一次尝试已经写入临时 drop-in 但未完成记录保存；本次沿用改动记录，"
            "恢复阶段按「原本不存在」删除该片段"
        )
        ctx.data["accounting"] = record
        return record
    if not settings:
        record["detail"] = (
            "目标机器的记账默认值已经满足采集需要（CPU / 内存 / Tasks 均已开启，"
            "没有需要打开的项），本轮没有改动任何配置"
        )
        ctx.data["accounting"] = record
        return record

    await stage_and_install(
        client,
        ctx,
        ACCOUNTING_DROP_IN,
        drop_in_content(settings).encode(),
        backup_name=DROP_IN_BACKUP_NAME,
        timeout=timeout,
    )
    await client.run_privileged("systemctl daemon-reload", timeout=timeout)
    record["applied"] = True
    record["detail"] = (
        f"写入临时 drop-in 并执行 systemctl daemon-reload，打开 "
        f"{'、'.join(sorted(settings))}；恢复阶段删除该片段并再次 daemon-reload"
    )
    ctx.data["accounting"] = record
    return record
