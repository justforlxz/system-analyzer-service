"""统一时间线查询与回放引擎（任务 1.5）。

三个模块的分工：

- ``models``：读侧的数据形状：条目、轨道、状态、游标、错误类型。
- ``engine``：查询与拼装，只通过 ``TimelineReader`` 取数据。
- 接口说明见 ``docs/timeline-query.md``；排序键与收尾规则见 ``docs/time-base.md``。

本包不解析 ``.bootrec``（1.3）、不提供 HTTP 接口（1.6）、不生成分析结论（2.x）、不做界面（4.x）。
"""

from system_analyzer.timeline.engine import (
    TIMELINE_KINDS,
    TimelineEngine,
    TimelineReader,
)
from system_analyzer.timeline.models import (
    AxisItem,
    ClockDrift,
    MetricPoint,
    ResourceStatus,
    TimeCursor,
    TimelineBootNotFoundError,
    TimelineError,
    TimelineEvent,
    TimelineKindError,
    TimelineRangeError,
    TimelineScopeError,
    TimelineSpan,
    TimelineState,
    TimelineTracks,
    Track,
    TrackState,
    TrackStatus,
)

__all__ = [
    "TIMELINE_KINDS",
    "AxisItem",
    "ClockDrift",
    "MetricPoint",
    "ResourceStatus",
    "TimeCursor",
    "TimelineBootNotFoundError",
    "TimelineEngine",
    "TimelineError",
    "TimelineEvent",
    "TimelineKindError",
    "TimelineRangeError",
    "TimelineReader",
    "TimelineScopeError",
    "TimelineSpan",
    "TimelineState",
    "TimelineTracks",
    "Track",
    "TrackState",
    "TrackStatus",
]
