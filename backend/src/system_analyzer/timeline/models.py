"""时间线查询与回放的数据形状。

本模块只定义读侧的输出结构，不做任何查询：查询与拼装在 ``engine`` 模块里。
字段与语义以 ``docs/data-model.md``（统一记录与六个数据对象）、``docs/time-base.md``
（排序键、开放区间收尾、漂移）与 ``docs/timeline-query.md``（本任务的接口说明）为准。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from system_analyzer.domain.bootrec import RecordKind

WALL_CLOCK_UNRELIABLE_DRIFT_NS = 1_000_000_000
"""建议阈值：boot 级漂移极差超过 1 秒时，不把墙钟换算用于跨启动比较（time-base 第 4.4 节）。"""


class TimelineError(Exception):
    """时间线查询的基类异常。"""


class TimelineBootNotFoundError(TimelineError):
    """这次启动不在库里：没有可回放的时间轴。"""


class TimelineRangeError(TimelineError):
    """时间范围非法，例如起点晚于终点。"""


class TimelineKindError(TimelineError):
    """``kinds`` 里出现本任务不产出轨道的记录种类。"""


class TimelineScopeError(TimelineError):
    """读到的记录不属于查询的那次启动；跨启动混排是禁止的（time-base 第 3.2、10 节）。"""


class TrackStatus(StrEnum):
    """一条轨道的数据可用性。三种状态互斥，含义见 ``docs/timeline-query.md``。"""

    AVAILABLE = "available"  # 有记录：正常画线
    UNAVAILABLE = "unavailable"  # 没有记录，且指标流被标成不可用：显示原因，不画成 0
    NO_DATA = "no_data"  # 没有记录，也没有任何采集声明它不可用：按缺口处理


@dataclass(frozen=True, slots=True)
class ResourceStatus:
    """整次启动的资源数据可用性，来自各次采集的 manifest 声明。"""

    # available 为 None 表示没有任何采集声明过；False 时由 reasons 说明原因
    available: bool | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClockDrift:
    """boot 级的启动锚点与漂移，供界面决定是否打「墙钟不可靠」标记（time-base 第 4.4 节）。"""

    # 锚点候选的中位数（纳秒）；没有候选时为 None
    anchor_ns: int | None
    # 锚点候选的极差（纳秒），即漂移；没有候选时为 None
    drift_ns: int | None
    # 参与计算的候选个数
    anchor_count: int

    @property
    def unreliable(self) -> bool:
        """漂移超过建议阈值：墙钟换算只适合显示时间文字，不用于跨启动比较。"""
        return self.drift_ns is not None and self.drift_ns > WALL_CLOCK_UNRELIABLE_DRIFT_NS


@dataclass(frozen=True, slots=True)
class AxisItem:
    """时间轴上的一个条目。三种轨道共用这组定位字段。"""

    record_id: str
    collection_id: str
    # boot 内位置；只有墙钟的记录为 None
    monotonic_ns: int | None
    # 墙钟时刻；没有墙钟读数的记录为 None
    realtime_ns: int | None


@dataclass(frozen=True, slots=True)
class TimelineEvent(AxisItem):
    """瞬时事件。一个时刻，没有持续长度。"""

    entity: str | None
    source_plugin: str
    stream_id: str
    event_type: str
    severity: str
    message: str | None
    attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TimelineSpan(AxisItem):
    """持续过程。``monotonic_ns`` 是开始时刻，结束时刻见 ``end_*_ns``。"""

    entity: str | None
    source_plugin: str
    stream_id: str
    span_type: str
    state: str | None
    # 结束时刻：记录声明过就用声明的值，否则按所属采集的窗口终点收尾
    end_monotonic_ns: int | None
    end_realtime_ns: int | None
    # True 表示记录本身声明了结束（任一时钟域），False 表示开放区间
    end_is_declared: bool
    # 开放区间按哪次采集的窗口收尾；未收尾时为 None
    closed_by: str | None
    attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MetricPoint(AxisItem):
    """指标流上的一个采样点。没有数据时根本没有这个条目，不会补 0。"""

    entity: str | None
    source_plugin: str
    stream_id: str
    name: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class Track:
    """一条轨道：记录种类 + 逻辑流 + 实体 唯一确定。

    ``track_id`` 是对外的稳定标识，调用方按不透明字符串处理：它只用于区分轨道，不用于拼接
    或解析。同一个 ``track_id`` 在不同次查询里指向同一条轨道。
    """

    track_id: str
    kind: RecordKind
    entity: str | None
    stream_id: str | None
    source_plugin: str | None
    status: TrackStatus
    # status 为 unavailable 时非空：这条流没有数据的原因
    unavailable_reason: str | None
    # 指标流的指标名与单位；其它轨道为 None
    name: str | None
    unit: str | None
    items: tuple[AxisItem, ...]


@dataclass(frozen=True, slots=True)
class TimelineTracks:
    """一次启动在给定时间范围内的轨道数据，供时间线界面直接渲染。"""

    machine_id: str
    boot_id: str
    # 本次查询的时间范围（闭区间）；None 表示该端无界
    window_start_ns: int | None
    window_end_ns: int | None
    # 这条轴覆盖的时间范围（整次启动的数据，不受查询范围影响）；没有任何数据时都是 None
    axis_start_ns: int | None
    axis_end_ns: int | None
    tracks: tuple[Track, ...]
    # 全部轨道按排序键 (monotonic_ns, collection_id, record_id) 排好的同一条轴
    items: tuple[AxisItem, ...]
    # 只有墙钟的记录：位置未知，不与上面的轴混排（time-base 第 2、10 节）
    wall_clock_items: tuple[AxisItem, ...]
    resources: ResourceStatus
    clock: ClockDrift


@dataclass(frozen=True, slots=True)
class TrackState:
    """某一时刻一条轨道的状态。"""

    track_id: str
    kind: RecordKind
    entity: str | None
    status: TrackStatus
    unavailable_reason: str | None
    # 该时刻或之前最近的一个采样点（重叠观测时只取最靠近游标的这条）
    metric: MetricPoint | None
    # 该时刻正在进行的区间
    spans: tuple[TimelineSpan, ...]
    # 该时刻或之前最近的一个事件
    last_event: TimelineEvent | None


@dataclass(frozen=True, slots=True)
class TimelineState:
    """一次启动在指定时刻的状态快照。"""

    machine_id: str
    boot_id: str
    monotonic_ns: int
    tracks: tuple[TrackState, ...]
    resources: ResourceStatus
    clock: ClockDrift


@dataclass(frozen=True, slots=True)
class TimeCursor:
    """统一时间游标：位置落在哪一纳秒，以及前后可停靠的时刻。

    ``start_ns`` / ``end_ns`` 是这条轴覆盖的范围。没有任何数据时，五个时刻字段都是 None。
    """

    machine_id: str
    boot_id: str
    start_ns: int | None
    end_ns: int | None
    position_ns: int | None
    # 位置之前最近的一个数据时刻；已在起点时为 None
    previous_ns: int | None
    # 位置之后最近的一个数据时刻；已在终点时为 None
    next_ns: int | None
