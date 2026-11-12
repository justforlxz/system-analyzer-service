"""用 LightDM 原生自动登录临时打开指定桌面用户的图形会话。

任务 3.6（MKACG-779）。这个模块只回答三件事：

1. **怎么打开自动登录**——往 ``/etc/lightdm/lightdm.conf.d/`` 放一个临时片段，内容是
   LightDM 自己的 ``autologin-user`` 设置。不改 ``/etc/lightdm/lightdm.conf``，不改用户密码，
   不模拟键盘，也不碰 ``/dev/uinput``。片段通过 3.3 的 ``restore.stage_and_install()`` 安装，
   因此备份、权限属主、改动记录与 ``restore.sh`` 都沿用同一条路径。
2. **怎么确认登录成功**——重启 LightDM 之后读 ``loginctl``，要求出现目标用户**新建立的**、
   位于 ``seat0`` 的、``Active=yes`` 的图形会话。只是「有一个会话」不算成功：远程 SSH 会话、
   没有 seat 的会话、还没激活的会话都要排除。
3. **怎么区分两种失败**——「自动登录根本没有发生」（没有新会话，或 LightDM 没重启成功）与
   「自动登录发生了但桌面会话没建立」（有新会话，但缺 seat0、不是 active 或类型不对）给不同的
   ``DesktopLoginError.kind`` 与不同的中文消息。两者都在 ``ctx.data["desktop_login"]`` 里留下
   结构化字段，读记录的人不必猜。

恢复不归这个模块管：片段登记在 ``ctx.data["changes"]`` 里，Post 阶段的
``plan.restore_configuration()`` 会按「原本不存在」把它删掉。**恢复只删片段，不重启 LightDM**，
所以采集结束后已有的桌面会话不会被恢复动作打断；片段删除后，下一次 LightDM 启动才回到
原来的无自动登录状态。
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.restore import stage_and_install
from system_analyzer.remote.client import SshClient
from system_analyzer.remote.errors import RemoteError, SshCommandError

DESKTOP_USER_OPTION = "desktop_user"
"""任务 payload 的 ``options.desktop_user``：要自动登录的桌面用户名。"""

DESKTOP_LOGIN_TIMEOUT_OPTION = "desktop_login_timeout"
"""任务 payload 的 ``options.desktop_login_timeout``：等待桌面会话建立的秒数。"""

DEFAULT_DESKTOP_LOGIN_TIMEOUT_SECONDS = 90.0
"""默认等待时间。DDE 会话从登录到座位激活要十几秒，留足余量但不超过阶段上限。"""

FRAGMENT_DIR = "/etc/lightdm/lightdm.conf.d"
"""LightDM 的配置片段目录。只往这里放临时文件。"""

AUTOLOGIN_FRAGMENT = f"{FRAGMENT_DIR}/99-system-analyzer-autologin.conf"
"""临时自动登录片段的路径。文件名前缀 99 让它排在发行版自带片段之后。"""

AUTOLOGIN_BACKUP_NAME = "lightdm-autologin.conf"
"""本地备份与远端暂存里用的文件名。"""

LIGHTDM_CONFIG = "/etc/lightdm/lightdm.conf"
"""LightDM 主配置。这个任务只读它做前后核对，绝不修改。"""

LIGHTDM_SERVICE = "lightdm"
"""LightDM 的 systemd 服务名。"""

GRAPHICAL_SESSION_TYPES = ("x11", "wayland", "mir")
"""``loginctl`` 里算作图形会话的 ``Type`` 取值。``tty`` 是远程或文本会话，不算桌面。"""

_FAILURE_NOT_TRIGGERED = "autologin_not_triggered"
_FAILURE_SESSION_NOT_ESTABLISHED = "session_not_established"

_USERNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
"""Linux 用户名允许的形状。它会被写进配置片段，所以只接受这一种字符集。"""

LIGHTDM_STATE_COMMAND = (
    "printf 'lightdm_active=%s\\n' \"$(systemctl is-active lightdm 2>/dev/null)\"; "
    "printf 'lightdm_conf_sha256=%s\\n' "
    "\"$(sha256sum /etc/lightdm/lightdm.conf 2>/dev/null | awk '{print $1}')\"; "
    "printf 'lightdm_conf_mode=%s\\n' "
    "\"$(stat -c '%a %u %g' /etc/lightdm/lightdm.conf 2>/dev/null)\"; "
    "printf 'fragment_dir=%s\\n' "
    "\"$(ls -1 /etc/lightdm/lightdm.conf.d 2>/dev/null | tr '\\n' ' ')\""
)
"""一次读回 LightDM 的当前状态：是否在运行、主配置的校验和与权限、片段目录内容。

