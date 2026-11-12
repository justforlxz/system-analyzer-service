"""三个阶段各自执行哪些步骤，以及步骤之间的边界。

本任务（3.2）只交付编排骨架：阶段的边界、顺序、状态、幂等、超时取消与清理在这里定下来；
每个阶段具体对目标机器做什么属于 3.4（资源采集）、3.5（Journal 采集）、3.6（LightDM 登录）、
3.7（数据回传），它们把动作填进下面这些步骤里，不需要改动编排。

已经带真实实现的步骤只有三个：Pre 的「检查目标环境」「备份系统配置」与 Post 的「恢复环境」。
其余步骤是明确标注了归属任务的占位实现，调用后不改变目标机器上的任何东西。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.states import CollectionPhase
from system_analyzer.remote.errors import RemoteError
from system_analyzer.remote.probe import probe_system_info

StepAction = Callable[[PhaseContext], Awaitable[None]]

BACKUP_DIR_NAME = "backup"
"""备份文件在本次采集的临时目录里的子目录名。Post 恢复时按同一个名字找回来。"""


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
    """连上去取一次系统基本信息，确认目标机器可用、口令可用。

    这一条同时是「这里真的连得上」的证明：连不上、口令错、超时都会抛 ``SshError`` 子类，
    阶段随之失败，后面两个阶段不会在没确认环境的情况下动手。
    """
    async with ctx.connect() as client:
        info = await probe_system_info(client)
    ctx.data["system_info"] = info.as_dict()


async def backup_configuration(ctx: PhaseContext) -> None:
    """把接下来可能要临时修改的远端文件下载到本次采集的临时目录。

    要备份哪些文件来自任务 payload 的 ``options.backup_paths``：Pre 会改什么，只有填动作的
    3.4、3.6 知道，编排不猜。清单为空时不产生任何远端访问。

    备份清单写进 ``ctx.data["backup"]``：它随记录持久化，所以即使本次尝试在 In Progress
    阶段失败、任务被重新领取，Post 的「恢复环境」仍然知道要把哪些文件写回去。

    同一次采集只备份一次：记录里已经有备份清单时直接返回。重试时若重新下载，目标机器上那份可能
    已经被临时配置改过，等于拿改过的内容覆盖原始备份，Post 再把改过的内容写回去还「看起来成功」。
    """
    files: dict[str, str] = dict((ctx.data.get("backup") or {}).get("files") or {})
    if files:
        return
    paths = tuple(ctx.options.get("backup_paths") or ())
    if paths:
        backup_dir = ctx.scratch_dir / BACKUP_DIR_NAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        async with ctx.connect() as client:
            for index, remote_path in enumerate(paths):
                local_name = f"{index}.bak"
                await client.download(remote_path, backup_dir / local_name)
                files[remote_path] = local_name
    ctx.data["backup"] = {"paths": list(paths), "files": files}


async def apply_temporary_configuration(_ctx: PhaseContext) -> None:
    """占位：临时开启 systemd 资源记账等改动，由任务 3.4 填入。当前不改变目标机器。"""


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
    """把 Pre 备份过的远端文件原样写回去。

    没有备份清单时什么都不做（Pre 没备份过文件）。备份文件在本地找不到时抛 ``RemoteError``：
    这种情况下远端很可能停在一份被改过的配置上，必须让阶段失败、让人看见，不能安静地跳过。
    """
    backup = ctx.data.get("backup") or {}
    files: dict[str, str] = dict(backup.get("files") or {})
    if not files:
        return
    backup_dir = ctx.scratch_dir / BACKUP_DIR_NAME
    async with ctx.connect() as client:
        for remote_path, local_name in files.items():
            local_path = backup_dir / local_name
            if not local_path.is_file():
                raise RemoteError(
                    f"本地备份文件不存在，无法恢复远端文件 {remote_path}：{local_path}"
                )
            await client.upload(local_path, remote_path)
    ctx.data["restored"] = sorted(files)
