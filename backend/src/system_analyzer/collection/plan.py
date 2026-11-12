"""三个阶段各自执行哪些步骤，以及步骤之间的边界。

本任务（3.2）只交付编排骨架：阶段的边界、顺序、状态、幂等、超时取消与清理在这里定下来；
每个阶段具体对目标机器做什么属于 3.4（资源采集）、3.5（Journal 采集）、3.6（LightDM 登录）、
3.7（数据回传），它们把动作填进下面这些步骤里，不需要改动编排。

3.3（MKACG-776）把 Pre 的环境检查、备份、临时配置与 Post 的恢复填成了真实实现：

- 「检查目标环境」除了取系统基本信息，还做一次能力检查（systemd、cgroup v2、能否提权），
  不满足时用可读的理由让阶段失败，后面几步不会在没确认过的机器上动手。
- 「备份系统配置」把 ``options.backup_paths`` 里的文件下载到本地备份目录，并记下它们的
  权限与属主——恢复时要连属性一起写回去。
- 「应用临时配置」建远端暂存目录、临时打开资源记账、把能脱离 Worker 手工运行的恢复脚本
  放到目标机器上。
- 「恢复环境」按记录把文件恢复原样（原先存在的写回内容与属性，原先不存在的删掉），
  重新加载 systemd 配置，最后删掉远端暂存目录。

占位实现只剩 In Progress 的三步与 Post 的「停止采样」「收集数据」，归属见上面那段。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from system_analyzer.collection import staging
from system_analyzer.collection.accounting import enable_temporary_accounting
from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.environment import (
    EnvironmentReport,
    ensure_supported,
    inspect_environment,
)
from system_analyzer.collection.restore import (
    BACKUP_DIR_NAME as BACKUP_DIR_NAME,
)
from system_analyzer.collection.restore import (
    FileChange,
    adopt_recorded_changes,
    has_pending_restore,
    local_backup_dir,
    read_changes,
    record_change,
    restore_environment,
    upload_restore_assets,
)
from system_analyzer.collection.states import CollectionPhase
from system_analyzer.remote.probe import probe_system_info

StepAction = Callable[[PhaseContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CollectionStep:
    """一个步骤：名字进操作记录，``run`` 是真正执行的动作。"""

    name: str
    run: StepAction


@dataclass(frozen=True, slots=True)
class PhasePlan:
    """一个阶段要按顺序执行的步骤。"""

    phase: CollectionPhase
    steps: tuple[CollectionStep, ...]


def default_plan() -> dict[CollectionPhase, PhasePlan]:
    """本任务验收用的三阶段计划。

    3.4–3.7 在各自的步骤里填动作；步骤名字、步骤顺序与阶段边界保持不变。
    """
    return {
        CollectionPhase.PRE: PhasePlan(
            phase=CollectionPhase.PRE,
            steps=(
                CollectionStep(name="检查目标环境", run=inspect_target),
                CollectionStep(name="备份系统配置", run=backup_configuration),
                CollectionStep(name="应用临时配置", run=apply_temporary_configuration),
            ),
        ),
        CollectionPhase.IN_PROGRESS: PhasePlan(
            phase=CollectionPhase.IN_PROGRESS,
            steps=(
                CollectionStep(name="启动采样", run=start_sampling),
                CollectionStep(name="触发桌面登录", run=trigger_desktop_login),
                CollectionStep(name="持续采集", run=collect_continuously),
            ),
        ),
        CollectionPhase.POST: PhasePlan(
            phase=CollectionPhase.POST,
            steps=(
                CollectionStep(name="停止采样", run=stop_sampling),
                CollectionStep(name="收集数据", run=gather_collected_data),
                CollectionStep(name="恢复环境", run=restore_configuration),
            ),
        ),
    }


# ---- Pre：环境检查、备份、临时配置 -----------------------------------------


async def inspect_target(ctx: PhaseContext) -> None:
    """连上去取一次系统基本信息，再做一次采集前提检查。

    这一条同时是「这里真的连得上」的证明：连不上、口令错、超时都会抛 ``SshError`` 子类，
    阶段随之失败，后面两个阶段不会在没确认环境的情况下动手。

    结论在失败之前就写进记录：即使这次采集被拒，``ctx.data["environment"]`` 里也留着
    「为什么不行」的实测数据，读记录的人不用再连一次机器。
    """
    async with ctx.connect() as client:
        info = await probe_system_info(client)
        report = await inspect_environment(client)
    ctx.data["system_info"] = info.as_dict()
    ctx.data["environment"] = report.as_dict()
    ensure_supported(report)


async def backup_configuration(ctx: PhaseContext) -> None:
    """把接下来可能要临时修改的远端文件下载到本次采集的临时目录，并记下权限与属主。

    要备份哪些文件来自任务 payload 的 ``options.backup_paths``：Pre 会改什么，只有填动作的
    3.4、3.6 知道，编排不猜。清单为空时不产生任何远端访问。

    备份清单写进 ``ctx.data["backup"]``：它随记录持久化，所以即使本次尝试在 In Progress
    阶段失败、任务被重新领取，Post 的「恢复环境」仍然知道要把哪些文件写回去。
    每个文件除了本地备份文件名，还记了 ``mode`` / ``uid`` / ``gid`` 与「改动前是否存在」：
    恢复要连权限与属主一起写回去，新建出来的文件则要删掉。

    同一次采集只备份一次：记录里已经有备份清单时直接返回。重试时若重新下载，目标机器上那份可能
    已经被临时配置改过，等于拿改过的内容覆盖原始备份，Post 再把改过的内容写回去还「看起来成功」。

    重试时先读上一次尝试留在目标机器上的改动记录：进程若在安装中途被强杀，采集记录还没有
    这次改动，而改动记录已经在远端落盘。把这些路径并入本次记录后，下面就不会再下载一份已经
    被改过的内容当成原始配置。
    """
    files = _backed_up_files(ctx)
    paths = tuple(ctx.options.get("backup_paths") or ())
    async with ctx.connect() as client:
        await adopt_recorded_changes(client, ctx)
        recorded = {item.path: item for item in read_changes(ctx)}
        for remote_path in paths:
            if remote_path not in files and remote_path in recorded:
                files[remote_path] = recorded[remote_path]
        for index, remote_path in enumerate(paths):
            if remote_path in files:
                continue
            files[remote_path] = await record_change(
                client, ctx, remote_path, backup_name=f"{index}.bak"
            )
    ctx.data["backup"] = {
        "paths": list(paths),
        "files": {path: change.as_mapping() for path, change in files.items()},
    }


async def apply_temporary_configuration(ctx: PhaseContext) -> None:
    """建远端暂存目录、临时打开资源记账、把恢复脚本放到目标机器上。

    远端暂存目录里放三样东西：备份副本、待安装的新配置、一份能脱离 Worker 手工执行的
    ``restore.sh``。后者的意义是进程被强杀时人还能在目标机器上直接收尾，不必先修好 Worker。

    记账按 Pre 能力检查的结论决定要不要打开：目标机器本来就够用时这一步什么都不改，
    同样留一条记录说明「不用改」，而不是安静跳过。
    """
    report = EnvironmentReport.from_mapping(ctx.data.get("environment"))
    async with ctx.connect() as client:
        await staging.create_workspace(client, ctx)
        await enable_temporary_accounting(client, ctx, report=report)
        await upload_restore_assets(client, ctx)


# ---- In Progress：启动采样、触发登录、持续采集 -----------------------------


async def start_sampling(_ctx: PhaseContext) -> None:
    """占位：启动远程采样循环，由任务 3.4 填入。当前不改变目标机器。"""


async def trigger_desktop_login(_ctx: PhaseContext) -> None:
    """占位：改 LightDM 配置并重启，等桌面会话建立，由任务 3.6 填入。当前不改变目标机器。"""


async def collect_continuously(_ctx: PhaseContext) -> None:
    """占位：在采样窗口内持续采集，由任务 3.4、3.5 填入。当前不改变目标机器。"""


# ---- Post：停止采样、收集数据、恢复环境 -----------------------------------


async def stop_sampling(_ctx: PhaseContext) -> None:
    """占位：停止采样循环，由任务 3.4 填入。当前不改变目标机器。"""


async def gather_collected_data(_ctx: PhaseContext) -> None:
    """占位：把采集结果与原始文件回传到中心服务器，由任务 3.7 填入。当前不改变目标机器。"""


async def restore_configuration(ctx: PhaseContext) -> None:
    """把 Pre 备份过的远端文件原样写回去，撤销自己加的改动，删掉远端暂存目录。

    什么都没动过时（Pre 在「检查目标环境」就失败）直接返回，不再连一次机器：Post 在每次尝试
    都会执行，重复执行必须是无害的，而这时连不上不该被记成「清理失败」。

    恢复动作可重复执行：原先存在的文件每次都被写回同一份备份内容（连权限与属主一起），
    原先不存在的文件每次都被删掉，删不存在的文件也是成功。

    备份文件在本地找不到时抛 ``RemoteError``：这种情况下远端很可能停在一份被改过的配置上，
    必须让阶段失败、让人看见，不能安静地跳过。
    """
    if not has_pending_restore(ctx):
        ctx.data["restored"] = []
        ctx.data["reverted"] = []
        return
    async with ctx.connect() as client:
        outcome = await restore_environment(client, ctx)
    ctx.data["restored"] = outcome["payload"]
    ctx.data["reverted"] = outcome["changes"]


def _backed_up_files(ctx: PhaseContext) -> dict[str, FileChange]:
    """读出记录里已有的备份清单。兼容 3.2 的旧形状（值只是一个本地文件名）。"""
    backup = ctx.data.get("backup") or {}
    files: dict[str, FileChange] = {}
    for path, raw in (backup.get("files") or {}).items():
        if isinstance(path, str):
            files[path] = FileChange.from_mapping(path, raw)
    return files


__all__ = [
    "BACKUP_DIR_NAME",
    "CollectionStep",
    "PhasePlan",
    "apply_temporary_configuration",
    "backup_configuration",
    "collect_continuously",
    "default_plan",
    "gather_collected_data",
    "inspect_target",
    "local_backup_dir",
    "restore_configuration",
    "start_sampling",
    "stop_sampling",
    "trigger_desktop_login",
]
