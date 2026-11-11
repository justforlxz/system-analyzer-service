"""诊断数据的时间基准：时钟域、单位换算与 Journal 时间戳归一化。

规范正文见 ``docs/time-base.md``，本模块是其中可以执行的那一部分：把一条 Journal 事件的
原始字段换算成它在 boot 时间轴上的位置。查询、排序、游标与分页属于 1.5 的范围，不在这里。

两条与 0.2 的接口有关的约定：

- 时间字段的单位是纳秒（``monotonic_ns``、``realtime_ns``），Journal 的原始字段是微秒，
  换算只有乘 1000 这一步，不补低位。
- Boot ID 在模型里用 ``/proc/sys/kernel/random/boot_id`` 的原值（小写、带连字符），
  journald 的 ``_BOOT_ID`` 是 32 位小写十六进制，比较前必须归一化。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from system_analyzer.domain.bootrec import ClockDomain

NS_PER_MICROSECOND = 1_000
"""Journal 的时间字段是微秒，模型字段是纳秒。"""

NANOSECONDS_LIMIT = 2**63 - 1
"""时间字段在 0.2 里是 int64，换算结果必须落在它之内，不静默溢出。"""

BOOT_ID_FIELD = "_BOOT_ID"
MONOTONIC_FIELD = "__MONOTONIC_TIMESTAMP"
REALTIME_FIELD = "__REALTIME_TIMESTAMP"
SOURCE_REALTIME_FIELD = "_SOURCE_REALTIME_TIMESTAMP"

NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MINUTE = 60 * NANOSECONDS_PER_SECOND
NANOSECONDS_PER_HOUR = 60 * NANOSECONDS_PER_MINUTE
NANOSECONDS_PER_DAY = 24 * NANOSECONDS_PER_HOUR

_BOOT_ID_HEX_LENGTH = 32
_BOOT_ID_SLICES = (8, 12, 16, 20)
"""32 位十六进制里插入连字符的位置，即 8-4-4-4-12。"""


def micros_to_ns(micros: int) -> int:
    """把 Journal 的微秒读数换成模型使用的纳秒读数。

    只有乘 1000 这一步：Journal 的时间戳精度是微秒，换算后低三位恒为 0。不要用别的字段
    （例如 ``_SOURCE_REALTIME_TIMESTAMP`` 的低位）去补，那是另一个读数，不是同一次测量的
    更高精度。

    超出 ``int64`` 纳秒范围的读数直接拒绝：0.2 把时间字段定成 ``int64``，静默溢出会写进去一个
    模型装不下的值（``2**63 - 1`` 纳秒约等于 292 年）。
    """
    if micros < 0:
        raise ValueError(f"时间读数不能为负：{micros}")
    if micros > NANOSECONDS_LIMIT // NS_PER_MICROSECOND:
        raise ValueError(
            f"时间读数 {micros} 微秒换算成纳秒后超出 int64 范围（上限 {NANOSECONDS_LIMIT} 纳秒）"
        )
    return micros * NS_PER_MICROSECOND


def normalize_boot_id(raw: str) -> str:
    """把 Boot ID 统一成 ``/proc/sys/kernel/random/boot_id`` 的形式：小写、8-4-4-4-12。

    journald 的 ``_BOOT_ID`` 是 32 位小写十六进制、不带连字符；``/proc`` 与模型里的是带连字符
    的 UUID 形式。两者是同一个值的两种写法，跨来源比较前必须归一化，否则会得出「两次启动」
    这种错误结论。
    """
    hex_only = raw.strip().lower().replace("-", "")
    if len(hex_only) != _BOOT_ID_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in hex_only
    ):
        raise ValueError(f"不是合法的 Boot ID：{raw!r}")
    parts: list[str] = []
    previous = 0
    for position in _BOOT_ID_SLICES:
        parts.append(hex_only[previous:position])
        previous = position
    parts.append(hex_only[previous:])
    return "-".join(parts)


def format_boot_relative(nanoseconds: int) -> str:
    """把 boot 内位置写成 ``<天>d HH:MM:SS.nnnnnnnnn``，与 ``-o short-monotonic`` 对得上。

    ``[175830.828563]`` 这种输出就是「启动后 175830.828563 秒」，换算成本函数的
    ``2d 00:50:30.828563000``。
    """
    if nanoseconds < 0:
        raise ValueError(f"boot 内位置不能为负：{nanoseconds}")
    days, rest = divmod(nanoseconds, NANOSECONDS_PER_DAY)
    hours, rest = divmod(rest, NANOSECONDS_PER_HOUR)
    minutes, rest = divmod(rest, NANOSECONDS_PER_MINUTE)
    seconds, fraction = divmod(rest, NANOSECONDS_PER_SECOND)
    return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:09d}"


def format_utc(nanoseconds: int) -> str:
    """把 Unix 纪元纳秒写成 UTC 的 ISO 8601（微秒精度，与来源的精度一致）。"""
    seconds, fraction = divmod(nanoseconds, NANOSECONDS_PER_SECOND)
    moment = datetime.fromtimestamp(seconds, UTC).replace(microsecond=fraction // 1_000)
    return moment.isoformat(timespec="microseconds")


@dataclass(frozen=True)
class JournalEventPosition:
    """一条 Journal 事件在 boot 时间轴上的位置。

    ``boot_start_realtime_ns`` 是「单调时钟零点在墙钟上的位置」，由本条记录的
    ``realtime_ns - monotonic_ns`` 算出。它是 Boot 实体 ``started_realtime_ns`` 的估计值：
    两者在微秒舍入范围内应当相等，差得更多说明墙钟在启动过程中被调整过。
    """

    boot_id: str
    clock_domain: ClockDomain
    monotonic_ns: int | None
    realtime_ns: int | None
    source_realtime_ns: int | None
    boot_start_realtime_ns: int | None

    @property
    def boot_relative(self) -> str | None:
        if self.monotonic_ns is None:
            return None
        return format_boot_relative(self.monotonic_ns)

    @property
    def realtime_utc(self) -> str | None:
        if self.realtime_ns is None:
            return None
        return format_utc(self.realtime_ns)


def journal_event_position(fields: Mapping[str, str]) -> JournalEventPosition:
    """把一条 Journal 事件的原始字段换算成它在 boot 时间轴上的位置。

    ``fields`` 是 ``journalctl -o export`` 或 ``journalctl -o json`` 给出的原始键值，值都是
    字符串（JSON 输出里也是字符串）。换算是纯函数，不读文件、不访问网络。

    同时缺 ``__MONOTONIC_TIMESTAMP`` 与 ``__REALTIME_TIMESTAMP`` 时无法定位这条事件，
    抛 ``ValueError``；``_SOURCE_REALTIME_TIMESTAMP`` 不参与定位，只原样带出来。
    """
    boot_id = normalize_boot_id(_required(fields, BOOT_ID_FIELD))
    monotonic_ns = _optional_ns(fields, MONOTONIC_FIELD)
    realtime_ns = _optional_ns(fields, REALTIME_FIELD)
    source_realtime_ns = _optional_ns(fields, SOURCE_REALTIME_FIELD)

    if monotonic_ns is None and realtime_ns is None:
        raise ValueError(
            "这条事件既没有 __MONOTONIC_TIMESTAMP 也没有 __REALTIME_TIMESTAMP，无法定位到时间轴"
        )
    clock_domain = (
        ClockDomain.BOOT_MONOTONIC if monotonic_ns is not None else ClockDomain.WALL_REALTIME
    )
    boot_start_realtime_ns = (
        realtime_ns - monotonic_ns if realtime_ns is not None and monotonic_ns is not None else None
    )
    return JournalEventPosition(
        boot_id=boot_id,
        clock_domain=clock_domain,
        monotonic_ns=monotonic_ns,
        realtime_ns=realtime_ns,
        source_realtime_ns=source_realtime_ns,
        boot_start_realtime_ns=boot_start_realtime_ns,
    )


def _required(fields: Mapping[str, str], name: str) -> str:
    value = fields.get(name)
    if value is None or value == "":
        raise ValueError(f"缺少 {name}，无法确定这条事件属于哪一次启动")
    return value


def _optional_ns(fields: Mapping[str, str], name: str) -> int | None:
    value = fields.get(name)
    if value is None or value == "":
        return None
    try:
        micros = int(value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是微秒整数，实际是 {value!r}") from error
    return micros_to_ns(micros)
