"""统一时间线查询与回放引擎。

职责：把一次启动的多份采集数据拼成一条按 boot 内位置排列的时间轴，回答三类问题：

- 时间范围里各轨道有什么（``TimelineEngine.tracks``）；
- 某一时刻各轨道是什么状态（``TimelineEngine.state_at``）；
- 时间游标停在哪、前后还能停到哪（``TimelineEngine.cursor`` / ``step_cursor``）。

规则全部来自 ``docs/time-base.md``，本模块不另立口径：

- 排序键固定为 ``(monotonic_ns, collection_id, record_id)``（第 7.1 节）。
- 跨启动不排在同一序列里：查询必须给定 ``(machine_id, boot_id)``，读到的记录若不属于这次
  启动就拒绝（第 3.2、10 节）。
- ``Span`` 的开放区间用所属 collection 的采集窗口终点收尾，不修改记录、不用查询时刻收尾
  （第 6 节）。
- 只有墙钟的记录单独返回，不进入 boot 内排序（第 2、10 节）。

读侧只通过 ``TimelineReader`` 取数据（``DiagnosticStore`` 已经满足），不写 SQL、不改存储层。

本模块不做分析结论（2.x）、不做界面（4.x）、不提供 HTTP 接口（1.6）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    MetricStreamEntity,
    Record,
    RecordKind,
    Severity,
)
from system_analyzer.storage import CollectionState, ImportStatus
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

TIMELINE_KINDS: tuple[RecordKind, ...] = (RecordKind.EVENT, RecordKind.SPAN, RecordKind.METRIC)
"""本任务产出轨道的记录种类。Profile、Annotation、Artifact 的展示属于 2.x 与 4.x。"""

DEFAULT_AXIS_CACHE_SECONDS = 10.0
"""整条轴的进程内缓存时间（秒）。

一次真实采集可能有数万条 Journal 记录，读取并排序整次启动的数据需要数秒。回放时每一帧都会
调用 ``state_at``，窗口变化也会反复调用 ``tracks``；如果没有缓存，界面每操作一次都要重新读
一遍全部记录。缓存只保存读出来的轴，不改变任何字段的推导规则。

