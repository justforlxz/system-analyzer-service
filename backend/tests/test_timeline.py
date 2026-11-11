"""时间线引擎的单元测试：固定数据、不连数据库。

用例覆盖 ``docs/time-base.md`` 里对 1.5 的硬约束（排序键、跨启动禁止、开放区间收尾、
墙钟单独排列）与本任务的验收标准（时间范围边界、不可用与无数据的区分、指定时刻状态、游标）。
真实 PostgreSQL 上的同族断言在 ``tests/integration/test_timeline_engine.py``。

读侧替身刻意只实现 ``TimelineReader`` 的四个方法，并断言引擎不会把时间范围推给存储层：
范围筛选是引擎自己的口径，换一个存储实现不能改变结果。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    CollectionRef,
    MetricStreamEntity,
    Record,
    RecordKind,
)
from system_analyzer.storage import CollectionState, DiagnosticStore, ImportStatus
from system_analyzer.timeline import (
    MetricPoint,
    TimeCursor,
    TimelineBootNotFoundError,
    TimelineEngine,
    TimelineEvent,
    TimelineKindError,
    TimelineRangeError,
    TimelineScopeError,
    TimelineSpan,
    TrackStatus,
)

MACHINE = "machine:uos-PC"
BOOT_A = "boot:a"
BOOT_B = "boot:b"
COL_A1 = "collection:a1"
COL_A2 = "collection:a2"

ANCHOR = 1_000_000_000_000
"""墙钟上的启动锚点；带墙钟读数的记录按「ANCHOR + monotonic_ns」给出。"""

S = 1_000_000_000
"""一秒的纳秒数，用例里的时刻都用它做单位。"""


# ---------------------------------------------------------------------------
# 固定数据
# ---------------------------------------------------------------------------


def _id(number: int) -> str:
    return f"{number:032x}"


def _boot(boot_id: str = BOOT_A, *, started_realtime_ns: int | None = ANCHOR) -> BootEntity:
    return BootEntity(
        entity_type="boot",
        boot_id=boot_id,
        machine_id=MACHINE,
        started_realtime_ns=started_realtime_ns,
        boot_index=1,
    )


def _collection(
    collection_id: str = COL_A1,
    *,
    boot_id: str = BOOT_A,
    resources_available: bool | None = True,
    resources_reason: str | None = None,
) -> CollectionState:
    return CollectionState(
        collection=CollectionRef(
            collection_id=collection_id,
            machine_id=MACHINE,
            boot_id=boot_id,
            mode=CollectionMode.SSH_COLLECTION,
            created_realtime_ns=ANCHOR,
        ),
        import_status=ImportStatus.IMPORTED,
        import_error=None,
        source_sha256=None,
        resources_available=resources_available,
        resources_reason=resources_reason,
    )


def _stream(
    entity_id: str,
    name: str,
    unit: str,
    *,
    available: bool = True,
    reason: str | None = None,
    boot_id: str = BOOT_A,
) -> MetricStreamEntity:
    return MetricStreamEntity(
        entity_type="metric_stream",
        entity_id=entity_id,
        machine_id=MACHINE,
        boot_id=boot_id,
        name=name,
        unit=unit,
        available=available,
        unavailable_reason=reason,
    )


def _event(
    number: int,
    monotonic_ns: int,
    *,
    entity: str | None = "service:nginx.service",
    collection_id: str = COL_A1,
    source_plugin: str = "systemd",
    stream_id: str = "systemd.units",
    event_type: str = "systemd.unit.started",
    severity: str = "info",
    boot_id: str = BOOT_A,
    with_realtime: bool = True,
    realtime_offset: int = 0,
) -> Record:
    return Record(
        record_id=_id(number),
        kind=RecordKind.EVENT,
        machine_id=MACHINE,
        boot_id=boot_id,
        collection_id=collection_id,
        source_plugin=source_plugin,
        stream_id=stream_id,
        monotonic_ns=monotonic_ns,
        realtime_ns=(ANCHOR + monotonic_ns + realtime_offset if with_realtime else None),
        clock_domain=ClockDomain.BOOT_MONOTONIC,
        entity=entity,
        payload={
            "event_type": event_type,
            "severity": severity,
            "message": f"事件 {number}",
            "attributes": {},
        },
    )


def _span(
    number: int,
    monotonic_ns: int,
    *,
    end: int | None = None,
    end_realtime_ns: int | None = None,
    entity: str | None = "service:nginx.service",
    collection_id: str = COL_A1,
    span_type: str = "service.active",
    state: str | None = "active",
    boot_id: str = BOOT_A,
) -> Record:
    """``end`` 给了就同时声明两个时钟域的结束；只给 ``end_realtime_ns`` 表示只有墙钟收尾。"""
    return Record(
        record_id=_id(number),
        kind=RecordKind.SPAN,
        machine_id=MACHINE,
        boot_id=boot_id,
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=monotonic_ns,
        realtime_ns=ANCHOR + monotonic_ns,
        clock_domain=ClockDomain.BOOT_MONOTONIC,
        entity=entity,
        payload={
            "span_type": span_type,
            "state": state,
            "end_monotonic_ns": end,
            "end_realtime_ns": (ANCHOR + end if end is not None else end_realtime_ns),
            "attributes": {},
        },
    )


def _point(
    number: int,
    monotonic_ns: int,
    *,
    value: float = 12.5,
    entity: str = "metric:cpu.total.utilization",
    name: str = "cpu.total.utilization",
    unit: str = "percent",
    stream_id: str = "cpu",
    collection_id: str = COL_A1,
    boot_id: str = BOOT_A,
    realtime_offset: int = 0,
) -> Record:
    return Record(
        record_id=_id(number),
        kind=RecordKind.METRIC,
        machine_id=MACHINE,
        boot_id=boot_id,
        collection_id=collection_id,
        source_plugin="resource-sampler",
        stream_id=stream_id,
        monotonic_ns=monotonic_ns,
        realtime_ns=ANCHOR + monotonic_ns + realtime_offset,
        clock_domain=ClockDomain.BOOT_MONOTONIC,
        entity=entity,
        payload={"name": name, "value": value, "unit": unit},
    )


def _wall_event(number: int, realtime_ns: int, *, boot_id: str = BOOT_A) -> Record:
    """只有墙钟的日志：没有单调读数，不能进入 boot 内排序。"""
    return Record(
        record_id=_id(number),
        kind=RecordKind.EVENT,
        machine_id=MACHINE,
        boot_id=boot_id,
        collection_id=COL_A2,
        source_plugin="journal-importer",
        stream_id="systemd.units",
        monotonic_ns=None,
        realtime_ns=realtime_ns,
        clock_domain=ClockDomain.WALL_REALTIME,
        entity="service:nginx.service",
        payload={
            "event_type": "journal.entry",
            "severity": "info",
            "message": "外来日志没有单调时间戳",
            "attributes": {},
        },
    )


@dataclass
class FakeReader:
    """``TimelineReader`` 的替身：按 (machine_id, boot_id) 返回事先摆好的数据。"""

    boots: dict[tuple[str, str], BootEntity]
    records: dict[tuple[str, str], list[Record]]
    streams: dict[tuple[str, str], list[MetricStreamEntity]]
    collections: dict[tuple[str, str], list[CollectionState]] = field(default_factory=dict)

    async def get_boot(self, machine_id: str, boot_id: str) -> BootEntity | None:
        return self.boots.get((machine_id, boot_id))

    async def list_collections(
        self,
        *,
        machine_id: str | None = None,
        boot_id: str | None = None,
        status: ImportStatus | None = None,
    ) -> list[CollectionState]:
        states = [
            state
            for (machine, boot), group in self.collections.items()
            if (machine_id is None or machine == machine_id)
            and (boot_id is None or boot == boot_id)
            for state in group
        ]
        if status is not None:
            states = [state for state in states if state.import_status is status]
        return states

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
    ) -> list[Record]:
        # 只为结果正确性负责：范围筛选现在在引擎里做，将来下推给存储层也不该被这条替身拦住
        return [
            record
            for record in self.records.get((machine_id, boot_id), [])
            if record.clock_domain is clock_domain and (not kinds or record.kind in kinds)
        ]

    async def list_metric_streams(self, machine_id: str, boot_id: str) -> list[MetricStreamEntity]:
        return list(self.streams.get((machine_id, boot_id), []))


def _reader(
    records: Sequence[Record] = (),
    *,
    boot: BootEntity | None = None,
    streams: Sequence[MetricStreamEntity] = (),
    collections: Sequence[CollectionState] | None = None,
) -> FakeReader:
    key = (MACHINE, BOOT_A)
    return FakeReader(
        boots={key: boot or _boot()},
        records={key: list(records)},
        streams={key: list(streams)},
        collections={
            key: list(collections) if collections is not None else [_collection()],
        },
    )


def _engine(reader: FakeReader) -> TimelineEngine:
    return TimelineEngine(reader)


def _item_ids(items: Sequence[Any]) -> list[str]:
    return [item.record_id for item in items]


# ---------------------------------------------------------------------------
# 老事件、老指标排在同一条轴上
# ---------------------------------------------------------------------------


async def test_two_sources_share_one_axis_in_order() -> None:
    """不同插件、不同数据种类的记录排在同一条轴上，顺序就是排序键的顺序。"""
    reader = _reader(
        [
            _event(1, 2 * S),
            _point(2, 1 * S),
            _point(
                3,
                3 * S,
                entity="metric:memory.used",
                name="memory.used",
                unit="bytes",
                stream_id="memory",
            ),
            _span(4, 1 * S, end=5 * S),
        ],
        streams=[
            _stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent"),
            _stream("metric:memory.used", "memory.used", "bytes"),
        ],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    # 1 秒处有两条：先按 collection_id、再按 record_id 决定先后（time-base 第 7.1 节）
    assert _item_ids(result.items) == [_id(2), _id(4), _id(1), _id(3)]
    assert [(item.monotonic_ns) for item in result.items] == [1 * S, 1 * S, 2 * S, 3 * S]
    assert result.axis_start_ns == 1 * S
    assert result.axis_end_ns == 5 * S

    # 每条轨道只装自己那一类的条目，轨道标识稳定
    assert [track.track_id for track in result.tracks] == [
        "event:systemd:systemd.units:service:nginx.service",
        "metric:resource-sampler:cpu:metric:cpu.total.utilization",
        "metric:resource-sampler:memory:metric:memory.used",
        "span:systemd:systemd.units:service:nginx.service",
    ]
    by_id = {track.track_id: track for track in result.tracks}
    assert by_id["metric:resource-sampler:cpu:metric:cpu.total.utilization"].items[0].value == 12.5


async def test_same_nanosecond_orders_by_collection_then_record_id() -> None:
    """同一纳秒的多条记录靠后两个键稳定排序，不同次查询得到同一个顺序。"""
    reader = _reader(
        [
            _event(1, 1 * S, collection_id=COL_A2),
            _event(2, 1 * S, collection_id=COL_A1),
        ]
    )
    engine = _engine(reader)

    first = await engine.tracks(MACHINE, BOOT_A)
    second = await engine.tracks(MACHINE, BOOT_A)

    assert _item_ids(first.items) == [_id(2), _id(1)]
    assert _item_ids(first.items) == _item_ids(second.items)


async def test_kinds_filter_returns_only_requested_tracks() -> None:
    reader = _reader(
        [_event(1, 1 * S), _point(2, 2 * S)],
        streams=[_stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent")],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A, kinds=[RecordKind.METRIC])

    assert [track.kind for track in result.tracks] == [RecordKind.METRIC]
    assert _item_ids(result.items) == [_id(2)]


async def test_open_span_closure_does_not_depend_on_kinds() -> None:
    """只取部分种类时，开放区间仍按整次采集的窗口收尾，不能被截短。"""
    reader = _reader([_span(1, 1 * S), _point(2, 100 * S)])

    everything = await _engine(reader).tracks(MACHINE, BOOT_A)
    spans_only = await _engine(reader).tracks(MACHINE, BOOT_A, kinds=[RecordKind.SPAN])

    full_span = next(item for item in everything.items if isinstance(item, TimelineSpan))
    narrow_span = next(item for item in spans_only.items if isinstance(item, TimelineSpan))
    assert full_span.end_monotonic_ns == 100 * S
    assert narrow_span.end_monotonic_ns == 100 * S
    assert narrow_span.end_is_declared is False
    assert narrow_span.closed_by == COL_A1
    # 轴的起止也是整次启动的范围，不随筛选变化
    assert (spans_only.axis_start_ns, spans_only.axis_end_ns) == (
        everything.axis_start_ns,
        everything.axis_end_ns,
    )


async def test_clock_drift_does_not_depend_on_kinds() -> None:
    """只取部分种类时，启动级锚点与漂移仍按整次启动的全部记录计算。"""
    reader = _reader([_event(1, 1 * S), _point(2, 2 * S, realtime_offset=2 * S)])

    everything = await _engine(reader).tracks(MACHINE, BOOT_A)
    events_only = await _engine(reader).tracks(MACHINE, BOOT_A, kinds=[RecordKind.EVENT])

    assert everything.clock.drift_ns == 2 * S
    assert everything.clock.anchor_count == 3
    assert everything.clock.unreliable is True
    assert events_only.clock == everything.clock


# ---------------------------------------------------------------------------
# 跨启动不串
# ---------------------------------------------------------------------------


async def test_each_boot_is_queried_alone_and_mixing_is_rejected() -> None:
    reader = FakeReader(
        boots={(MACHINE, BOOT_A): _boot(), (MACHINE, BOOT_B): _boot(BOOT_B)},
        records={
            (MACHINE, BOOT_A): [_event(1, 1 * S)],
            (MACHINE, BOOT_B): [_event(2, 2 * S, boot_id=BOOT_B)],
        },
        streams={},
        collections={
            (MACHINE, BOOT_A): [_collection()],
            (MACHINE, BOOT_B): [_collection(COL_A2, boot_id=BOOT_B)],
        },
    )
    engine = _engine(reader)

    boot_a = await engine.tracks(MACHINE, BOOT_A)
    boot_b = await engine.tracks(MACHINE, BOOT_B)

    assert _item_ids(boot_a.items) == [_id(1)]
    assert _item_ids(boot_b.items) == [_id(2)]
    assert boot_a.boot_id == BOOT_A and boot_b.boot_id == BOOT_B

    # 存储层把别的启动的记录混进来时必须拒绝，而不是排出一个看似合理的顺序
    mixed = FakeReader(
        boots={(MACHINE, BOOT_A): _boot()},
        records={(MACHINE, BOOT_A): [_event(3, 1 * S, boot_id=BOOT_B)]},
        streams={},
    )
    with pytest.raises(TimelineScopeError):
        await _engine(mixed).tracks(MACHINE, BOOT_A)


async def test_missing_boot_is_rejected() -> None:
    with pytest.raises(TimelineBootNotFoundError):
        await _engine(_reader()).tracks(MACHINE, "boot:not-there")


# ---------------------------------------------------------------------------
# 不可用 / 无数据
# ---------------------------------------------------------------------------


async def test_unavailable_stream_does_not_hide_other_tracks() -> None:
    reader = _reader(
        [_point(1, 1 * S), _point(2, 2 * S), _point(3, 3 * S), _event(4, 2 * S)],
        streams=[
            _stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent"),
            _stream(
                "metric:memory.used",
                "memory.used",
                "bytes",
                available=False,
                reason="/proc/meminfo 在采集窗口内读取失败",
            ),
            _stream(
                "metric:disk.read_bytes_per_second",
                "disk.read_bytes_per_second",
                "bytes_per_second",
                available=False,
                reason="目标机器没有 iostat",
            ),
        ],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    tracks = {track.track_id: track for track in result.tracks}
    cpu = tracks["metric:resource-sampler:cpu:metric:cpu.total.utilization"]
    assert cpu.status is TrackStatus.AVAILABLE
    assert [point.value for point in cpu.items] == [12.5, 12.5, 12.5]

    memory = tracks["metric:-:-:metric:memory.used"]
    assert memory.status is TrackStatus.UNAVAILABLE
    assert memory.unavailable_reason == "/proc/meminfo 在采集窗口内读取失败"
    assert memory.items == ()

    # 不可用的流不影响事件轨道；2 秒处的采样点与事件按 record_id 定先后
    assert _item_ids(result.items) == [_id(1), _id(2), _id(4), _id(3)]


async def test_stream_with_records_shows_data_even_if_marked_unavailable() -> None:
    """同一次启动下多次采集对同一条流的 available 判断不一致时，有记录就显示数据。"""
    reader = _reader(
        [
            _point(
                1,
                1 * S,
                entity="metric:memory.used",
                name="memory.used",
                unit="bytes",
                stream_id="memory",
            ),
            _point(
                2,
                2 * S,
                entity="metric:memory.used",
                name="memory.used",
                unit="bytes",
                stream_id="memory",
            ),
        ],
        streams=[
            _stream(
                "metric:memory.used",
                "memory.used",
                "bytes",
                available=False,
                reason="/proc/meminfo 读取失败（另一次采集的判断）",
            )
        ],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    memory = result.tracks[0]
    assert memory.status is TrackStatus.AVAILABLE
    assert memory.unavailable_reason is None
    assert [point.monotonic_ns for point in memory.items] == [1 * S, 2 * S]


async def test_declared_stream_without_records_is_no_data() -> None:
    """声明成可用却一个采样点都没有：按「无数据」处理，不当成不可用，也不补 0。"""
    reader = _reader(
        [],
        streams=[_stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent")],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    (cpu,) = result.tracks
    assert cpu.status is TrackStatus.NO_DATA
    assert cpu.unavailable_reason is None
    assert cpu.items == ()
    assert result.axis_start_ns is None and result.axis_end_ns is None


async def test_resource_absence_is_reported_without_metric_tracks() -> None:
    """Journal-only 导入：没有资源轨道，但整份数据「不含资源」这件事要能读出来。"""
    reader = _reader(
        [_event(1, 1 * S), _event(2, 5 * S)],
        collections=[
            _collection(
                COL_A2,
                resources_available=False,
                resources_reason="journal_import：压缩包只含 Journal，没有采集 CPU、内存、磁盘 I/O",
            )
        ],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    assert [track.kind for track in result.tracks] == [RecordKind.EVENT]
    assert result.resources.available is False
    assert result.resources.reasons == (
        "journal_import：压缩包只含 Journal，没有采集 CPU、内存、磁盘 I/O",
    )


# ---------------------------------------------------------------------------
# 开放区间收尾
# ---------------------------------------------------------------------------


async def test_open_span_closes_at_its_own_collection_window() -> None:
    reader = _reader(
        [
            # 第一次采集：窗口终点 9 秒
            _span(1, 2 * S, state="active"),
            _event(2, 9 * S),
            # 第二次采集：窗口终点 4 秒
            _span(3, 1 * S, collection_id=COL_A2, state="active"),
            _event(4, 4 * S, collection_id=COL_A2),
            # 已声明结束的区间不受窗口影响
            _span(5, 3 * S, end=3 * S + S // 2),
        ],
        collections=[_collection(), _collection(COL_A2)],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    spans = {span.record_id: span for span in result.items if isinstance(span, TimelineSpan)}
    assert spans[_id(1)].end_monotonic_ns == 9 * S
    assert spans[_id(1)].end_is_declared is False
    assert spans[_id(1)].closed_by == COL_A1

    assert spans[_id(3)].end_monotonic_ns == 4 * S
    assert spans[_id(3)].closed_by == COL_A2

    assert spans[_id(5)].end_monotonic_ns == 3 * S + S // 2
    assert spans[_id(5)].end_is_declared is True
    assert spans[_id(5)].closed_by is None
    # 收尾后的结束时刻参与轴的终点
    assert result.axis_end_ns == 9 * S


async def test_open_span_closure_does_not_follow_the_query_window() -> None:
    reader = _reader([_span(1, 2 * S), _event(2, 9 * S)])
    engine = _engine(reader)

    narrow = await engine.tracks(MACHINE, BOOT_A, start_ns=2 * S, end_ns=3 * S)
    full = await engine.tracks(MACHINE, BOOT_A)

    narrow_span = next(item for item in narrow.items if isinstance(item, TimelineSpan))
    full_span = next(item for item in full.items if isinstance(item, TimelineSpan))
    assert narrow_span.end_monotonic_ns == 9 * S
    assert full_span.end_monotonic_ns == 9 * S
    assert narrow.axis_end_ns == full.axis_end_ns == 9 * S


async def test_span_with_only_wall_clock_end_is_not_reclosed() -> None:
    """``end_monotonic_ns`` 为空而 ``end_realtime_ns`` 有值：按墙钟收尾，不换算成单调时间。"""
    reader = _reader([_span(1, 2 * S, end_realtime_ns=ANCHOR + 6 * S), _event(2, 9 * S)])

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    span = next(item for item in result.items if isinstance(item, TimelineSpan))
    assert span.end_is_declared is True
    assert span.end_monotonic_ns is None
    assert span.end_realtime_ns == ANCHOR + 6 * S
    assert span.closed_by is None


# ---------------------------------------------------------------------------
# 时间范围边界
# ---------------------------------------------------------------------------


async def test_window_boundaries_are_inclusive() -> None:
    reader = _reader([_point(1, 1 * S), _point(2, 2 * S), _point(3, 3 * S)])
    engine = _engine(reader)

    at_start = await engine.tracks(MACHINE, BOOT_A, start_ns=2 * S)
    assert _item_ids(at_start.items) == [_id(2), _id(3)]

    at_end = await engine.tracks(MACHINE, BOOT_A, end_ns=2 * S)
    assert _item_ids(at_end.items) == [_id(1), _id(2)]

    both = await engine.tracks(MACHINE, BOOT_A, start_ns=2 * S, end_ns=2 * S)
    assert _item_ids(both.items) == [_id(2)]
    assert both.window_start_ns == both.window_end_ns == 2 * S


async def test_inverted_window_is_rejected() -> None:
    engine = _engine(_reader([_point(1, 1 * S)]))

    with pytest.raises(TimelineRangeError):
        await engine.tracks(MACHINE, BOOT_A, start_ns=3 * S, end_ns=1 * S)


async def test_window_outside_data_keeps_tracks_and_empties_items() -> None:
    reader = _reader(
        [_point(1, 1 * S)],
        streams=[_stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent")],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A, start_ns=10 * S, end_ns=20 * S)

    assert result.items == ()
    # 轨道本身仍然列出：这条窗口里没有数据，不等于这条轨道不存在
    assert [track.track_id for track in result.tracks] == [
        "metric:resource-sampler:cpu:metric:cpu.total.utilization"
    ]
    assert result.tracks[0].status is TrackStatus.AVAILABLE
    assert result.tracks[0].items == ()
    # 轴的起止按整次启动的数据算，不跟着窗口跑
    assert result.axis_start_ns == 1 * S and result.axis_end_ns == 1 * S


async def test_span_spanning_the_window_is_returned() -> None:
    reader = _reader(
        [
            _span(1, 1 * S, end=5 * S),
            _span(2, 0, end=1 * S),
        ]
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A, start_ns=2 * S, end_ns=3 * S)

    assert _item_ids(result.items) == [_id(1)]


# ---------------------------------------------------------------------------
# 指定时刻的状态
# ---------------------------------------------------------------------------


async def test_state_at_reports_nearest_sample_active_spans_and_last_event() -> None:
    reader = _reader(
        [
            _point(1, 1 * S, value=12.5),
            _point(2, 2 * S, value=30.25),
            _point(3, 3 * S, value=8.0),
            _span(4, 1 * S, end=5 * S),
            _event(5, 500_000_000),
            _event(6, 5 * S // 2),
        ],
        streams=[_stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent")],
    )

    state = await _engine(reader).state_at(MACHINE, BOOT_A, 5 * S // 2)

    tracks = {track.track_id: track for track in state.tracks}
    cpu = tracks["metric:resource-sampler:cpu:metric:cpu.total.utilization"]
    # 取该时刻或之前最近的一条：2 秒的 30.25，不是 3 秒的 8.0
    assert isinstance(cpu.metric, MetricPoint)
    assert cpu.metric.monotonic_ns == 2 * S
    assert cpu.metric.value == 30.25

    service = tracks["span:systemd:systemd.units:service:nginx.service"]
    assert [span.record_id for span in service.spans] == [_id(4)]

    events = tracks["event:systemd:systemd.units:service:nginx.service"]
    assert isinstance(events.last_event, TimelineEvent)
    assert events.last_event.record_id == _id(6)


async def test_state_at_before_any_data_keeps_tracks() -> None:
    reader = _reader(
        [_point(1, 5 * S, value=1.0)],
        streams=[_stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent")],
    )

    state = await _engine(reader).state_at(MACHINE, BOOT_A, 0)

    (cpu,) = state.tracks
    assert cpu.status is TrackStatus.AVAILABLE
    assert cpu.metric is None
    assert cpu.spans == ()
    assert cpu.last_event is None


# ---------------------------------------------------------------------------
# 时间游标
# ---------------------------------------------------------------------------


async def test_cursor_boundaries_and_stepping() -> None:
    reader = _reader(
        [
            _event(1, 1 * S),
            _point(2, 2 * S),
            _span(3, 3 * S, end=4 * S),
        ],
        streams=[_stream("metric:cpu.total.utilization", "cpu.total.utilization", "percent")],
    )
    engine = _engine(reader)

    start = await engine.cursor(MACHINE, BOOT_A)
    assert (start.start_ns, start.end_ns, start.position_ns) == (1 * S, 4 * S, 1 * S)
    assert start.previous_ns is None
    assert start.next_ns == 2 * S

    middle = await engine.cursor(MACHINE, BOOT_A, at_ns=5 * S // 2)
    assert middle.previous_ns == 2 * S
    assert middle.next_ns == 3 * S

    # 区间的结束时刻也是一个可以停靠的时刻
    end = await engine.cursor(MACHINE, BOOT_A, at_ns=4 * S)
    assert end.position_ns == 4 * S
    assert end.previous_ns == 3 * S
    assert end.next_ns is None

    # 越界夹到起止点
    assert (await engine.cursor(MACHINE, BOOT_A, at_ns=0)).position_ns == 1 * S
    assert (await engine.cursor(MACHINE, BOOT_A, at_ns=99 * S)).position_ns == 4 * S

    stepped = await engine.step_cursor(start)
    assert stepped.position_ns == 2 * S
    assert stepped.previous_ns == 1 * S
    assert stepped.next_ns == 3 * S

    backed = await engine.step_cursor(end, forward=False)
    assert backed.position_ns == 3 * S

    # 已经在两端就原样返回
    assert await engine.step_cursor(end) == end
    assert await engine.step_cursor(start, forward=False) == start


async def test_cursor_on_empty_boot_is_all_none() -> None:
    cursor = await _engine(_reader()).cursor(MACHINE, BOOT_A)

    assert cursor == TimeCursor(
        machine_id=MACHINE,
        boot_id=BOOT_A,
        start_ns=None,
        end_ns=None,
        position_ns=None,
        previous_ns=None,
        next_ns=None,
    )


# ---------------------------------------------------------------------------
# 只有墙钟的记录与漂移
# ---------------------------------------------------------------------------


async def test_wall_clock_records_stay_off_the_axis() -> None:
    reader = _reader(
        [_event(1, 1 * S), _wall_event(2, ANCHOR + 3 * S)],
        collections=[_collection(), _collection(COL_A2)],
    )

    result = await _engine(reader).tracks(MACHINE, BOOT_A)

    assert _item_ids(result.items) == [_id(1)]
    assert _item_ids(result.wall_clock_items) == [_id(2)]
    assert all(track.kind is RecordKind.EVENT for track in result.tracks)
    assert sum(len(track.items) for track in result.tracks) == 1


async def test_clock_drift_and_unreliable_threshold() -> None:
    steady = _reader(
        [_event(1, 1 * S), _event(2, 2 * S)],
        boot=_boot(started_realtime_ns=None),
    )
    steady_clock = (await _engine(steady).tracks(MACHINE, BOOT_A)).clock
    assert steady_clock.anchor_count == 2
    assert steady_clock.drift_ns == 0
    assert steady_clock.unreliable is False

    drifted = _reader(
        [
            _event(1, 1 * S),
            _event(2, 2 * S, realtime_offset=2 * S),
        ],
        boot=_boot(started_realtime_ns=None),
    )
    drifted_clock = (await _engine(drifted).tracks(MACHINE, BOOT_A)).clock
    assert drifted_clock.anchor_count == 2
    assert drifted_clock.drift_ns == 2 * S
    assert drifted_clock.unreliable is True


async def test_unsupported_kind_is_rejected() -> None:
    """范围外的种类与拼错的值都走本模块的异常，不泄漏 ValueError。"""
    engine = _engine(_reader([_event(1, 1 * S)]))

    with pytest.raises(TimelineKindError):
        await engine.tracks(MACHINE, BOOT_A, kinds=[RecordKind.ANNOTATION])
    with pytest.raises(TimelineKindError):
        await engine.tracks(MACHINE, BOOT_A, kinds=["anntation"])


# ---------------------------------------------------------------------------
# 存储替身的形状与真实存储一致
# ---------------------------------------------------------------------------


def test_diagnostic_store_satisfies_the_reader_protocol() -> None:
    """引擎只依赖四个方法；真实存储不需要额外适配。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    # create_async_engine 不连接数据库，这里只检查方法与签名
    store = DiagnosticStore(create_async_engine("postgresql+asyncpg://u@127.0.0.1:1/db"))
    for method in ("get_boot", "list_collections", "list_records_in_boot", "list_metric_streams"):
        assert callable(getattr(store, method, None)), method
