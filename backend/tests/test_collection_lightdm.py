"""LightDM 临时自动登录：会话判定、两类失败、恢复到原样、恢复不重启 LightDM。

全部用假目标机器走真实编排，不连真机；真机证据在 issue MKACG-779 的评论里。
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from collection_fakes import FakeConnector, RecordingSshClient
from system_analyzer.collection import staging
from system_analyzer.collection.accounting import ACCOUNTING_DROP_IN
from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.lightdm import (
    AUTOLOGIN_FRAGMENT,
    LIGHTDM_CONFIG,
    DesktopLoginError,
    LogindSession,
    autologin_fragment,
    desktop_login_timeout,
    desktop_user,
    established_session,
    parse_lightdm_state,
    parse_sessions,
    wait_for_desktop_session,
)
from system_analyzer.collection.orchestrator import CollectionOutcome, run_collection
from system_analyzer.collection.restore import record_change, register_change, restore_environment
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus

TARGET = "uos-152"
RUN_ID = "6a0f1b2c-7777-4c3d-9e8f-000000000036"
ORIGINAL_CONFIG = b"[Seat:*]\ngreeter-session=lightdm-deepin-greeter\nuser-session=deepin\n"

SAMPLE_SESSIONS = """session=2
Id=2
Name=uos
Seat=seat0
Type=x11
Class=user
Active=yes
State=active
Remote=no
TTY=tty1
Display=:0
session=677
Id=677
Name=uos
Seat=
Type=tty
Class=user
Active=yes
State=active
Remote=yes
TTY=pts/1
Display=
"""


def make_connector(config: bytes = ORIGINAL_CONFIG) -> FakeConnector:
    return FakeConnector({TARGET: {LIGHTDM_CONFIG: config}})


class SingleClient:
    """只交出一条固定假客户端的连接器，用于直接调用单个函数。"""

    def __init__(self, client: RecordingSshClient) -> None:
        self.client = client

    def connect(self, _target_id: str):
        @asynccontextmanager
        async def open_client():
            yield self.client

        return open_client()


async def run(connector, scratch_dir: Path, **options) -> CollectionOutcome:
    return await run_collection(
        target_id=TARGET,
        run_id=RUN_ID,
        connector=connector,
        scratch_dir=scratch_dir,
        options={"desktop_user": "uos", "desktop_login_timeout": 0.05, **options},
    )


def lightdm_state(outcome: CollectionOutcome) -> dict:
    return outcome.attempt.data["desktop_login"]


# ---- 解析与会话判定 -------------------------------------------------------


def test_parses_loginctl_sessions_and_their_graphical_state() -> None:
    sessions = parse_sessions(SAMPLE_SESSIONS)

    assert [session.session_id for session in sessions] == ["2", "677"]
    desktop = sessions[0]
    assert (desktop.user, desktop.seat, desktop.type, desktop.class_) == (
        "uos",
        "seat0",
        "x11",
        "user",
    )
    assert desktop.is_desktop_ready is True
    remote = sessions[1]
    assert remote.seat == ""
    assert remote.is_desktop_ready is False


def test_half_written_session_block_is_dropped() -> None:
    """缺 ``Id`` 的块不能当成会话：不能把半截输出算成「桌面已经建立」。"""
    assert parse_sessions("session=9\nName=uos\nSeat=seat0\n") == ()


def test_a_seat0_session_is_not_ready_while_it_is_still_activating() -> None:
    session = LogindSession(
        session_id="42",
        user="uos",
        seat="seat0",
        type="x11",
        class_="user",
        active=False,
        state="online",
        remote=False,
    )

    assert session.is_desktop_ready is False


def test_only_a_new_session_counts_as_the_autologin_result() -> None:
    """采集前就存在的 seat0 会话不能算这次自动登录建起来的。"""
    sessions = parse_sessions(SAMPLE_SESSIONS)

    assert established_session(sessions, user="uos", baseline_ids={"2"}) is None
    assert established_session(sessions, user="uos", baseline_ids=set()) == sessions[0]


def test_parses_lightdm_state() -> None:
    state = parse_lightdm_state(
        "lightdm_active=active\n"
        "lightdm_conf_sha256=abc123\n"
        "lightdm_conf_mode=644 0 0\n"
        "fragment_dir=99-other.conf\n"
    )

    assert state.active is True
    assert state.config_sha256 == "abc123"
    assert state.config_mode == "644 0 0"
    assert state.fragments == ("99-other.conf",)


def test_desktop_options_are_validated_without_touching_the_machine() -> None:
    assert desktop_user({}) is None
    assert desktop_user({"desktop_user": "  "}) is None
    assert desktop_user({"desktop_user": "uos"}) == "uos"
    assert desktop_login_timeout({"desktop_login_timeout": 5}) == 5.0
    assert desktop_login_timeout({"desktop_login_timeout": "soon"}) == 90.0
    assert desktop_login_timeout({"desktop_login_timeout": 0}) == 90.0
    assert desktop_login_timeout({"desktop_login_timeout": 99999}) == 600.0

    with pytest.raises(DesktopLoginError) as raised:
        desktop_user({"desktop_user": "uos; reboot"})
    assert raised.value.kind == "autologin_not_triggered"


def test_fragment_uses_lightdm_native_autologin_only() -> None:
    content = autologin_fragment("uos")

    assert "[Seat:*]" in content
    assert "autologin-user=uos" in content
    assert "autologin-user-timeout=0" in content
    # 没有任何键盘模拟或设备写入
    assert "uinput" not in content
    assert "xdotool" not in content


# ---- 完整编排：成功 -------------------------------------------------------


async def test_autologin_builds_a_seat0_session_then_restores_without_restart(
    tmp_path: Path,
) -> None:
    """成功路径：片段生效 → 新 seat0 图形会话 → 结束后删片段，主配置不变且不再重启。"""
    connector = make_connector()
    before = hashlib.sha256(ORIGINAL_CONFIG).hexdigest()

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.SUCCEEDED
    record = lightdm_state(outcome)
    assert record["established"] is True
    session = record["session"]
    assert (session["user"], session["seat"], session["type"], session["active"]) == (
        "uos",
        "seat0",
        "x11",
        True,
    )
    assert record["baseline_sessions"][0]["id"] == "2", "采集前的会话必须记下来做基线"
    assert record["lightdm_before"]["config_sha256"] == before

    # 片段只在采集窗口内存在，Post 把它删掉
    assert AUTOLOGIN_FRAGMENT not in connector.files[TARGET]
    assert ACCOUNTING_DROP_IN not in connector.files[TARGET]
    assert not [
        path for path in connector.files[TARGET] if path.startswith(staging.workspace_dir(RUN_ID))
    ]
    assert connector.files[TARGET][LIGHTDM_CONFIG] == ORIGINAL_CONFIG

    # 全程只重启一次 LightDM，且重启时片段已经装好
    assert len(connector.lightdm_restarts) == 1
    assert connector.lightdm_restarts[0]["user"] == "uos"
    restarts = [
        command
        for command in connector.privileged_commands_of(TARGET)
        if command == "systemctl restart lightdm"
    ]
    assert len(restarts) == 1, "恢复阶段不得再重启 LightDM"
    # LightDM 配置不归 systemd 管：恢复只删片段，不做 daemon-reload 之外的 systemd 动作
    assert "systemctl daemon-reload" in connector.privileged_commands_of(TARGET)


async def test_restored_config_and_checksum_match_the_snapshot(tmp_path: Path) -> None:
    """恢复后主配置逐字节一致、片段目录回到空，备份记录的校验和与快照一致。"""
    connector = make_connector()
    before = hashlib.sha256(ORIGINAL_CONFIG).hexdigest()

    outcome = await run(connector, tmp_path)

    after = hashlib.sha256(connector.files[TARGET][LIGHTDM_CONFIG]).hexdigest()
    assert after == before
    fragments = [
        path for path in connector.files[TARGET] if path.startswith("/etc/lightdm/lightdm.conf.d/")
    ]
    assert fragments == []
    change = next(
        item for item in outcome.attempt.data["changes"] if item["path"] == AUTOLOGIN_FRAGMENT
    )
    assert change["existed"] is False


async def test_existing_fragment_is_recorded_with_attributes_and_checksum(
    tmp_path: Path,
) -> None:
    """片段原本存在时，备份记录要留下权限、属主与 SHA-256，恢复后写回原内容。"""
    original = "# 发行版自带片段\n[Seat:*]\nautologin-user=someone-else\n".encode()
    connector = make_connector()
    connector.files[TARGET][AUTOLOGIN_FRAGMENT] = original

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.SUCCEEDED
    change = next(
        item for item in outcome.attempt.data["changes"] if item["path"] == AUTOLOGIN_FRAGMENT
    )
    assert change["existed"] is True
    assert (change["mode"], change["uid"], change["gid"]) == ("644", 0, 0)
    assert change["checksum"] == hashlib.sha256(original).hexdigest()
    assert connector.files[TARGET][AUTOLOGIN_FRAGMENT] == original


# ---- 完整编排：两类失败 ---------------------------------------------------


async def test_missing_autologin_is_reported_as_not_triggered(tmp_path: Path) -> None:
    """自动登录没发生：没有新会话。采集前的 seat0 会话存在也不能算成功。"""
    connector = make_connector()
    connector.autologin_effective = False

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.FAILED
    assert outcome.attempt.phase(CollectionPhase.IN_PROGRESS).status is PhaseStatus.FAILED
    record = lightdm_state(outcome)
    assert record["established"] is False
    assert record["failure_kind"] == "autologin_not_triggered"
    assert "没有出现新的本地会话" in record["detail"]
    assert outcome.failure is not None and "没有出现新的本地会话" in outcome.failure
    # 失败也要恢复：片段被删掉
    assert AUTOLOGIN_FRAGMENT not in connector.files[TARGET]


async def test_created_but_incomplete_session_is_a_different_failure(tmp_path: Path) -> None:
    """自动登录发生了但会话没建立：会话存在却不在 seat0。错误分类必须不同。"""
    connector = make_connector()
    connector.desktop_session_ready = False

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.FAILED
    record = lightdm_state(outcome)
    assert record["established"] is False
    assert record["failure_kind"] == "session_not_established"
    assert "桌面会话没有建立" in record["detail"]
    assert "seat0" in record["detail"]
    assert record["observed_sessions"], "要把观察到的会话写进记录，否则无法解释判定"
    assert AUTOLOGIN_FRAGMENT not in connector.files[TARGET]


async def test_lightdm_restart_failure_is_reported_as_not_triggered(tmp_path: Path) -> None:
    connector = make_connector()
    connector.lightdm_restart_stderr = "Unit lightdm.service not found."

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.FAILED
    record = lightdm_state(outcome)
    assert record["failure_kind"] == "autologin_not_triggered"
    assert "重启 LightDM 失败" in record["detail"]
    assert "Unit lightdm.service not found." in record["detail"]
    assert (
        sum(
            command == "systemctl restart lightdm"
            for command in connector.privileged_commands_of(TARGET)
        )
        == 1
    )
    assert AUTOLOGIN_FRAGMENT not in connector.files[TARGET]


async def test_unknown_user_fails_before_any_lightdm_change(tmp_path: Path) -> None:
    connector = make_connector()
    connector.known_users = {"someone-else"}

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.FAILED
    record = lightdm_state(outcome)
    assert record["failure_kind"] == "autologin_not_triggered"
    assert "没有用户 uos" in record["detail"]
    assert connector.lightdm_restarts == []
    assert not any("lightdm" in command for command in connector.privileged_commands_of(TARGET))


# ---- 未配置桌面用户 / 默认计划 --------------------------------------------


async def test_without_a_desktop_user_the_machine_is_untouched(tmp_path: Path) -> None:
    connector = make_connector()

    outcome = await run_collection(
        target_id=TARGET,
        run_id=RUN_ID,
        connector=connector,
        scratch_dir=tmp_path,
        options={},
    )

    assert outcome.status is AttemptStatus.SUCCEEDED
    record = outcome.attempt.data["desktop_login"]
    assert record["applied"] is False
    assert record["user"] is None
    assert connector.lightdm_restarts == []
    assert AUTOLOGIN_FRAGMENT not in connector.files[TARGET]


# ---- 恢复到原样 -----------------------------------------------------------


async def test_restoring_an_absent_fragment_is_idempotent(tmp_path: Path) -> None:
    """片段不存在时删它是幂等的：同一份记录连跑两次恢复都成功。"""
    connector = make_connector()

    client = RecordingSshClient(connector.files[TARGET], hostname=TARGET)
    ctx = PhaseContext(
        target_id=TARGET,
        run_id=RUN_ID,
        attempt=1,
        options={},
        scratch_dir=tmp_path,
        data={},
        connector=SingleClient(client),  # type: ignore[arg-type]
    )
    register_change(ctx, await record_change(client, ctx, AUTOLOGIN_FRAGMENT, backup_name="a.bak"))

    first = await restore_environment(client, ctx)
    second = await restore_environment(client, ctx)

    assert first == second == {"payload": [], "changes": [AUTOLOGIN_FRAGMENT]}
    assert AUTOLOGIN_FRAGMENT not in client.files
    assert client.files[LIGHTDM_CONFIG] == ORIGINAL_CONFIG


# ---- 等待循环 -------------------------------------------------------------


async def test_waiting_stops_at_the_deadline_and_reports_what_it_saw(tmp_path: Path) -> None:
    """超时返回最后一次观察结果，而不是无限轮询。"""
    connector = make_connector()

    client = RecordingSshClient(connector.files[TARGET], hostname=TARGET)
    session, observed = await wait_for_desktop_session(
        client, user="uos", baseline_ids=set(), timeout=0.03
    )

    assert session is not None
    assert {item.session_id for item in observed} == {"2", "677"}