每一项都用 ``$(...) 2>/dev/null`` 包住：某一项读不到时是空值，不会让整条命令失败。
"""

SESSIONS_COMMAND = (
    "for id in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do "
    "printf 'session=%s\\n' \"$id\"; "
    'loginctl show-session "$id" '
    "-p Id -p Name -p Seat -p Type -p Class -p Active -p State -p Remote 2>/dev/null || true; "
    "done"
)
"""列出全部 logind 会话及其关键属性。一条命令取回，避免逐个会话往返。"""


@dataclass(frozen=True, slots=True)
class LogindSession:
    """一个 logind 会话里与「是否桌面已建立」有关的字段。"""

    session_id: str
    user: str
    seat: str
    type: str
    class_: str
    active: bool
    state: str
    remote: bool

    @property
    def is_graphical(self) -> bool:
        return self.type in GRAPHICAL_SESSION_TYPES and self.class_ == "user"

    @property
    def is_desktop_ready(self) -> bool:
        """目标用户已经坐在 seat0 上、图形会话处于活动状态。"""
        return (
            self.seat == "seat0"
            and self.active
            and self.state == "active"
            and self.is_graphical
            and not self.remote
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "user": self.user,
            "seat": self.seat,
            "type": self.type,
            "class": self.class_,
            "active": self.active,
            "state": self.state,
            "remote": self.remote,
        }

    def summary(self) -> str:
        """一行可读摘要，进错误消息与记录。"""
        seat = self.seat or "无 seat"
        return (
            f"会话 {self.session_id}（用户 {self.user or '未知'}，{seat}，"
            f"Type={self.type or '未知'}，Class={self.class_ or '未知'}，"
            f"Active={'yes' if self.active else 'no'}，State={self.state or '未知'}，"
            f"Remote={'yes' if self.remote else 'no'}）"
        )


@dataclass(frozen=True, slots=True)
class LightDmState:
    """采集前的 LightDM 状态，用于事后核对主配置没有被改过。"""

    active: bool
    config_sha256: str | None
    config_mode: str | None
    fragments: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "config_sha256": self.config_sha256,
            "config_mode": self.config_mode,
            "fragments": list(self.fragments),
        }


class DesktopLoginError(RemoteError):
    """自动登录没有成功。``kind`` 区分「没有触发」与「会话没建立」。"""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        sessions: tuple[LogindSession, ...] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.sessions = sessions


def desktop_user(options: Mapping[str, Any]) -> str | None:
    """从任务选项里取桌面用户名。没配置时返回 ``None``，表示本次采集不做桌面登录。"""
    raw = options.get(DESKTOP_USER_OPTION)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise DesktopLoginError(
            _FAILURE_NOT_TRIGGERED,
            f"任务选项 {DESKTOP_USER_OPTION} 必须是字符串，实际是 {type(raw).__name__}",
        )
    user = raw.strip()
    if not user:
        return None
    if not _USERNAME.match(user):
        raise DesktopLoginError(
            _FAILURE_NOT_TRIGGERED,
            f"桌面用户名不合法：{user!r}；只允许字母、数字、下划线、点和短横线，"
            "且以字母或下划线开头",
        )
    return user


def desktop_login_timeout(options: Mapping[str, Any]) -> float:
    """等待桌面会话建立的秒数。配置不合法时用默认值，不让采集因为一个可选项失败。"""
    raw = options.get(DESKTOP_LOGIN_TIMEOUT_OPTION)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return DEFAULT_DESKTOP_LOGIN_TIMEOUT_SECONDS
    value = float(raw)
    if value <= 0:
        return DEFAULT_DESKTOP_LOGIN_TIMEOUT_SECONDS
    return min(value, 600.0)


def autologin_fragment(user: str) -> str:
    """临时片段的内容。只写 LightDM 自己的自动登录设置，不写任何脚本或键盘模拟。"""
    return (
        "# 由 system-analyzer-service 在采集期间临时创建，采集结束后自动删除。\n"
        "# 目的：让 LightDM 用原生自动登录为用户建立桌面会话。\n"
        "[Seat:*]\n"
        f"autologin-user={user}\n"
        "autologin-user-timeout=0\n"
    )


def parse_lightdm_state(stdout: str) -> LightDmState:
    """解析 ``LIGHTDM_STATE_COMMAND`` 的输出。缺项留空，不猜。"""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    fragments = tuple(sorted(item for item in values.get("fragment_dir", "").split() if item))
    return LightDmState(
        active=values.get("lightdm_active", "") == "active",
        config_sha256=values.get("lightdm_conf_sha256") or None,
        config_mode=values.get("lightdm_conf_mode") or None,
        fragments=fragments,
    )


def parse_sessions(stdout: str) -> tuple[LogindSession, ...]:
    """解析 ``SESSIONS_COMMAND`` 的输出。

    会话之间用 ``session=<id>`` 分隔；每个会话的字段来自 ``loginctl show-session`` 的
    ``Key=Value`` 行。缺 ``Id`` 的块整块丢弃，避免把半截输出当成会话。
    """
    sessions: list[LogindSession] = []
    current_id: str | None = None
    fields: dict[str, str] = {}

    def flush() -> None:
        if current_id is None or not fields.get("Id"):
            return
        sessions.append(
            LogindSession(
                session_id=fields.get("Id", current_id),
                user=fields.get("Name", ""),
                seat=fields.get("Seat", ""),
                type=fields.get("Type", ""),
                class_=fields.get("Class", ""),
                active=fields.get("Active", "") == "yes",
                state=fields.get("State", ""),
                remote=fields.get("Remote", "") == "yes",
            )
        )

    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("session="):
            flush()
            current_id = stripped.partition("=")[2].strip() or None
            fields = {}
            continue
        key, separator, value = stripped.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    flush()
    return tuple(sessions)


async def read_sessions(client: SshClient) -> tuple[LogindSession, ...]:
    """读一次全部 logind 会话。"""
    result = await client.run(SESSIONS_COMMAND)
    return parse_sessions(result.stdout)


async def trigger_desktop_login(ctx: PhaseContext) -> dict[str, Any]:
    """In Progress 的「触发桌面登录」步骤：装片段、重启 LightDM、等桌面会话。"""
    user = desktop_user(ctx.options)
    if user is None:
        record: dict[str, Any] = {
            "user": None,
            "applied": False,
            "fragment": None,
            "established": False,
            "failure_kind": None,
            "detail": (
                f"任务没有指定 {DESKTOP_USER_OPTION}，本次采集不做桌面登录；目标机器没有改动"
            ),
        }
        ctx.data["desktop_login"] = record
        return record

    timeout = desktop_login_timeout(ctx.options)
    async with ctx.connect() as client:
        return await apply_autologin(client, ctx, user, timeout=timeout)


async def apply_autologin(
    client: SshClient,
    ctx: PhaseContext,
    user: str,
    *,
    timeout: float = DEFAULT_DESKTOP_LOGIN_TIMEOUT_SECONDS,  # noqa: ASYNC109  等待会话建立的上限，由内部轮询落实
) -> dict[str, Any]:
    """在一条已建立的连接上完成「配置 → 重启 → 等会话 → 记录」。

    失败时先写 ``ctx.data["desktop_login"]`` 再抛 ``DesktopLoginError``：阶段会失败，但记录里
    留着到底停在哪一步，Post 仍会按改动清单把片段删掉。
    """
    state = await read_lightdm_state(client)
    if not state.active:
        raise DesktopLoginError(
            _FAILURE_NOT_TRIGGERED,
            "目标机器上的 LightDM 没有运行，无法用自动登录建立桌面会话"
            f"（systemctl is-active lightdm 的结果是 {state.as_mapping()['active']!r}）",
        )
    baseline = await read_sessions(client)
    baseline_ids = {session.session_id for session in baseline}
    record: dict[str, Any] = {
        "user": user,
        "applied": False,
        "fragment": AUTOLOGIN_FRAGMENT,
        "lightdm_before": state.as_mapping(),
        "baseline_sessions": [session.as_mapping() for session in baseline],
        "established": False,
        "failure_kind": None,
        "detail": None,
    }
    ctx.data["desktop_login"] = record

    try:
        await ensure_user_exists(client, user)
    except DesktopLoginError as exc:
        record["failure_kind"] = exc.kind
        record["detail"] = str(exc)
        ctx.data["desktop_login"] = record
        raise
    await stage_and_install(
        client,
        ctx,
        AUTOLOGIN_FRAGMENT,
        autologin_fragment(user).encode(),
        backup_name=AUTOLOGIN_BACKUP_NAME,
    )
    record["applied"] = True
    ctx.data["desktop_login"] = record

    failure = await _restart_lightdm(client)
    if failure is not None:
        record["failure_kind"] = _FAILURE_NOT_TRIGGERED
        record["detail"] = (
            f"重启 LightDM 失败，自动登录没有发生：{failure}。临时片段会在 Post 阶段删除"
        )
        ctx.data["desktop_login"] = record
        raise DesktopLoginError(_FAILURE_NOT_TRIGGERED, record["detail"])

    record["restarted"] = True
    ctx.data["desktop_login"] = record

    session, observed = await wait_for_desktop_session(
        client, user=user, baseline_ids=baseline_ids, timeout=timeout
    )
    record["observed_sessions"] = [item.as_mapping() for item in observed]
    if session is None:
        kind, detail = _classify_failure(user, baseline_ids, observed)
        record["failure_kind"] = kind
        record["detail"] = detail
        ctx.data["desktop_login"] = record
        raise DesktopLoginError(kind, detail, sessions=observed)

    record["established"] = True
    record["session"] = session.as_mapping()
    record["detail"] = f"LightDM 自动登录已建立桌面会话：{session.summary()}"
    ctx.data["desktop_login"] = record
    return record


async def read_lightdm_state(client: SshClient) -> LightDmState:
    """读采集前/后的 LightDM 状态。只读命令，不改任何文件。"""
    result = await client.run(LIGHTDM_STATE_COMMAND)
    return parse_lightdm_state(result.stdout)


async def ensure_user_exists(client: SshClient, user: str) -> None:
    """目标机器上必须真的有这个用户，否则 LightDM 会安静地退回登录界面。"""
    result = await client.run(f"id -u {shlex.quote(user)}", check=False)
    if not result.ok:
        raise DesktopLoginError(
            _FAILURE_NOT_TRIGGERED,
            f"目标机器上没有用户 {user}（id -u 退出码 {result.exit_status}），无法自动登录",
        )


async def wait_for_desktop_session(
    client: SshClient,
    *,
    user: str,
    baseline_ids: set[str],
    timeout: float,  # noqa: ASYNC109  等待会话建立的上限，由内部轮询落实
) -> tuple[LogindSession | None, tuple[LogindSession, ...]]:
    """轮询 logind，直到目标用户出现新的 seat0 活动图形会话，或等到超时。

    返回最后一次观察到的会话列表：超时时要把它们写进记录，才能解释「为什么判定没建立」。
    轮询间隔随超时缩放，测试里给一个很小的超时也不会白等固定的一秒。
    """
    deadline = time.monotonic() + timeout
    interval = min(1.0, max(0.02, timeout / 10.0))
    observed: tuple[LogindSession, ...] = ()
    while True:
        observed = await read_sessions(client)
        session = established_session(observed, user=user, baseline_ids=baseline_ids)
        if session is not None:
            return session, observed
        if time.monotonic() >= deadline:
            return None, observed
        await asyncio.sleep(interval)


def established_session(
    sessions: tuple[LogindSession, ...],
    *,
    user: str,
    baseline_ids: set[str],
) -> LogindSession | None:
    """在观察结果里找「这次自动登录新建立的、可用的」桌面会话。"""
    for session in sessions:
        if session.session_id in baseline_ids:
            continue
        if session.user != user:
            continue
        if session.is_desktop_ready:
            return session
    return None


def _classify_failure(
    user: str,
    baseline_ids: set[str],
    observed: tuple[LogindSession, ...],
) -> tuple[str, str]:
    """把超时结果分成两类失败，并写出可读原因。"""
    new_local = [
        session
        for session in observed
        if session.session_id not in baseline_ids and session.user == user and not session.remote
    ]
    if not new_local:
        before = "、".join(sorted(baseline_ids)) or "无"
        return (
            _FAILURE_NOT_TRIGGERED,
            f"LightDM 自动登录没有发生：重启 LightDM 后用户 {user} 没有出现新的本地会话"
            f"（重启前的会话：{before}）。可能是自动登录片段没有生效、LightDM 没有重启成功，"
            "或该用户被 LightDM 拒绝自动登录",
        )
    displayed = "；".join(session.summary() for session in new_local)
    return (
        _FAILURE_SESSION_NOT_ESTABLISHED,
        f"LightDM 自动登录已触发，但桌面会话没有建立：{displayed}。"
        "需要 seat0 上的 Active=yes 图形会话（Type 为 x11 / wayland / mir）",
    )


async def _restart_lightdm(client: SshClient) -> str | None:
    """重启 LightDM。成功返回 ``None``，失败返回一段不含口令的中文原因。"""
    try:
        result = await client.run_privileged("systemctl restart lightdm", check=False)
    except SshCommandError as exc:
        return _command_error_text(exc)
    if result.ok:
        return None
    return _command_error_text(result)


def _command_error_text(value: SshCommandError | object) -> str:
    """命令失败的简短描述，不含口令。"""
    stderr = getattr(value, "stderr", "") or ""
    status = getattr(value, "exit_status", None)
    detail = stderr.strip().replace("\n", " ")[:300]
    return f"退出码 {status}{('：' + detail) if detail else ''}"


__all__ = [
    "AUTOLOGIN_BACKUP_NAME",
    "AUTOLOGIN_FRAGMENT",
    "DEFAULT_DESKTOP_LOGIN_TIMEOUT_SECONDS",
    "DESKTOP_LOGIN_TIMEOUT_OPTION",
    "DESKTOP_USER_OPTION",
    "FRAGMENT_DIR",
    "LIGHTDM_CONFIG",
    "DesktopLoginError",
    "LightDmState",
    "LogindSession",
    "apply_autologin",
    "autologin_fragment",
    "desktop_login_timeout",
    "desktop_user",
    "established_session",
    "parse_lightdm_state",
    "parse_sessions",
    "read_sessions",
    "trigger_desktop_login",
    "wait_for_desktop_session",
]