导入完成后数据会变，但 API 与 Worker 是两个进程，没有可靠的跨进程失效通知。这里用较短的
存活时间限制陈旧窗口：最坏情况下界面在导入完成后的十秒内仍看到上一次的轴，刷新即可恢复。
"""

_KIND_BY_VALUE: dict[str, RecordKind] = {kind.value: kind for kind in TIMELINE_KINDS}
"""``kinds`` 的白名单：先查这里再转换，拼错的值也走本模块的异常（见 ``_wanted_kinds``）。"""

_BOOT_MONOTONIC = ClockDomain.BOOT_MONOTONIC
_WALL_REALTIME = ClockDomain.WALL_REALTIME


class TimelineReader(Protocol):
    """引擎需要的只读接口。``DiagnosticStore`` 已经满足，测试里用固定数据的替身。

    刻意只列出四个方法：时间线不改存储层，也不越权读别的表。
    """

    async def get_boot(self, machine_id: str, boot_id: str) -> BootEntity | None: ...

    async def list_collections(
        self,
        *,
        machine_id: str | None = None,
        boot_id: str | None = None,
        status: ImportStatus | None = None,
    ) -> list[CollectionState]: ...

    async def list_records_in_boot(
        self,
        machine_id: str,
        boot_id: str,
        *,
        clock_domain: ClockDomain = ClockDomain.BOOT_MONOTONIC,
        start_ns: int | None = None,
        end_ns: int | None = None,
        kinds: Sequence[RecordKind] | None = None,
        limit: int | None = None,
    ) -> list[Record]: ...

    async def list_metric_streams(
        self, machine_id: str, boot_id: str
    ) -> list[MetricStreamEntity]: ...


class TimelineEngine:
    """统一时间线查询与回放。"""

    def __init__(
        self,
        reader: TimelineReader,
        *,
        axis_cache_seconds: float = DEFAULT_AXIS_CACHE_SECONDS,
    ) -> None:
        self._reader = reader
        self._axis_cache_seconds = max(0.0, axis_cache_seconds)
        self._axis_cache: dict[tuple[str, str], tuple[float, _Axis]] = {}

    # ------------------------------------------------------------------
    # 轨道数据
    # ------------------------------------------------------------------

    async def tracks(
        self,
        machine_id: str,
        boot_id: str,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        kinds: Sequence[RecordKind] | None = None,
        include_attributes: bool = True,
    ) -> TimelineTracks:
        """一次启动在 ``[start_ns, end_ns]`` 内的轨道数据。

        时间范围是闭区间：正好落在起止点上的记录包含在内。``None`` 表示该端无界。
        起点晚于终点时抛 ``TimelineRangeError``，不做静默交换。

        范围只筛条目，不影响轴的起止，也不影响开放区间的收尾时刻：同一份数据在不同范围里
        回放，同一条区间的长度始终一样（time-base 第 6 节禁止用查询时刻收尾）。

        与窗口有交集就被返回：一条区间从窗口之前开始、在窗口之中结束，仍然在结果里，否则
        界面会把「当时一直活着的服务」画成不存在。

        ``include_attributes`` 为 false 时，事件与区间的 ``attributes`` 返回空对象。界面只用
        条目自身的定位与展示字段，而 Journal 事件的 ``attributes`` 带着整份原始字段，真实采集
        下会让响应体膨胀到数百兆。默认仍为 true，保持 1.5 的接口形状不变。
        """
        _check_window(start_ns, end_ns)
        window = _Window(start_ns, end_ns)
        wanted = _wanted_kinds(kinds)
        axis = await self._axis(machine_id, boot_id)
        wanted_items = tuple(item for item in axis.items if _kind_of(item) in wanted)
        if not include_attributes:
            wanted_items = tuple(_without_attributes(item) for item in wanted_items)
        tracks = tuple(track for track in axis.tracks if track.kind in wanted)
        if not include_attributes:
            tracks = tuple(
                replace(track, items=tuple(_without_attributes(item) for item in track.items))
                for track in tracks
            )
        wall_items = tuple(item for item in axis.wall_clock_items if _kind_of(item) in wanted)
        if not include_attributes:
            wall_items = tuple(_without_attributes(item) for item in wall_items)
        return TimelineTracks(
            machine_id=machine_id,
            boot_id=boot_id,
            window_start_ns=start_ns,
            window_end_ns=end_ns,
            axis_start_ns=axis.start_ns,
            axis_end_ns=axis.end_ns,
            tracks=tuple(
                replace(
                    track,
                    items=tuple(item for item in track.items if window.contains(item)),
                )
                for track in tracks
            ),
            items=tuple(item for item in wanted_items if window.contains(item)),
            # 只有墙钟的记录没有 boot 内位置，跟单调时间范围没有可比性，按原样返回
            wall_clock_items=wall_items,
            resources=axis.resources,
            clock=axis.clock,
        )

    # ------------------------------------------------------------------
    # 指定时刻的状态
    # ------------------------------------------------------------------

    async def state_at(self, machine_id: str, boot_id: str, monotonic_ns: int) -> TimelineState:
        """一次启动在 ``monotonic_ns`` 时刻各轨道的状态。

        返回每条轨道在该时刻或之前最近的一个采样点、正在进行的区间、以及最近的一个事件。
        两次采集时间重叠时，采样点只取最靠近游标的那一条（time-base 第 7.2 节）。

        时刻早于全部数据时，状态为空，但轨道本身仍然列出：没有数据不等于这条轨道不存在。
        """
        axis = await self._axis(machine_id, boot_id)
        return TimelineState(
            machine_id=machine_id,
            boot_id=boot_id,
            monotonic_ns=monotonic_ns,
            tracks=tuple(_track_state(track, monotonic_ns) for track in axis.tracks),
            resources=axis.resources,
            clock=axis.clock,
        )

    # ------------------------------------------------------------------
    # 统一时间游标
    # ------------------------------------------------------------------

    async def cursor(
        self, machine_id: str, boot_id: str, *, at_ns: int | None = None
    ) -> TimeCursor:
        """时间游标。``at_ns`` 缺省停在轴的起点，超出轴时夹到起止点。

        可停靠的时刻是数据发生变化的所有时刻：事件与采样点的时刻、区间的起止时刻。
        """
        axis = await self._axis(machine_id, boot_id)
        times = axis.stop_times
        if not times:
            return TimeCursor(
                machine_id=machine_id,
                boot_id=boot_id,
                start_ns=None,
                end_ns=None,
                position_ns=None,
                previous_ns=None,
                next_ns=None,
            )
        start_ns, end_ns = times[0], times[-1]
        position = start_ns if at_ns is None else min(max(at_ns, start_ns), end_ns)
        return TimeCursor(
            machine_id=machine_id,
            boot_id=boot_id,
            start_ns=start_ns,
            end_ns=end_ns,
            position_ns=position,
            previous_ns=_neighbour(times, position, forward=False),
            next_ns=_neighbour(times, position, forward=True),
        )

    async def step_cursor(self, cursor: TimeCursor, *, forward: bool = True) -> TimeCursor:
        """把游标挪到前后最近的一个数据时刻；已经在两端时原样返回。"""
        target = cursor.next_ns if forward else cursor.previous_ns
        if target is None:
            return cursor
        return await self.cursor(cursor.machine_id, cursor.boot_id, at_ns=target)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _axis(self, machine_id: str, boot_id: str) -> _Axis:
        """一次启动的整条轴，带短时间的进程内缓存。

        轨道筛选与时间窗口都不参与缓存键：轴的起止、开放区间收尾、锚点与漂移、可停靠时刻
        必须与筛选和查询范围无关（time-base 第 6、4.4 节）。缓存的就是这份与查询无关的轴，
        调用方再按自己的 ``kinds`` 与窗口裁剪。
        """
        key = (machine_id, boot_id)
        now = time.monotonic()
        cached = self._axis_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        boot = await self._reader.get_boot(machine_id, boot_id)
        if boot is None:
            raise TimelineBootNotFoundError(
                f"启动 {machine_id}/{boot_id} 不在库里，没有可回放的时间轴"
            )
        records = await self._reader.list_records_in_boot(
            machine_id, boot_id, kinds=list(TIMELINE_KINDS)
        )
        wall_records = await self._reader.list_records_in_boot(
            machine_id, boot_id, clock_domain=_WALL_REALTIME, kinds=list(TIMELINE_KINDS)
        )
        streams = await self._reader.list_metric_streams(machine_id, boot_id)
        collections = await self._reader.list_collections(machine_id=machine_id, boot_id=boot_id)
        _require_scope(records, machine_id, boot_id)
        _require_scope(wall_records, machine_id, boot_id)

        boot_windows = _collection_windows(records, _BOOT_MONOTONIC)
        axis_items = _sorted_items(_items(records, boot_windows, _BOOT_MONOTONIC), _BOOT_MONOTONIC)
        wall_windows = _collection_windows(wall_records, _WALL_REALTIME)
        wall_items = _sorted_items(
            _items(wall_records, wall_windows, _WALL_REALTIME), _WALL_REALTIME
        )
        axis = _Axis(
            tracks=_tracks(axis_items, streams),
            items=tuple(axis_items),
            wall_clock_items=tuple(wall_items),
            start_ns=_axis_start(axis_items),
            end_ns=_axis_end(axis_items),
            stop_times=_stop_times(axis_items),
            resources=_resources(collections),
            clock=_clock(boot, records),
        )
        if self._axis_cache_seconds > 0:
            self._axis_cache[key] = (now + self._axis_cache_seconds, axis)
            self._prune_axis_cache(now)
        return axis

    def _prune_axis_cache(self, now: float) -> None:
        """丢掉过期条目；条目数超过上限时再丢掉最早到期的那一条。"""
        expired = [key for key, (expires, _axis) in self._axis_cache.items() if expires <= now]
        for key in expired:
            self._axis_cache.pop(key, None)
        if len(self._axis_cache) <= 16:
            return
        oldest = min(self._axis_cache, key=lambda key: self._axis_cache[key][0])
        self._axis_cache.pop(oldest, None)


# ----------------------------------------------------------------------
# 一次查询的中间结果
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Axis:
    tracks: tuple[Track, ...]
    items: tuple[AxisItem, ...]
    wall_clock_items: tuple[AxisItem, ...]
    start_ns: int | None
    end_ns: int | None
    stop_times: tuple[int, ...]
    resources: ResourceStatus
    clock: ClockDrift


@dataclass(frozen=True, slots=True)
class _Window:
    start_ns: int | None
    end_ns: int | None

    def contains(self, item: AxisItem) -> bool:
        if item.monotonic_ns is None:  # pragma: no cover - 只有墙钟的条目不走这条轴
            return False
        if isinstance(item, TimelineSpan):
            if not _within(item.monotonic_ns, None, self.end_ns):
                return False
            # 结束时刻未知时按「从开始起一直有效」处理，与 state_at 一致
            stop = item.end_monotonic_ns
            return stop is None or _within(stop, self.start_ns, None)
        return _within(item.monotonic_ns, self.start_ns, self.end_ns)


def _within(value: int, lower: int | None, upper: int | None) -> bool:
    """闭区间判断，两端都可以无界。"""
    if lower is not None and value < lower:
        return False
    return not (upper is not None and value > upper)


def _check_window(start_ns: int | None, end_ns: int | None) -> None:
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise TimelineRangeError(f"时间范围起点 {start_ns} 晚于终点 {end_ns}，空区间不查询")


def _wanted_kinds(kinds: Sequence[RecordKind] | str | None) -> tuple[RecordKind, ...]:
    """把 ``kinds`` 归一成时间线支持的种类；拼错的值也抛本模块的异常，不泄漏 ``ValueError``。"""
    if not kinds:
        return TIMELINE_KINDS
    wanted: list[RecordKind] = []
    for kind in kinds:
        value = getattr(kind, "value", kind)
        resolved = _KIND_BY_VALUE.get(value)
        if resolved is None:
            allowed = "、".join(item.value for item in TIMELINE_KINDS)
            raise TimelineKindError(f"{value} 不在时间线轨道范围内；本任务只产出 {allowed}")
        if resolved not in wanted:
            wanted.append(resolved)
    return tuple(wanted)


def _kind_of(item: AxisItem) -> RecordKind:
    if isinstance(item, MetricPoint):
        return RecordKind.METRIC
    if isinstance(item, TimelineEvent):
        return RecordKind.EVENT
    return RecordKind.SPAN


def _without_attributes(item: AxisItem) -> AxisItem:
    """去掉事件与区间的 ``attributes``，保留定位与展示字段。"""
    if isinstance(item, (TimelineEvent, TimelineSpan)):
        return replace(item, attributes={})
    return item


# ----------------------------------------------------------------------
# 记录到条目
# ----------------------------------------------------------------------


def _collection_windows(records: Sequence[Record], domain: ClockDomain) -> dict[str, int]:
    """每次采集的窗口终点：该采集内所有记录时间字段（含 ``end_*_ns``）的最大值。

    time-base 第 6 节规定开放区间用它收尾。窗口取整次采集的数据，不取查询范围，所以同一份
    数据在任何时候回放，收尾时刻都一样。
    """
    time_attr, end_key = _time_fields(domain)
    windows: dict[str, int] = {}
    for record in records:
        time_ns = getattr(record, time_attr)
        if time_ns is None:
            continue
        end_ns = _int_or_none(record.payload.get(end_key))
        stop = time_ns if end_ns is None else max(time_ns, end_ns)
        current = windows.get(record.collection_id)
        windows[record.collection_id] = stop if current is None else max(current, stop)
    return windows


def _time_fields(domain: ClockDomain) -> tuple[str, str]:
    if domain is _BOOT_MONOTONIC:
        return "monotonic_ns", "end_monotonic_ns"
    return "realtime_ns", "end_realtime_ns"


def _items(
    records: Sequence[Record], windows: dict[str, int], domain: ClockDomain
) -> list[AxisItem]:
    items: list[AxisItem] = []
    for record in records:
        item = _item(record, windows, domain)
        if item is not None:
            items.append(item)
    return items


def _item(record: Record, windows: dict[str, int], domain: ClockDomain) -> AxisItem | None:
    payload = record.payload
    if record.kind is RecordKind.EVENT:
        return TimelineEvent(
            record_id=record.record_id,
            collection_id=record.collection_id,
            monotonic_ns=record.monotonic_ns,
            realtime_ns=record.realtime_ns,
            entity=record.entity,
            source_plugin=record.source_plugin,
            stream_id=record.stream_id,
            event_type=payload["event_type"],
            severity=payload.get("severity", Severity.INFO.value),
            message=payload.get("message"),
            attributes=dict(payload.get("attributes") or {}),
        )
    if record.kind is RecordKind.SPAN:
        return _span(record, windows, domain)
    if record.kind is RecordKind.METRIC:
        return MetricPoint(
            record_id=record.record_id,
            collection_id=record.collection_id,
            monotonic_ns=record.monotonic_ns,
            realtime_ns=record.realtime_ns,
            entity=record.entity,
            source_plugin=record.source_plugin,
            stream_id=record.stream_id,
            name=payload["name"],
            value=float(payload["value"]),
            unit=payload["unit"],
        )
    return None


def _span(record: Record, windows: dict[str, int], domain: ClockDomain) -> TimelineSpan:
    payload = record.payload
    end_monotonic_ns = _int_or_none(payload.get("end_monotonic_ns"))
    end_realtime_ns = _int_or_none(payload.get("end_realtime_ns"))
    # 任一时钟域声明了结束，这条区间就是已闭合的；不把墙钟收尾换算成单调时间（第 6 节）
    end_is_declared = end_monotonic_ns is not None or end_realtime_ns is not None
    closed_by = None
    if not end_is_declared:
        time_attr, _ = _time_fields(domain)
        start_ns = getattr(record, time_attr)
        window_end = windows.get(record.collection_id)
        if window_end is not None and (start_ns is None or window_end >= start_ns):
            if domain is _BOOT_MONOTONIC:
                end_monotonic_ns = window_end
            else:
                end_realtime_ns = window_end
            closed_by = record.collection_id
    return TimelineSpan(
        record_id=record.record_id,
        collection_id=record.collection_id,
        monotonic_ns=record.monotonic_ns,
        realtime_ns=record.realtime_ns,
        entity=record.entity,
        source_plugin=record.source_plugin,
        stream_id=record.stream_id,
        span_type=payload["span_type"],
        state=payload.get("state"),
        end_monotonic_ns=end_monotonic_ns,
        end_realtime_ns=end_realtime_ns,
        end_is_declared=end_is_declared,
        closed_by=closed_by,
        attributes=dict(payload.get("attributes") or {}),
    )


def _sorted_items(items: Sequence[AxisItem], domain: ClockDomain) -> tuple[AxisItem, ...]:
    """按 time-base 第 7.1 节的排序键排：``(时刻, collection_id, record_id)``。"""
    attr = "monotonic_ns" if domain is _BOOT_MONOTONIC else "realtime_ns"
    return tuple(
        sorted(
            items,
            key=lambda item: (
                _sort_time(getattr(item, attr)),
                item.collection_id,
                item.record_id,
            ),
        )
    )


# ----------------------------------------------------------------------
# 轨道
# ----------------------------------------------------------------------


def _tracks(items: Sequence[AxisItem], streams: Sequence[MetricStreamEntity]) -> tuple[Track, ...]:
    """把条目分成轨道。

    指标轨道以 ``metric_stream`` 实体为准，所以「声明过但没有数据的流」也能作为一条轨道出现；
    其它种类的轨道按 ``(记录种类, 逻辑流, 实体, 来源插件)`` 分组，这是 data-model 里序列的定义。
    """
    points_by_entity: dict[str | None, list[MetricPoint]] = {}
    lattice: dict[tuple[RecordKind, str | None, str | None, str | None], list[AxisItem]] = {}
    for item in items:
        if isinstance(item, MetricPoint):
            points_by_entity.setdefault(item.entity, []).append(item)
        else:
            lattice.setdefault(
                (_kind_of(item), item.entity, item.stream_id, item.source_plugin), []
            ).append(item)

    tracks: list[Track] = []
    for stream in streams:
        points = points_by_entity.pop(stream.entity_id, [])
        first = points[0] if points else None
        tracks.append(
            Track(
                track_id=_track_id(
                    RecordKind.METRIC,
                    first.source_plugin if first else None,
                    first.stream_id if first else None,
                    stream.entity_id,
                ),
                kind=RecordKind.METRIC,
                entity=stream.entity_id,
                stream_id=first.stream_id if first else None,
                source_plugin=first.source_plugin if first else None,
                # 有记录的窗口就显示数据：别因为另一次采集把这条流标成不可用就隐藏采样点
                status=(
                    TrackStatus.AVAILABLE
                    if points
                    else (TrackStatus.NO_DATA if stream.available else TrackStatus.UNAVAILABLE)
                ),
                unavailable_reason=(
                    None if points or stream.available else stream.unavailable_reason
                ),
                name=stream.name,
                unit=stream.unit,
                items=tuple(points),
            )
        )
    # 指标流实体覆盖不到的采样点（正常数据不会出现）：有记录就是可用，不丢数据
    for entity_id, points in points_by_entity.items():
        first = points[0]
        tracks.append(
            Track(
                track_id=_track_id(
                    RecordKind.METRIC, first.source_plugin, first.stream_id, entity_id
                ),
                kind=RecordKind.METRIC,
                entity=entity_id,
                stream_id=first.stream_id,
                source_plugin=first.source_plugin,
                status=TrackStatus.AVAILABLE,
                unavailable_reason=None,
                name=first.name,
                unit=first.unit,
                items=tuple(points),
            )
        )
    for (kind, entity, stream_id, source_plugin), group in lattice.items():
        tracks.append(
            Track(
                track_id=_track_id(kind, source_plugin, stream_id, entity),
                kind=kind,
                entity=entity,
                stream_id=stream_id,
                source_plugin=source_plugin,
                status=TrackStatus.AVAILABLE,
                unavailable_reason=None,
                name=None,
                unit=None,
                items=tuple(group),
            )
        )
    return tuple(sorted(tracks, key=lambda track: track.track_id))


def _track_id(
    kind: RecordKind, source_plugin: str | None, stream_id: str | None, entity: str | None
) -> str:
    """轨道的稳定标识。缺的部分用 ``-`` 占位，调用方按不透明字符串处理。"""
    return f"{kind.value}:{source_plugin or '-'}:{stream_id or '-'}:{entity or '-'}"


# ----------------------------------------------------------------------
# 状态、游标、资源与漂移
# ----------------------------------------------------------------------


def _track_state(track: Track, monotonic_ns: int) -> TrackState:
    metric = None
    last_event = None
    spans: list[TimelineSpan] = []
    for item in track.items:
        if isinstance(item, MetricPoint):
            if _at_or_before(item.monotonic_ns, monotonic_ns):
                metric = item
        elif isinstance(item, TimelineEvent):
            if _at_or_before(item.monotonic_ns, monotonic_ns):
                last_event = item
        elif isinstance(item, TimelineSpan):
            if _covers(item, monotonic_ns):
                spans.append(item)
    return TrackState(
        track_id=track.track_id,
        kind=track.kind,
        entity=track.entity,
        status=track.status,
        unavailable_reason=track.unavailable_reason,
        metric=metric,
        spans=tuple(spans),
        last_event=last_event,
    )


def _at_or_before(value: int | None, moment: int) -> bool:
    return value is not None and value <= moment


def _covers(span: TimelineSpan, moment: int) -> bool:
    if not _at_or_before(span.monotonic_ns, moment):
        return False
    # 结束时刻未知（只有墙钟声明了结束、或根本没收尾）时，按「从开始起一直有效」处理
    end_ns = span.end_monotonic_ns
    return end_ns is None or moment <= end_ns


def _axis_start(items: Sequence[AxisItem]) -> int | None:
    starts = [item.monotonic_ns for item in items if item.monotonic_ns is not None]
    return min(starts) if starts else None


def _axis_end(items: Sequence[AxisItem]) -> int | None:
    stops = []
    for item in items:
        if isinstance(item, TimelineSpan):
            stop = item.end_monotonic_ns
            stops.append(stop if stop is not None else item.monotonic_ns)
        else:
            stops.append(item.monotonic_ns)
    values = [value for value in stops if value is not None]
    return max(values) if values else None


def _stop_times(items: Sequence[AxisItem]) -> tuple[int, ...]:
    """游标可停靠的时刻：事件与采样点的时刻，区间的起点与结束点。"""
    times: set[int] = set()
    for item in items:
        if item.monotonic_ns is not None:
            times.add(item.monotonic_ns)
        if isinstance(item, TimelineSpan) and item.end_monotonic_ns is not None:
            times.add(item.end_monotonic_ns)
    return tuple(sorted(times))


def _neighbour(times: Sequence[int], position: int, *, forward: bool) -> int | None:
    if forward:
        return next((moment for moment in times if moment > position), None)
    return next((moment for moment in reversed(times) if moment < position), None)


def _resources(collections: Sequence[CollectionState]) -> ResourceStatus:
    """整次启动的资源数据可用性。

    任何一次采集声明有资源，这次启动就有资源；全部声明没有才是没有；没有声明时为 None
    （data-model 第 7 节、time-base 第 8.1 节）。
    """
    declared = [
        state.resources_available for state in collections if state.resources_available is not None
    ]
    reasons = tuple(
        sorted({state.resources_reason for state in collections if state.resources_reason})
    )
    return ResourceStatus(available=any(declared) if declared else None, reasons=reasons)


def _clock(boot: BootEntity, records: Sequence[Record]) -> ClockDrift:
    """启动锚点候选的中位数与极差（漂移），见 time-base 第 4.1、4.4 节。"""
    anchors: list[int] = []
    if boot.started_realtime_ns is not None:
        anchors.append(boot.started_realtime_ns)
    for record in records:
        if record.monotonic_ns is not None and record.realtime_ns is not None:
            anchors.append(record.realtime_ns - record.monotonic_ns)
    if not anchors:
        return ClockDrift(anchor_ns=None, drift_ns=None, anchor_count=0)
    return ClockDrift(
        anchor_ns=_median(anchors),
        drift_ns=max(anchors) - min(anchors),
        anchor_count=len(anchors),
    )


def _median(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _require_scope(records: Sequence[Record], machine_id: str, boot_id: str) -> None:
    """跨启动混排是禁令：读到的记录不属于这次启动就直接拒绝，不返回一个看似合理的顺序。"""
    for record in records:
        if record.machine_id != machine_id or record.boot_id != boot_id:
            raise TimelineScopeError(
                f"记录 {record.record_id} 属于 {record.machine_id}/{record.boot_id}，"
                f"不属于查询的 {machine_id}/{boot_id}；禁止跨启动排序"
            )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sort_time(value: int | None) -> int:
    """排序用的时刻：缺失时排在前面，让它不至于把整条轴算错。"""
    return -1 if value is None else value


__all__ = [
    "TIMELINE_KINDS",
    "TimelineBootNotFoundError",
    "TimelineEngine",
    "TimelineError",
    "TimelineKindError",
    "TimelineRangeError",
    "TimelineReader",
    "TimelineScopeError",
]
