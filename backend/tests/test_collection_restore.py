"""备份、提权安装、恢复与恢复脚本：可重复执行、属性还原、新旧记录兼容。

用假客户端走真实代码，不连目标机器。「恢复成原样」的断言直接比对文件内容、权限与属主，
不看「调用过 install」这种弱证据。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from collection_fakes import RecordingSshClient
from system_analyzer.collection import staging
from system_analyzer.collection.accounting import ACCOUNTING_DROP_IN, DROP_IN_BACKUP_NAME
from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.plan import BACKUP_DIR_NAME
from system_analyzer.collection.restore import (
    CHANGE_JOURNAL_NAME,
    FileChange,
    adopt_recorded_changes,
    build_restore_script,
    record_change,
    register_change,
    restore_environment,
    stage_and_install,
)
from system_analyzer.remote.errors import RemoteError

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"
RUN_ID = "5b1f0a4e-0000-4000-8000-000000000001"


class FakeConnector:
    """只有一个方法的连接器：直接把同一条假客户端交出去。"""

    def __init__(self, client: RecordingSshClient) -> None:
        self.client = client

    def connect(self, _target_id: str):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def open_client():
            yield self.client

        return open_client()


def make_ctx(tmp_path: Path, client: RecordingSshClient) -> PhaseContext:
    return PhaseContext(
        target_id=TARGET,
        run_id=RUN_ID,
        attempt=1,
        options={},
        scratch_dir=tmp_path,
        data={},
        connector=FakeConnector(client),  # type: ignore[arg-type]
    )


def make_client(config: bytes = ORIGINAL_CONFIG) -> RecordingSshClient:
    client = RecordingSshClient({CONFIG_PATH: config}, hostname=TARGET)
    client.modes[CONFIG_PATH] = "600"
    client.owners[CONFIG_PATH] = (0, 0)
    return client


def test_new_shape_records_attributes_and_checksum() -> None:
    change = FileChange(
        path=CONFIG_PATH,
        existed=True,
        mode="600",
        uid=0,
        gid=0,
        backup_local="0.bak",
        backup_remote="backup/0.bak",
        checksum="abc",
    )

    restored = FileChange.from_mapping(CONFIG_PATH, change.as_mapping())

    assert restored == change


def test_old_shape_from_3_2_is_still_readable() -> None:
    """3.2 的记录值是「本地文件名」：照读，属性未知，恢复时只写内容。"""
    change = FileChange.from_mapping(CONFIG_PATH, "0.bak")

    assert change.existed is True
    assert change.backup_local == "0.bak"
    assert change.has_attributes is False


def test_missing_file_record_means_delete_on_restore() -> None:
    change = FileChange.from_mapping(ACCOUNTING_DROP_IN, {"existed": False})

    assert change.existed is False
    assert change.has_attributes is False
    assert change.backup_local is None


async def test_record_change_backs_up_content_and_attributes(tmp_path: Path) -> None:
    client = make_client()
    ctx = make_ctx(tmp_path, client)

    change = await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak")

    assert change.existed is True
    assert (change.mode, change.uid, change.gid) == ("600", 0, 0)
    backed_up = tmp_path / BACKUP_DIR_NAME / "0.bak"
    assert backed_up.read_bytes() == ORIGINAL_CONFIG
    assert change.checksum == hashlib.sha256(ORIGINAL_CONFIG).hexdigest()
    # 备份是只读动作：目标机器上的文件一个字节都没变
    assert client.files[CONFIG_PATH] == ORIGINAL_CONFIG


async def test_record_change_of_a_missing_path_marks_it_as_created(tmp_path: Path) -> None:
    client = make_client()
    ctx = make_ctx(tmp_path, client)

    change = await record_change(client, ctx, ACCOUNTING_DROP_IN, backup_name="drop.conf")

    assert change.existed is False
    assert change.backup_local is None
    assert client.downloads == []


async def test_unreadable_stat_is_retried_as_root(tmp_path: Path) -> None:
    """普通身份读不到状态、提权能读到时，按真实状态备份，不能记成「不存在」。"""
    client = make_client()
    client.unprivileged_stat_denied.add(CONFIG_PATH)
    ctx = make_ctx(tmp_path, client)

    change = await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak")

    assert change.existed is True
    assert (change.mode, change.uid, change.gid) == ("600", 0, 0)
    assert (tmp_path / BACKUP_DIR_NAME / "0.bak").read_bytes() == ORIGINAL_CONFIG


async def test_state_that_cannot_be_read_at_all_fails_instead_of_deleting(
    tmp_path: Path,
) -> None:
    """两种身份都读不到状态、但路径确实存在：备份阶段失败，绝不记成「原本不存在」。"""
    client = make_client()
    client.stat_unavailable.add(CONFIG_PATH)
    ctx = make_ctx(tmp_path, client)

    with pytest.raises(RemoteError, match="读不到目标文件的状态"):
        await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak")

    assert CONFIG_PATH in client.files


async def test_change_journal_is_written_before_install(tmp_path: Path) -> None:
    """安装之前改动记录已经在远端落盘，进程在安装中途死亡时下一次尝试读得到。"""
    client = make_client()
    ctx = make_ctx(tmp_path, client)

    await stage_and_install(client, ctx, ACCOUNTING_DROP_IN, b"[Manager]\n", backup_name="d.c")

    journal = staging.workspace_dir(RUN_ID) + "/" + CHANGE_JOURNAL_NAME
    assert journal in client.files
    assert ACCOUNTING_DROP_IN.encode() in client.files[journal]


async def test_adopting_the_journal_keeps_the_original_change(tmp_path: Path) -> None:
    """新尝试从远端改动记录读回上一次没写进采集记录的改动，不重新备份改过的内容。"""
    client = make_client()
    first = make_ctx(tmp_path, client)
    await stage_and_install(client, first, ACCOUNTING_DROP_IN, b"[Manager]\n", backup_name="d.c")

    second = make_ctx(tmp_path, client)
    adopted = await adopt_recorded_changes(client, second)

    assert adopted == {ACCOUNTING_DROP_IN}
    change = next(item for item in second.data["changes"] if item["path"] == ACCOUNTING_DROP_IN)
    assert change["existed"] is False
    assert not [path for path in client.downloads if path == ACCOUNTING_DROP_IN]


async def test_stage_and_install_uses_one_privileged_install(tmp_path: Path) -> None:
    """新配置先落到暂存目录，再以 root 身份安装：普通用户不能直接写 /etc。"""
    client = make_client()
    ctx = make_ctx(tmp_path, client)

    change = await stage_and_install(
        client, ctx, ACCOUNTING_DROP_IN, b"[Manager]\nDefaultIOAccounting=yes\n", backup_name="d.c"
    )

    assert change.existed is False
    installs = [item for item in client.privileged_commands if item.startswith("install ")]
    assert len(installs) == 1
    assert ACCOUNTING_DROP_IN in installs[0]
    assert client.files[ACCOUNTING_DROP_IN] == b"[Manager]\nDefaultIOAccounting=yes\n"
    assert client.modes[ACCOUNTING_DROP_IN] == "644"
    assert client.owners[ACCOUNTING_DROP_IN] == (0, 0)
    # 恢复脚本同时落到了远端暂存目录
    script_path = staging.workspace_dir(RUN_ID) + "/" + staging.RESTORE_SCRIPT_NAME
    assert script_path in client.files


async def test_restore_puts_content_and_attributes_back(tmp_path: Path) -> None:
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    change = await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak")
    ctx.data["changes"] = [change.as_mapping()]
    client.files[CONFIG_PATH] = b"[Seat:*]\n# changed by the run\n"
    client.modes[CONFIG_PATH] = "666"
    client.owners[CONFIG_PATH] = (1000, 1000)

    outcome = await restore_environment(client, ctx)

    assert outcome["changes"] == [CONFIG_PATH]
    assert client.files[CONFIG_PATH] == ORIGINAL_CONFIG
    assert client.modes[CONFIG_PATH] == "600"
    assert client.owners[CONFIG_PATH] == (0, 0)


async def test_restore_deletes_files_that_did_not_exist(tmp_path: Path) -> None:
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    await stage_and_install(client, ctx, ACCOUNTING_DROP_IN, b"[Manager]\n", backup_name="d.c")
    assert ACCOUNTING_DROP_IN in client.files

    outcome = await restore_environment(client, ctx)

    assert outcome["changes"] == [ACCOUNTING_DROP_IN]
    assert ACCOUNTING_DROP_IN not in client.files
    assert f"rm -f {ACCOUNTING_DROP_IN}" in client.privileged_commands


async def test_restore_is_repeatable(tmp_path: Path) -> None:
    """同一份记录恢复两次，结果一致：第二次写回的还是同一份备份内容。"""
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    await stage_and_install(client, ctx, ACCOUNTING_DROP_IN, b"[Manager]\n", backup_name="d.c")
    register_change(ctx, await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak"))
    client.files[CONFIG_PATH] = b"# changed by the run\n"

    first = await restore_environment(client, ctx)
    after_first = (dict(client.files), dict(client.modes), dict(client.owners))
    second = await restore_environment(client, ctx)

    assert first == second
    assert (client.files, client.modes, client.owners) == after_first
    assert client.files[CONFIG_PATH] == ORIGINAL_CONFIG
    assert ACCOUNTING_DROP_IN not in client.files


async def test_restore_reloads_systemd_only_for_systemd_config(tmp_path: Path) -> None:
    """改过 /etc/systemd 下的东西才重新加载；只恢复 LightDM 配置时不碰 systemd。"""
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    change = await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak")
    ctx.data["changes"] = [change.as_mapping()]
    client.files[CONFIG_PATH] = b"# changed by the run\n"
    client.privileged_commands.clear()

    await restore_environment(client, ctx)

    assert "systemctl daemon-reload" not in client.privileged_commands

    ctx_second = make_ctx(tmp_path / "second", client)
    await stage_and_install(
        client, ctx_second, ACCOUNTING_DROP_IN, b"[Manager]\n", backup_name="d.c"
    )
    client.privileged_commands.clear()

    await restore_environment(client, ctx_second)

    assert "systemctl daemon-reload" in client.privileged_commands


async def test_restore_removes_the_remote_staging_directory(tmp_path: Path) -> None:
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    await stage_and_install(client, ctx, ACCOUNTING_DROP_IN, b"[Manager]\n", backup_name="d.c")

    await restore_environment(client, ctx)

    workspace = staging.workspace_dir(RUN_ID)
    assert workspace not in client.directories
    assert not [path for path in client.files if path.startswith(workspace + "/")]
    assert f"rm -rf {workspace}" in client.commands


async def test_missing_local_backup_fails_loudly(tmp_path: Path) -> None:
    """备份丢了就报错：远端可能停在一份改过的配置上，不能安静地跳过。"""
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    change = await record_change(client, ctx, CONFIG_PATH, backup_name="0.bak")
    ctx.data["changes"] = [change.as_mapping()]
    (tmp_path / BACKUP_DIR_NAME / "0.bak").unlink()

    with pytest.raises(RemoteError, match="本地备份文件不存在"):
        await restore_environment(client, ctx)


async def test_old_shape_record_restores_content_without_touching_attributes(
    tmp_path: Path,
) -> None:
    """旧记录没有权限与属主：只写内容（cp -f），目标文件现有属性保持不变。"""
    client = make_client()
    ctx = make_ctx(tmp_path, client)
    (tmp_path / BACKUP_DIR_NAME).mkdir()
    (tmp_path / BACKUP_DIR_NAME / "0.bak").write_bytes(ORIGINAL_CONFIG)
    ctx.data["backup"] = {"paths": [CONFIG_PATH], "files": {CONFIG_PATH: "0.bak"}}
    client.files[CONFIG_PATH] = b"# changed by the run\n"
    client.modes[CONFIG_PATH] = "640"
    client.owners[CONFIG_PATH] = (0, 43)

    outcome = await restore_environment(client, ctx)

    assert outcome["payload"] == [CONFIG_PATH]
    assert client.files[CONFIG_PATH] == ORIGINAL_CONFIG
    assert client.modes[CONFIG_PATH] == "640"
    assert client.owners[CONFIG_PATH] == (0, 43)
    assert any(command.startswith("cp -f ") for command in client.privileged_commands)


async def test_restore_script_covers_both_kinds_of_change() -> None:
    script = build_restore_script(
        [
            FileChange(
                path=CONFIG_PATH,
                existed=True,
                mode="600",
                uid=0,
                gid=0,
                backup_local="0.bak",
                backup_remote="backup/0.bak",
            ),
            FileChange(path=ACCOUNTING_DROP_IN, existed=False),
        ]
    )

    assert script.startswith("#!/bin/sh")
    assert 'here=$(cd "$(dirname "$0")" && pwd)' in script
    assert f'sudo install -m 600 -o 0 -g 0 "$here"/backup/0.bak {CONFIG_PATH}' in script
    assert f"sudo rm -f {ACCOUNTING_DROP_IN}" in script
    # 改过 systemd 配置，脚本自己会重新加载
    assert "sudo systemctl daemon-reload" in script


def test_restore_script_skips_daemon_reload_for_non_systemd_files() -> None:
    script = build_restore_script([FileChange(path=CONFIG_PATH, existed=False)])

    assert "daemon-reload" not in script


def test_restore_script_refuses_to_pretend_when_backup_is_missing() -> None:
    """没有远端副本的旧记录：脚本明确报错退出，不假装恢复成功。"""
    script = build_restore_script(
        [FileChange(path=CONFIG_PATH, existed=True, backup_local="0.bak", backup_remote=None)]
    )

    assert "缺少备份" in script
    assert script.splitlines()[-1] == "exit 1"


def test_restore_script_quotes_paths_with_spaces() -> None:
    script = build_restore_script([FileChange(path="/etc/o dd/x.conf", existed=False)])

    assert "sudo rm -f '/etc/o dd/x.conf'" in script


def test_drop_in_backup_name_is_stable() -> None:
    """记账片段的备份文件名固定，手工恢复时能在暂存目录里一眼认出来。"""
    assert DROP_IN_BACKUP_NAME == "systemd-accounting.conf"
