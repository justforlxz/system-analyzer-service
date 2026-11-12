"""改动前先记账：备份原件、记录属性，结束时恢复，并留一份能手工跑的恢复脚本。

任务 3.3（MKACG-776）。这一模块回答三个问题：

1. **改之前是什么样**——``record_change()`` 读目标路径的权限与属主，把内容下载到 Worker 本地
   的备份目录，同时在远端暂存目录里留一份副本。读不到状态时先用提权再读一次；仍然读不到就
   让备份阶段失败，绝不把「不知道」记成「原本不存在」。
2. **怎么改**——``stage_and_install()`` 用提权把暂存文件安装到目标路径。目标机器上 ``/etc`` 下的
   目录只有 root 能写，普通用户连 ``touch`` 都会被拒绝，所以安装与删除都必须走
   ``SshClient.run_privileged()``，不能用 ``upload()`` 直接写目标路径。
   **安装之前**先把这次改动写进远端暂存目录里的改动记录（``changes.json``），进程在安装
   中途被强杀时，下一次尝试靠这份记录恢复，不必重新猜原始内容。
3. **怎么回去**——``restore_environment()`` 按记录逐条恢复：原先存在的写回原内容与
   ``mode`` / ``uid`` / ``gid``，原先不存在的删掉。整套动作可重复执行，跑两次结果一致。

恢复脚本是同一份记录的另一种执行方式：``build_restore_script()`` 生成一段 ``sh``，Worker 进程
不在了也能在目标机器上手工执行。它和 ``restore_environment()`` 做同样的事，所以两条路都得
可重复执行。

记录形状（存进 ``ctx.data``，随采集记录持久化）：

```json
{"path": "/etc/lightdm/lightdm.conf.d/99-x.conf", "existed": true, "mode": "644",
 "uid": 0, "gid": 0, "backup_local": "0.bak", "backup_remote": "backup/0.bak",
 "staged_remote": "stage/accounting.conf"}
```

3.2 的旧形状是 ``{远端路径: 本地文件名}``，没有属性也没有远端副本。旧记录要能照读：
属性未知时恢复用 ``cp -f``（只写内容，目标文件的权限与属主保持不变），
不能因为记录是旧的就报错。

改动记录（``changes.json``）是采集记录之外的第二份凭证：它直接留在目标机器的暂存目录里，
采集记录还没来得及落盘时下一次尝试仍能读到。``adopt_recorded_changes()`` 把它并回
``ctx.data["changes"]``，``backup_configuration()`` 因此不会再下载一份已经被改过的内容
当成「原始配置」。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system_analyzer.collection import staging
from system_analyzer.collection.context import PhaseContext
from system_analyzer.remote.client import SshClient
from system_analyzer.remote.errors import RemoteError

BACKUP_DIR_NAME = staging.BACKUP_DIR_NAME
"""Worker 本地备份目录的名字（在采集任务的本地临时目录下）。"""

DEFAULT_FILE_MODE = "644"
"""新装文件的默认权限。系统级配置片段通常是这个值。"""

DEFAULT_OWNER = (0, 0)
"""新装文件的默认属主，即 root:root。"""

SYSTEMD_CONFIG_PREFIX = "/etc/systemd/"
"""这个前缀下的文件被改动过时，恢复之后要重新加载 systemd 配置。"""

CHANGE_JOURNAL_NAME = "changes.json"
"""远端暂存目录里改动记录的文件名。它先于安装落盘，进程被强杀后下一次尝试靠它收尾。"""

_MODE = re.compile(r"^0?[0-7]{3,4}$")


@dataclass(frozen=True, slots=True)
class FileChange:
    """一个被改动过的文件：改动前是否存在、原来长什么样、备份在哪。"""

    path: str
    existed: bool
    mode: str | None = None
    uid: int | None = None
    gid: int | None = None
    backup_local: str | None = None
    backup_remote: str | None = None
    staged_remote: str | None = None
    checksum: str | None = None
    """备份内容的 SHA-256。记录里带上它，事后能核对「写回去的」与「当初备份的」是不是同一份。"""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "existed": self.existed,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "backup_local": self.backup_local,
            "backup_remote": self.backup_remote,
            "staged_remote": self.staged_remote,
            "checksum": self.checksum,
        }

    @classmethod
    def from_mapping(cls, path: str, raw: Any) -> FileChange:
        """读回记录。文件名这种旧形状按「改动前存在、备份是这个文件」解释。"""
        if isinstance(raw, str):
            return cls(path=path, existed=True, backup_local=raw)
        if not isinstance(raw, dict):
            return cls(path=path, existed=False)
        return cls(
            path=path,
            existed=raw.get("existed") is True,
            mode=_mode(raw.get("mode")),
            uid=_identifier(raw.get("uid")),
            gid=_identifier(raw.get("gid")),
            backup_local=_text_or_none(raw.get("backup_local")),
            backup_remote=_text_or_none(raw.get("backup_remote")),
            staged_remote=_text_or_none(raw.get("staged_remote")),
            checksum=_text_or_none(raw.get("checksum")),
        )

    @property
    def has_attributes(self) -> bool:
        """权限与属主是否都记下来了。旧记录没有这一项，恢复时只能写内容。"""
        return self.mode is not None and self.uid is not None and self.gid is not None


async def record_change(
    client: SshClient,
    ctx: PhaseContext,
    path: str,
    *,
    backup_name: str,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> FileChange:
    """读一次目标路径的状态并备份内容，返回这次改动的记录（不改目标机器上的任何文件）。

    路径原本不存在时只记「原本不存在」，恢复时据此删掉它；存在时内容下载到本地备份目录，
    并在远端备份目录留一份，供恢复脚本使用。
    """
    quoted = shlex.quote(path)
    stat = await client.run(f"stat -c '%a %u %g' {quoted}", timeout=timeout, check=False)
    attributes = _parse_stat(stat.stdout) if stat.ok else None
    if attributes is None:
        attributes = await _probe_as_root(client, path, timeout=timeout)
    if attributes is None:
        return FileChange(path=path, existed=False)

    mode, uid, gid = attributes
    local_backup_dir(ctx).mkdir(parents=True, exist_ok=True)
    local_path = local_backup_dir(ctx) / backup_name
    await client.download(path, local_path)
    return FileChange(
        path=path,
        existed=True,
        mode=mode,
        uid=uid,
        gid=gid,
        backup_local=backup_name,
        backup_remote=f"{BACKUP_DIR_NAME}/{backup_name}",
        checksum=hashlib.sha256(local_path.read_bytes()).hexdigest(),
    )


ABSENT_MARKER = "__sas_absent__"
"""提权探测的输出里表示「路径确实不存在」。它必须与「读不到」区分开。"""

UNKNOWN_MARKER = "__sas_unknown__"
"""提权探测跑完了却拿不到 ``stat`` 结果（例如目标机器没有可用的 ``stat``）。"""

_PROBE_SCRIPT = (
    f'if [ -e "$1" ]; then stat -c \'%a %u %g\' "$1" || echo {UNKNOWN_MARKER}; '
    f"else echo {ABSENT_MARKER}; fi"
)


async def _probe_as_root(
    client: SshClient,
    path: str,
    *,
    timeout: float | None,  # noqa: ASYNC109  超时参数沿用客户端接口
) -> tuple[str, int, int] | None:
    """普通身份读不到状态时的第二次尝试，一次问清「不存在」与「属性是什么」。

    提权本身失败会以 ``SshCommandError`` 上抛，不会走到这里；探测跑完却既没有属性也没有
    「不存在」标记时，说明 ``stat`` 不可用，抛错让备份阶段失败——绝不当成路径不存在，
    否则恢复会以 root 删掉一个原本存在、本次也没有创建的文件。
    """
    quoted = shlex.quote(path)
    result = await client.run_privileged(
        f"sh -c {shlex.quote(_PROBE_SCRIPT)} sh {quoted}", timeout=timeout
    )
    for line in result.stdout.splitlines():
        if line.strip() == ABSENT_MARKER:
            return None
        if line.strip() == UNKNOWN_MARKER:
            break
        attributes = _parse_stat(line)
        if attributes is not None:
            return attributes
    raise RemoteError(
        f"读不到目标文件的状态，无法确定它原本是否存在及其权限属主：{path}；"
        "为避免恢复时误删或误改，本次采集在备份阶段停止"
    )


def register_change(ctx: PhaseContext, change: FileChange) -> list[FileChange]:
    """把改动登记进这次采集的记录（按路径去重），返回登记后的完整清单。"""
    changes = read_changes(ctx)
    if not any(item.path == change.path for item in changes):
        changes.append(change)
    ctx.data["changes"] = [item.as_mapping() for item in changes]
    return changes


def read_changes(ctx: PhaseContext) -> list[FileChange]:
    """读出已经登记的改动清单。没有登记过时是空列表。"""
    raw = ctx.data.get("changes") or ()
    changes: list[FileChange] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        changes.append(FileChange.from_mapping(item["path"], item))
    return changes


async def stage_and_install(
    client: SshClient,
    ctx: PhaseContext,
    path: str,
    content: bytes,
    *,
    backup_name: str,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分
) -> FileChange:
    """改一个系统文件的完整流程：记录改动前状态 → 上传新内容到暂存目录 → 提权安装。

    新内容先落在远端暂存目录，再以 root 身份 ``install`` 到目标路径：普通用户不能直接往
    ``/etc`` 下写，而口令只存在于 ``remote/`` 的客户端里，所以这一步必须是提权命令。
    """
    await staging.create_workspace(client, ctx, timeout=timeout)
    changes = read_changes(ctx)
    existing = next((item for item in changes if item.path == path), None)
    change = existing or await record_change(
        client, ctx, path, backup_name=backup_name, timeout=timeout
    )

    staged_remote = f"{staging.STAGE_DIR_NAME}/{backup_name}"
    await staging.upload_bytes(
        client,
        ctx,
        staging.workspace_from(ctx) + "/" + staged_remote,
        content,
        mode="0600",
        timeout=timeout,
    )
    change = FileChange(**{**change.as_mapping(), "staged_remote": staged_remote})
    register_change(ctx, change)
    await write_change_journal(client, ctx, timeout=timeout)

    mode, uid, gid = _install_attributes(change)
    staged = shlex.quote(staging.workspace_from(ctx) + "/" + staged_remote)
    await client.run_privileged(
        f"install -m {mode} -o {uid} -g {gid} {staged} {shlex.quote(path)}", timeout=timeout
    )
    await write_restore_script(client, ctx, timeout=timeout)
    return change


async def write_change_journal(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分
) -> str:
    """把当前改动清单写进远端暂存目录，先写临时文件再原子替换。

    这一步发生在提权安装之前：进程若在安装中途被强杀，记录已经在了；若在写记录时被杀，
    旧记录仍然完整，临时文件被忽略。它只写 SSH 用户自己的暂存目录，不需要提权。
    """
    remote_path = staging.workspace_from(ctx) + "/" + CHANGE_JOURNAL_NAME
    temporary = remote_path + ".tmp"
    payload = {
        "version": 1,
        "changes": [item.as_mapping() for item in read_changes(ctx)],
    }
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
    await staging.upload_bytes(client, ctx, temporary, content, mode="0600", timeout=timeout)
    await client.run(f"mv -f {shlex.quote(temporary)} {shlex.quote(remote_path)}", timeout=timeout)
    ctx.data["change_journal"] = remote_path
    return remote_path


async def adopt_recorded_changes(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分
) -> set[str]:
    """读回上一次尝试留在目标机器上的改动记录，并入本次的记录，返回这些路径。

    读不到文件表示没有遗留记录；读到但解析不了属于状态不明，直接让阶段失败——这时继续备份
    会把一份已经被改过的内容当成原始配置。已经在本记录里的路径以本记录为准。
    """
    remote_path = staging.workspace_from(ctx) + "/" + CHANGE_JOURNAL_NAME
    result = await client.run(f"cat {shlex.quote(remote_path)}", timeout=timeout, check=False)
    if not result.ok:
        return set()
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteError(
            f"目标机器上的改动记录无法解析，无法确认采集前的原始状态：{remote_path}"
        ) from exc
    entries = raw.get("changes") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise RemoteError(
            f"目标机器上的改动记录格式不正确，无法确认采集前的原始状态：{remote_path}"
        )

    known = {item.path for item in read_changes(ctx)}
    adopted: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        change = FileChange.from_mapping(entry["path"], entry)
        if not _valid_journal_path(change.path):
            raise RemoteError(f"目标机器上的改动记录里有不合法的路径：{change.path!r}")
        if change.path in known:
            continue
        register_change(ctx, change)
        known.add(change.path)
        adopted.add(change.path)
    return adopted


async def upload_restore_assets(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> None:
    """把备份副本与恢复脚本放进远端暂存目录。

    远端副本的意义只有一个：Worker 进程死掉以后，人还能在目标机器上直接跑 ``restore.sh``。
    本地备份仍然是权威的那一份，进程内的恢复不依赖远端副本。
    """
    await staging.create_workspace(client, ctx, timeout=timeout)
    for change in changes_for_restore(ctx):
        if change.backup_local is None or change.backup_remote is None:
            continue
        local_path = local_backup_dir(ctx) / change.backup_local
        if not local_path.is_file():
            continue
        await client.upload(local_path, staging.workspace_from(ctx) + "/" + change.backup_remote)
    await write_restore_script(client, ctx, timeout=timeout)


def changes_for_restore(ctx: PhaseContext) -> list[FileChange]:
    """恢复脚本要覆盖的全部条目：payload 备份清单与采集过程自己的改动，按路径去重排序。"""
    combined: dict[str, FileChange] = {}
    backup = ctx.data.get("backup") or {}
    for path, raw in (backup.get("files") or {}).items():
        if isinstance(path, str):
            combined[path] = FileChange.from_mapping(path, raw)
    for change in read_changes(ctx):
        combined[change.path] = change
    return [combined[path] for path in sorted(combined)]


async def write_restore_script(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> str:
    """把当前改动清单写成恢复脚本，放到远端暂存目录。返回脚本的远端路径。"""
    await staging.create_workspace(client, ctx, timeout=timeout)
    remote_path = staging.workspace_from(ctx) + "/" + staging.RESTORE_SCRIPT_NAME
    script = build_restore_script(changes_for_restore(ctx))
    await staging.upload_bytes(
        client, ctx, remote_path, script.encode(), mode="0700", timeout=timeout
    )
    ctx.data["restore_script"] = remote_path
    return remote_path


def build_restore_script(changes: list[FileChange]) -> str:
    """生成一段可重复执行、可脱离 Worker 手工运行的 ``sh`` 恢复脚本。

    它只做两件事：把有备份的文件恢复成原内容与原来的权限属主，把原本不存在的文件删掉；
    改过 ``/etc/systemd`` 下的东西就重新加载一次 systemd 配置。脚本以远端暂存目录为基准，
    备份副本在同目录的 ``backup/`` 下，所以整个目录被搬走也还能用。
    """
    lines = [
        "#!/bin/sh",
        "# 由 system-analyzer-service 生成：把这次采集临时改过的文件恢复原样。",
        "# 用法：sh restore.sh（需要能执行 sudo 的终端）。重复执行结果一致。",
        "set -eu",
        'here=$(cd "$(dirname "$0")" && pwd)',
    ]
    for change in changes:
        target = shlex.quote(change.path)
        if not change.existed:
            lines.append(f"echo '删除 {change.path}'")
            lines.append(f"sudo rm -f {target}")
            continue
        if change.backup_remote is None:
            lines.append(f"echo '缺少备份，无法自动恢复 {change.path}，请人工核对' >&2")
            lines.append("exit 1")
            continue
        source = '"$here"/' + shlex.quote(change.backup_remote)
        mode, uid, gid = _install_attributes(change)
        lines.append(f"echo '恢复 {change.path}'")
        lines.append(f"sudo install -m {mode} -o {uid} -g {gid} {source} {target}")

    if any(item.path.startswith(SYSTEMD_CONFIG_PREFIX) for item in changes):
        lines.append("echo '重新加载 systemd 配置'")
        lines.append("sudo systemctl daemon-reload")
    return "\n".join(lines) + "\n"


def has_pending_restore(ctx: PhaseContext) -> bool:
    """这次采集有没有留下要收尾的东西：备份清单、自己的改动，或已经建好的远端暂存目录。

    三者皆无时说明 Pre 还没动过目标机器（例如它在「检查目标环境」就失败了），
    Post 不该为了「确认没有东西要收尾」再连一次机器：连不上会把这台机器的清理记成失败，
    而它其实从来没被改过。
    """
    return bool(
        (ctx.data.get("backup") or {}).get("files")
        or ctx.data.get("changes")
        or (ctx.data.get("workspace") or {}).get("remote_dir")
    )


async def restore_environment(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> dict[str, list[str]]:
    """把远端环境恢复原样，返回 ``{"payload": [...], "changes": [...]}`` 两类恢复过的路径。

    顺序是先恢复任务 payload 声明要备份的文件，再撤销采集过程自己加的改动，最后删掉暂存
    目录。可重复执行：目录先按需重建（第二次执行时它可能已经被删掉），恢复动作本身幂等。

    先恢复 payload 再撤销自己的改动，是因为 ``ctx.data["backup"]`` 里可能是别人（3.6 的
    LightDM 片段）改过的文件；自己的 drop-in 与它们互不影响。
    """
    await staging.create_workspace(client, ctx, timeout=timeout)
    payload = await restore_backed_up_files(client, ctx, timeout=timeout)
    changes = await revert_changes(client, ctx, timeout=timeout)
    if any(path.startswith(SYSTEMD_CONFIG_PREFIX) for path in (*payload, *changes)):
        await client.run_privileged("systemctl daemon-reload", timeout=timeout)
    await staging.remove_workspace(client, ctx, timeout=timeout)
    return {"payload": payload, "changes": changes}


async def restore_backed_up_files(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> list[str]:
    """按 ``ctx.data["backup"]`` 把备份过的文件写回原内容与原来的权限属主。"""
    backup = ctx.data.get("backup") or {}
    files = backup.get("files") or {}
    restored: list[str] = []
    for path, raw in files.items():
        if not isinstance(path, str):
            continue
        change = FileChange.from_mapping(path, raw)
        await _revert(client, ctx, change, timeout=timeout)
        restored.append(path)
    return sorted(restored)


async def revert_changes(
    client: SshClient,
    ctx: PhaseContext,
    *,
    timeout: float | None = None,  # noqa: ASYNC109  超时参数是公开接口的一部分，由 client.run 落实
) -> list[str]:
    """撤销采集过程自己加的改动：原先存在的写回原内容，原先不存在的删掉。"""
    reverted: list[str] = []
    for change in read_changes(ctx):
        await _revert(client, ctx, change, timeout=timeout)
        reverted.append(change.path)
    return sorted(reverted)


async def _revert(
    client: SshClient,
    ctx: PhaseContext,
    change: FileChange,
    *,
    timeout: float | None,  # noqa: ASYNC109  内部实现，超时由 client 的调用落实
) -> None:
    """恢复单个文件。目标原本不存在就删除（删不存在的文件也是成功）。"""
    if not change.existed:
        await client.run_privileged(f"rm -f {shlex.quote(change.path)}", timeout=timeout)
        return

    backup_dir = ctx.scratch_dir / BACKUP_DIR_NAME
    local_path = backup_dir / (change.backup_local or "")
    if change.backup_local is None or not local_path.is_file():
        raise RemoteError(f"本地备份文件不存在，无法恢复远端文件 {change.path}：{local_path}")
    staged_remote = f"{staging.STAGE_DIR_NAME}/restore-{change.backup_local}"
    await staging.upload_bytes(
        client,
        ctx,
        staging.workspace_from(ctx) + "/" + staged_remote,
        local_path.read_bytes(),
        mode="0600",
        timeout=timeout,
    )
    staged = shlex.quote(staging.workspace_from(ctx) + "/" + staged_remote)
    target = shlex.quote(change.path)
    if change.has_attributes:
        mode, uid, gid = _install_attributes(change)
        await client.run_privileged(
            f"install -m {mode} -o {uid} -g {gid} {staged} {target}", timeout=timeout
        )
    else:
        # 旧记录只有内容、没有权限与属主：只写内容，目标文件的属性保持现状。
        await client.run_privileged(f"cp -f {staged} {target}", timeout=timeout)


def _install_attributes(change: FileChange) -> tuple[str, int, int]:
    """安装一个文件时用的权限与属主。新文件按系统级配置的常见值，已有文件按记下来的原值。"""
    if change.has_attributes:
        return str(change.mode), int(change.uid or 0), int(change.gid or 0)
    return DEFAULT_FILE_MODE, DEFAULT_OWNER[0], DEFAULT_OWNER[1]


def _parse_stat(stdout: str) -> tuple[str, int, int] | None:
    """解析 ``stat -c '%a %u %g'`` 的输出。解析不了按「没读到」处理，不猜。"""
    parts = stdout.split()
    if len(parts) != 3:
        return None
    mode, uid, gid = parts
    parsed_mode = _mode(mode)
    parsed_uid = _identifier(uid)
    parsed_gid = _identifier(gid)
    if parsed_mode is None or parsed_uid is None or parsed_gid is None:
        return None
    return parsed_mode, parsed_uid, parsed_gid


def _mode(value: Any) -> str | None:
    """权限位只接受八进制数字，避免把远端返回的任意字符串拼进命令。"""
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and _MODE.match(value.strip()):
        return value.strip().lstrip("0") or "0"
    return None


def _identifier(value: Any) -> int | None:
    """uid / gid 只接受非负整数。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def local_backup_dir(ctx: PhaseContext) -> Path:
    """Worker 本地备份目录。测试与调用方按同一个名字找备份文件。"""
    return ctx.scratch_dir / BACKUP_DIR_NAME


def _valid_journal_path(path: str) -> bool:
    """改动记录里的路径必须是绝对路径且不含换行：它会被拼进提权命令。"""
    return path.startswith("/") and "\n" not in path and "\x00" not in path
