"""默认计划里「环境准备与回滚」的完整往返：准备 → 采集 → 恢复 → 清理。

四种结局（成功、失败、取消、超时）都要把目标机器恢复原样，记录要能追到具体文件。
全部用假客户端，不连真机；真机证据在 issue MKACG-776 的评论里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from collection_fakes import FakeConnector, RecordingSshClient
from system_analyzer.collection import staging
from system_analyzer.collection.accounting import ACCOUNTING_DROP_IN
from system_analyzer.collection.orchestrator import CollectionOutcome, run_collection
from system_analyzer.collection.plan import (
    BACKUP_DIR_NAME,
    CollectionStep,
    PhasePlan,
    default_plan,
)
from system_analyzer.collection.record import AttemptRecord, CollectionRecord
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import SshCommandError

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"
RUN_ID = "6a0f1b2c-1111-4c3d-9e8f-000000000042"

PRE = CollectionPhase.PRE
IN_PROGRESS = CollectionPhase.IN_PROGRESS
POST = CollectionPhase.POST


def make_connector(config: bytes = ORIGINAL_CONFIG) -> FakeConnector:
    return FakeConnector({TARGET: {CONFIG_PATH: config}})


async def run(connector, scratch_dir: Path, **kwargs) -> CollectionOutcome:
    return await run_collection(
        target_id=TARGET,
        run_id=RUN_ID,
        connector=connector,
        scratch_dir=scratch_dir,
        **kwargs,
    )


def with_steps(plan, phase, *steps) -> dict:
    result = dict(plan)
    result[phase] = PhasePlan(phase=phase, steps=tuple(steps))
    return result


def step(name: str, action) -> CollectionStep:
    return CollectionStep(name=name, run=action)


async def change_config(ctx) -> None:
    """代表 3.6 的 LightDM 片段：把目标机器上那个文件改掉。"""
    async with ctx.connect() as client:
        client.files[CONFIG_PATH] = b"[Seat:*]\n# temporary during collection\n"
        client.modes[CONFIG_PATH] = "600"


async def test_successful_run_prepares_and_cleans_up(tmp_path: Path) -> None:
    """一次成功采集：检查环境、备份配置、装临时片段、结束后全部撤回。"""
    connector = make_connector()
    plans = with_steps(default_plan(), IN_PROGRESS, step("改临时配置", change_config))

    outcome = await run(connector, tmp_path, plans=plans, options={"backup_paths": [CONFIG_PATH]})

    assert outcome.status is AttemptStatus.SUCCEEDED
    data = outcome.attempt.data

    # 能力检查的结论留在记录里
    assert data["environment"]["systemd_version"] == "255"
    assert data["environment"]["cgroup_version"] == "2"
    assert data["environment"]["privileged"] is True

    # 临时记账：借 drop-in 打开 IO 记账，并说明用了什么手段
    assert data["accounting"]["applied"] is True
    assert data["accounting"]["path"] == ACCOUNTING_DROP_IN
    assert data["accounting"]["settings"] == {"DefaultIOAccounting": "yes"}

    # 配置改动可追溯到具体文件：路径、权限、属主、备份校验和
    backup = data["backup"]["files"][CONFIG_PATH]
    assert backup["existed"] is True
    assert (backup["mode"], backup["uid"], backup["gid"]) == ("644", 0, 0)
    assert backup["backup_local"] == "0.bak"
    expected = hashlib.sha256(ORIGINAL_CONFIG).hexdigest()
    assert backup["checksum"] == expected
    assert (tmp_path / BACKUP_DIR_NAME / "0.bak").read_bytes() == ORIGINAL_CONFIG

    # 记账片段在那份改动清单里，恢复时按「原本不存在」删掉
    assert [item["path"] for item in data["changes"]] == [ACCOUNTING_DROP_IN]
    assert data["changes"][0]["existed"] is False

    # 目标机器回到原样：内容与权限都是采集前那一份，临时片段与暂存目录都没了
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    assert connector.modes_of(TARGET)[CONFIG_PATH] == "644", "采集期间改成的 600 必须被写回"
    assert ACCOUNTING_DROP_IN not in connector.files[TARGET]
    workspace = staging.workspace_dir(RUN_ID)
    assert not [path for path in connector.files[TARGET] if path.startswith(workspace + "/")]
    assert data["restored"] == [CONFIG_PATH]
    assert data["reverted"] == [ACCOUNTING_DROP_IN]


async def test_preparation_installs_config_with_root_privileges(tmp_path: Path) -> None:
    """普通用户写不了 /etc，所以安装与删除都必须是提权命令；口令不经过命令行。"""
    connector = make_connector()
    plans = with_steps(default_plan(), IN_PROGRESS, step("改临时配置", change_config))

    await run(connector, tmp_path, plans=plans, options={"backup_paths": [CONFIG_PATH]})

    privileged = connector.privileged_commands_of(TARGET)
    installs = [item for item in privileged if item.startswith("install ")]
    assert any(ACCOUNTING_DROP_IN in item for item in installs)
    assert any(item == f"rm -f {ACCOUNTING_DROP_IN}" for item in privileged)
    # 改 systemd 配置的两次改动各要一次 daemon-reload：装上之后与删掉之后
    assert privileged.count("systemctl daemon-reload") == 2
    # 每次提权只调用一次 sudo，没有把第二条命令串在 sudo 后面
    assert all("&&" not in item and "sudo " not in item for item in privileged)


async def test_restore_script_lands_on_the_target_and_is_removed_afterwards(
    tmp_path: Path,
) -> None:
    """恢复脚本在准备阶段放到目标机器上，采集正常结束时随暂存目录一起删掉。"""
    connector = make_connector()
    seen: dict[str, str] = {}

    async def inspect_staging(ctx) -> None:
        async with ctx.connect() as client:
            workspace = staging.workspace_dir(RUN_ID)
            script = f"{workspace}/{staging.RESTORE_SCRIPT_NAME}"
            assert script in client.files, "准备阶段应当已经把恢复脚本放上去"
            seen["script"] = client.files[script].decode()

    plans = with_steps(default_plan(), IN_PROGRESS, step("看一眼暂存目录", inspect_staging))

    await run(connector, tmp_path, plans=plans, options={"backup_paths": [CONFIG_PATH]})

    assert "#!/bin/sh" in seen["script"]
    assert "sudo install" in seen["script"]
    assert ACCOUNTING_DROP_IN in seen["script"]
    workspace = staging.workspace_dir(RUN_ID)
    assert not [path for path in connector.files[TARGET] if path.startswith(workspace + "/")]


@pytest.mark.parametrize(
    ("ending", "expected_status"),
    [
        ("failed", AttemptStatus.FAILED),
        ("cancelled", AttemptStatus.CANCELLED),
        ("timed_out", AttemptStatus.TIMED_OUT),
    ],
)
async def test_every_ending_still_restores_the_configuration(
    tmp_path: Path, ending: str, expected_status: AttemptStatus
) -> None:
    """失败、取消、超时三种结局：Post 仍然执行，临时片段被删掉、暂存目录被清掉。"""
    connector = make_connector()
    state = {"cancel": False}

    async def boom(_ctx) -> None:
        if ending == "timed_out":
            await asyncio.sleep(30)
            return
        raise RuntimeError("采样起不来")

    async def request_cancel(_ctx) -> None:
        state["cancel"] = True

    async def cancelled() -> bool:
        return state["cancel"]

    in_progress_steps = [step("改临时配置", change_config)]
    if ending == "cancelled":
        in_progress_steps.append(step("请求取消", request_cancel))
    in_progress_steps.append(step("启动采样", boom))
    in_progress_steps.append(step("不该跑到这里", _unreachable))

    plans = with_steps(default_plan(), IN_PROGRESS, *in_progress_steps)
    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        cancelled=cancelled if ending == "cancelled" else None,
        phase_timeouts={IN_PROGRESS: 0.05} if ending == "timed_out" else None,
    )

    assert outcome.status is expected_status
    assert outcome.attempt.phase(POST).status is PhaseStatus.SUCCEEDED
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    assert connector.modes_of(TARGET)[CONFIG_PATH] == "644", "采集期间改成的 600 必须被写回"
    assert ACCOUNTING_DROP_IN not in connector.files[TARGET]
    workspace = staging.workspace_dir(RUN_ID)
    assert not [path for path in connector.files[TARGET] if path.startswith(workspace + "/")]


async def _unreachable(_ctx) -> None:
    raise AssertionError("这一步不该被执行")


async def test_retry_adopts_the_journal_of_a_killed_attempt(tmp_path: Path) -> None:
    """上一次在安装中途被强杀（采集记录没存下改动）：下一次按远端改动记录收尾并删掉遗留片段。"""
    connector = make_connector()
    plans = with_steps(default_plan(), IN_PROGRESS, step("改临时配置", change_config))

    # 模拟「应用临时配置」装完 drop-in、写下改动记录后被强杀：
    # 远端留着片段与改动记录，采集记录里这次改动没有存下来，Pre 记成 interrupted。
    workspace = staging.workspace_dir(RUN_ID)
    connector.files[TARGET][ACCOUNTING_DROP_IN] = b"[Manager]\nDefaultIOAccounting=yes\n"
    connector.files[TARGET][workspace + "/changes.json"] = json.dumps(
        {"version": 1, "changes": [{"path": ACCOUNTING_DROP_IN, "existed": False}]}
    ).encode()
    killed = CollectionRecord(run_id=RUN_ID, target_id=TARGET)
    interrupted = AttemptRecord(attempt=1)
    killed.attempts.append(interrupted)
    interrupted.move(AttemptStatus.RUNNING)
    interrupted.phase(PRE).move(PhaseStatus.RUNNING)
    interrupted.phase(PRE).move(PhaseStatus.INTERRUPTED)
    checksum = hashlib.sha256(ORIGINAL_CONFIG).hexdigest()
    interrupted.data["backup"] = {
        "paths": [CONFIG_PATH],
        "files": {
            CONFIG_PATH: {
                "path": CONFIG_PATH,
                "existed": True,
                "mode": "644",
                "uid": 0,
                "gid": 0,
                "backup_local": "0.bak",
                "backup_remote": "backup/0.bak",
                "checksum": checksum,
            }
        },
    }
    (tmp_path / BACKUP_DIR_NAME).mkdir(exist_ok=True)
    (tmp_path / BACKUP_DIR_NAME / "0.bak").write_bytes(ORIGINAL_CONFIG)

    second = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        previous=killed,
    )

    assert second.status is AttemptStatus.SUCCEEDED
    assert second.attempt.phase(PRE).status is PhaseStatus.SUCCEEDED
    assert second.attempt.data["accounting"]["applied"] is True
    assert ACCOUNTING_DROP_IN not in connector.files[TARGET]
    assert second.attempt.data["backup"]["files"][CONFIG_PATH]["checksum"] == checksum
    assert workspace + "/changes.json" not in connector.files[TARGET]


async def test_retry_does_not_back_up_an_already_modified_file(tmp_path: Path) -> None:
    """改动记录说这个文件原本存在：重试时按记录里的备份恢复，不再下载当前（已改过的）内容。"""
    connector = make_connector()
    plans = with_steps(default_plan(), IN_PROGRESS, step("改临时配置", change_config))
    workspace = staging.workspace_dir(RUN_ID)
    modified = b"[Seat:*]\n# temporary during collection\n"
    connector.files[TARGET][CONFIG_PATH] = modified
    connector.files[TARGET][workspace + "/changes.json"] = json.dumps(
        {
            "version": 1,
            "changes": [
                {
                    "path": CONFIG_PATH,
                    "existed": True,
                    "mode": "644",
                    "uid": 0,
                    "gid": 0,
                    "backup_local": "0.bak",
                    "backup_remote": "backup/0.bak",
                    "checksum": hashlib.sha256(ORIGINAL_CONFIG).hexdigest(),
                }
            ],
        }
    ).encode()
    (tmp_path / BACKUP_DIR_NAME).mkdir()
    (tmp_path / BACKUP_DIR_NAME / "0.bak").write_bytes(ORIGINAL_CONFIG)

    killed = CollectionRecord(run_id=RUN_ID, target_id=TARGET)
    interrupted = AttemptRecord(attempt=1)
    killed.attempts.append(interrupted)
    interrupted.move(AttemptStatus.RUNNING)
    interrupted.phase(PRE).move(PhaseStatus.RUNNING)
    interrupted.phase(PRE).move(PhaseStatus.INTERRUPTED)

    second = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        previous=killed,
    )

    assert second.status is AttemptStatus.SUCCEEDED
    assert connector.downloads_of(TARGET) == []
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    assert (
        second.attempt.data["backup"]["files"][CONFIG_PATH]["checksum"]
        == hashlib.sha256(ORIGINAL_CONFIG).hexdigest()
    )


async def test_retry_adopting_one_journal_entry_still_backs_up_the_rest(tmp_path: Path) -> None:
    """改动记录只覆盖其中一个路径时，其余路径照常备份，不能因为清单非空就整段跳过。"""
    other_path = "/etc/systemd/journald.conf"
    other_content = b"[Journal]\n# original journald\n"
    connector = make_connector()
    connector.files[TARGET][other_path] = other_content
    plans = with_steps(default_plan(), IN_PROGRESS, step("改临时配置", change_config))

    workspace = staging.workspace_dir(RUN_ID)
    connector.files[TARGET][workspace + "/changes.json"] = json.dumps(
        {
            "version": 1,
            "changes": [
                {
                    "path": CONFIG_PATH,
                    "existed": True,
                    "mode": "644",
                    "uid": 0,
                    "gid": 0,
                    "backup_local": "0.bak",
                    "backup_remote": "backup/0.bak",
                    "checksum": hashlib.sha256(ORIGINAL_CONFIG).hexdigest(),
                }
            ],
        }
    ).encode()
    (tmp_path / BACKUP_DIR_NAME).mkdir()
    (tmp_path / BACKUP_DIR_NAME / "0.bak").write_bytes(ORIGINAL_CONFIG)

    killed = CollectionRecord(run_id=RUN_ID, target_id=TARGET)
    interrupted = AttemptRecord(attempt=1)
    killed.attempts.append(interrupted)
    interrupted.move(AttemptStatus.RUNNING)
    interrupted.phase(PRE).move(PhaseStatus.RUNNING)
    interrupted.phase(PRE).move(PhaseStatus.INTERRUPTED)

    second = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH, other_path]},
        previous=killed,
    )

    assert second.status is AttemptStatus.SUCCEEDED
    files = second.attempt.data["backup"]["files"]
    assert set(files) == {CONFIG_PATH, other_path}
    assert connector.downloads_of(TARGET) == [other_path], "已并入改动记录的路径不能再下载一次"
    assert (tmp_path / BACKUP_DIR_NAME / "1.bak").read_bytes() == other_content
    assert connector.files[TARGET][other_path] == other_content


class RefusingSudoClient(RecordingSshClient):
    """`sudo` 拒绝的客户端：验证「这台机器不给提权」被当成可读的结论而不是连不上。"""

    async def run_privileged(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议签名保持一致
        check: bool = True,
    ) -> CommandResult:
        self.privileged_commands.append(command)
        raise SshCommandError(
            "远端命令失败：sudo 需要口令",
            command=f"sudo -S -p '' {command}",
            exit_status=1,
            stderr="需要密码",
        )


class RefusingSudoConnector:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.clients: list[RefusingSudoClient] = []

    def connect(self, _target_id: str):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def open_client():
            client = RefusingSudoClient(self.files, hostname=TARGET)
            self.clients.append(client)
            yield client

        return open_client()


async def test_machine_without_sudo_fails_before_touching_anything(tmp_path: Path) -> None:
    """不能提权的机器：Pre 在检查阶段就失败，说清原因，目标机器上一个文件都没动。"""
    connector = RefusingSudoConnector({CONFIG_PATH: ORIGINAL_CONFIG})

    outcome = await run(connector, tmp_path, options={"backup_paths": [CONFIG_PATH]})

    assert outcome.status is AttemptStatus.FAILED
    detail = outcome.attempt.phase(PRE).detail
    assert detail is not None
    assert "检查目标环境" in detail
    assert "sudo" in detail
    assert outcome.attempt.data["environment"]["privileged"] is False
    # 没有任何安装动作，文件、暂存目录都没变
    assert connector.files[CONFIG_PATH] == ORIGINAL_CONFIG
    assert not [path for path in connector.files if path.startswith(staging.REMOTE_STAGING_ROOT)]
    assert not any(
        command.startswith("install ")
        for client in connector.clients
        for command in client.commands
    )
