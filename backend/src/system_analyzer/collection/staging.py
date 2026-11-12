"""远端暂存目录：创建、上传、清理。

任务 3.3（MKACG-776）。采集期间要在目标机器上放几样东西：原配置的副本、待安装的新配置、
一份能脱离 Worker 手工执行的恢复脚本。它们都放在一个按采集任务 id 命名的目录里，
位置是 ``/var/tmp`` 而不是 ``/tmp``：``/tmp`` 在重新启动时会被清空，而采集过程本身可能
包含一次 LightDM 重启（3.6），暂存内容不能跟着消失。

目录由 SSH 用户自己创建（``/var/tmp`` 是 1777），往里放文件也不需要 root；
只有把文件安装到 ``/etc`` 下才需要提权（见 ``collection.restore``）。

目录名来自采集任务的 ``run_id``。``run_id`` 由 ``collection.jobs`` 限定为 UUID，这里再校验
一次字符集：这个名字会拼进 ``rm -rf`` 的参数，允许 ``..`` 之类等于把删除范围交给调用方。
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from system_analyzer.collection.context import PhaseContext
from system_analyzer.remote.client import SshClient

REMOTE_STAGING_ROOT = "/var/tmp/system-analyzer-collection"
"""远端暂存目录的根。每个采集任务在它下面有一个同名子目录。"""

BACKUP_DIR_NAME = "backup"
"""原件副本的子目录名。恢复脚本从这里读回内容，Worker 进程不在时也能用。"""

STAGE_DIR_NAME = "stage"
"""待安装文件与本地暂存文件的子目录名。"""

RESTORE_SCRIPT_NAME = "restore.sh"
"""能脱离 Worker 手工执行的恢复脚本的文件名。"""

LOCAL_STAGE_DIR_NAME = "stage"
"""Worker 本地暂存目录的子目录名（在采集任务的本地临时目录下）。"""

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def workspace_dir(run_id: str) -> str:
    """采集任务在目标机器上的暂存目录。``run_id`` 不合法时直接拒绝，不拼出可疑路径。"""
    if not _SAFE_RUN_ID.match(run_id):
        raise ValueError(f"采集任务 id 不能用来拼远端目录名：{run_id!r}")
    return f"{REMOTE_STAGING_ROOT}/{run_id}"


def workspace_from(ctx: PhaseContext) -> str:
    """这次尝试用的远端暂存目录。Pre 建过一次就记在记录里，重试时沿用同一个路径。"""
    stored = (ctx.data.get("workspace") or {}).get("remote_dir")
    if isinstance(stored, str) and stored:
        return stored
    return workspace_dir(ctx.run_id)


def local_stage_path(ctx: PhaseContext, name: str) -> Path:
    """Worker 本地暂存文件的位置。上传前先落到这里，上传接口只接受已经存在的本地文件。"""
    directory = ctx.scratch_dir / LOCAL_STAGE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


async def create_workspace(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> str:
    """建好远端暂存目录（含备份子目录与暂存子目录），返回它的路径。

    重复执行是安全的：``mkdir -p`` 不会因为目录已存在而失败，重试与手工恢复都能直接用。
    ``umask 077`` 让新建的目录只有 SSH 用户自己能进，别人的临时文件看不进来。
    """
    remote_dir = workspace_from(ctx)
    command = (
        f"umask 077 && mkdir -p {shlex.quote(remote_dir + '/' + BACKUP_DIR_NAME)} "
        f"{shlex.quote(remote_dir + '/' + STAGE_DIR_NAME)}"
    )
    await client.run(command, timeout=timeout)
    ctx.data["workspace"] = {
        "run_id": ctx.run_id,
        "remote_dir": remote_dir,
        "backup_dir": f"{remote_dir}/{BACKUP_DIR_NAME}",
        "stage_dir": f"{remote_dir}/{STAGE_DIR_NAME}",
    }
    return remote_dir


async def upload_bytes(
    client: SshClient,
    ctx: PhaseContext,
    remote_path: str,
    content: bytes,
    *,
    mode: str = "0600",
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> None:
    """把一段内容写到远端暂存目录里的某个文件，并设置权限。

    先落到 Worker 本地，再用 SFTP 上传：上传接口只接受本地已存在的文件，
    而 DDL 与配置片段的内容是现算出来的。
    """
    local_path = local_stage_path(ctx, f"upload-{_unique_name(remote_path)}")
    local_path.write_bytes(content)
    await client.upload(local_path, remote_path)
    await client.run(f"chmod {mode} {shlex.quote(remote_path)}", timeout=timeout)


async def remove_workspace(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> None:
    """删掉整个远端暂存目录。目录已经不在时同样是成功（``rm -rf`` 不报错）。"""
    remote_dir = workspace_from(ctx)
    await client.run(f"rm -rf {shlex.quote(remote_dir)}", timeout=timeout)


def _unique_name(remote_path: str) -> str:
    """本地暂存文件名：远端路径整段压平，避免在本地再建一层目录。"""
    return remote_path.strip("/").replace("/", "-")
