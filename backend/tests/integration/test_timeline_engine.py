"""时间线引擎在真实 PostgreSQL 上的集成测试。

运行方式见 ``docs/implementation-baseline.md``：先起库并执行 ``alembic upgrade head``，再执行
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``，未配置时直接报错。

数据用仓库里的 ``docs/samples/bootrec-v1`` 样例：``full.bootrec``（SSH 采集）与
``journal-only.bootrec``（Journal 导入）属于同一次启动，正好验证「两类不同来源的数据排在同一条
轴上」；``resources-missing.bootrec`` 用来验证不可用轨道。

样例里的 boot_id 是固定的，需要第二条启动时用 ``_replay`` 把样例改写到新的 collection / boot 上，
``record_id`` 按新内容重算（data-model 第 8 节），与真实导入方做的一样。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sample_bootrec import SampleBundle, load_sample
from system_analyzer.domain.bootrec import (
    CollectionRef,
    Record,
    RecordKind,
    compute_record_id,
)
from system_analyzer.storage import DiagnosticStore
from system_analyzer.timeline import (
    MetricPoint,
    TimelineEngine,
    TimelineEvent,
    TimelineRangeError,
    TimelineSpan,
    TrackStatus,
)

pytestmark = pytest.mark.integration

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
SECOND_BOOT = "9a7e4c11-2b3d-4f58-8a90-6c1d2e3f4a5b"
THIRD_BOOT = "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d"

FULL_COLLECTION = "collection:full-0001"
JOURNAL_COLLECTION = "collection:journal-only-0001"
MISSING_COLLECTION = "collection:resources-missing-0001"

S = 1_000_000_000
"""一秒的纳秒数。样例里的事件、区间的时刻都是整秒的倍数。"""


@pytest.fixture
async def db(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例从空表开始，避免上一条用例的数据被查出来。"""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        for table in ("records", "entities", "collections", "boots", "machines"):
            await conn.execute(text(f"DELETE FROM {table}"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def store(db: AsyncEngine) -> DiagnosticStore:
    return DiagnosticStore(db)


@dataclass(frozen=True, slots=True)
class Replayed:
    """改写后的样例：新的 collection、实体与原始记录字典。"""

    collection: CollectionRef
    entities: list[BaseModel]
    record_lines: list[dict[str, Any]]


def _replay(sample: SampleBundle, collection_id: str, boot_id: str) -> Replayed:
    """把样例换到另一个 collection / boot 上，``record_id`` 按新内容重算。"""
    entities: list[BaseModel] = []
    for entity in sample.entities:
        update = {"boot_id": boot_id} if getattr(entity, "boot_id", None) is not None else {}
        entities.append(entity.model_copy(update=update) if update else entity)
    lines: list[dict[str, Any]] = []
    for line in sample.record_lines:
        content = {**line, "collection_id": collection_id, "boot_id": boot_id}
        content["record_id"] = compute_record_id(
            {key: value for key, value in content.items() if key != "record_id"}
        )
        lines.append(content)
    return Replayed(
        collection=sample.collection.model_copy(
            update={"collection_id": collection_id, "boot_id": boot_id}
        ),
        entities=entities,
        record_lines=lines,
    )


async def _import(store: DiagnosticStore, sample: SampleBundle) -> None:
    # 真实导入方也会把 manifest 的资源声明一起写进去（docs/data-model.md 第 7 节）
    await store.import_collection(
        sample.collection,
        sample.entities,
        sample.record_lines,
        source_sha256=sample.source_sha256,
        resources=sample.resources,
    )


def _keys(items: Any) -> list[tuple[int | None, str, str]]:
    return [(item.monotonic_ns, item.collection_id, item.record_id) for item in items]


# ---------------------------------------------------------------------------
# 两类来源同轴
# ---------------------------------------------------------------------------


async def test_two_collections_land_on_one_axis_in_order(store: DiagnosticStore) -> None:
    await _import(store, load_sample("full.bootrec"))
    await _import(store, load_sample("journal-only.bootrec"))
    engine = TimelineEngine(store)

    result = await engine.tracks(MACHINE_ID, BOOT_ID)

    # 排序键就是 time-base 第 7.1 节的 (monotonic_ns, collection_id, record_id)
    assert _keys(result.items) == sorted(_keys(result.items))
    # 固定数据：full 的 14 条（3 事件 + 2 区间 + 9 采样点）加 journal-only 的 2 条事件
    assert [item.monotonic_ns for item in result.items] == [
        900_000_000,
        900_000_000,
        1 * S,
        1 * S,
        1 * S,
        1_200_000_000,
        1_200_000_000,
        1_200_000_000,
        2 * S,
        2 * S,
        2 * S,
        3 * S,
        3 * S,
        3 * S,
        5 * S,
        5 * S,
    ]
    assert result.axis_start_ns == 900_000_000
    # nginx 的区间声明在 8.5 秒结束，它决定轴的终点
    assert result.axis_end_ns == 8_500_000_000
    # 样例全部带墙钟读数，没有「位置未知」的记录
    assert result.wall_clock_items == ()

    # 1.2 秒处两条事件来自不同插件，仍按 collection_id 定先后
    at_1_2 = [item for item in result.items if item.monotonic_ns == 1_200_000_000]
    assert [item.collection_id for item in at_1_2[:2]] == [FULL_COLLECTION, FULL_COLLECTION]
    assert at_1_2[2].collection_id == JOURNAL_COLLECTION

    tracks = {track.track_id: track for track in result.tracks}
    # 两条事件轨道分别来自 SSH 采集与 Journal 导入，指向同一个服务
    assert tracks["event:systemd:systemd.units:service:nginx.service"].status is (
        TrackStatus.AVAILABLE
    )
    assert tracks["event:journal-importer:systemd.units:service:nginx.service"].status is (
        TrackStatus.AVAILABLE
    )
    assert result.resources.available is True
    assert result.clock.drift_ns == 0
    assert result.clock.unreliable is False


async def test_each_boot_is_read_separately(store: DiagnosticStore) -> None:
    await _import(store, load_sample("full.bootrec"))
    replayed = _replay(
        load_sample("resources-missing.bootrec"), "collection:other-boot", SECOND_BOOT
    )
    await store.import_collection(replayed.collection, replayed.entities, replayed.record_lines)
    engine = TimelineEngine(store)

    first = await engine.tracks(MACHINE_ID, BOOT_ID)
    second = await engine.tracks(MACHINE_ID, SECOND_BOOT)

    assert {item.collection_id for item in first.items} == {FULL_COLLECTION}
    assert {item.collection_id for item in second.items} == {"collection:other-boot"}
    assert second.boot_id == SECOND_BOOT


# ---------------------------------------------------------------------------
# 时间范围边界
# ---------------------------------------------------------------------------


async def test_window_boundaries_on_real_data(store: DiagnosticStore) -> None:
    await _import(store, load_sample("full.bootrec"))
    await _import(store, load_sample("journal-only.bootrec"))
    engine = TimelineEngine(store)

    windowed = await engine.tracks(MACHINE_ID, BOOT_ID, start_ns=1 * S, end_ns=2 * S)
    # 1 秒与 2 秒的采样点、1.2 秒的事件与区间，加上 0.9 秒开始、5 秒结束的区间
    assert [item.monotonic_ns for item in windowed.items] == [
        900_000_000,
        1 * S,
        1 * S,
        1 * S,
        1_200_000_000,
        1_200_000_000,
        1_200_000_000,
        2 * S,
        2 * S,
        2 * S,
    ]
    # 范围不影响轴的起止
    assert windowed.axis_start_ns == 900_000_000
    assert windowed.axis_end_ns == 8_500_000_000

    # 起点等于终点：只返回该时刻的点，以及覆盖该时刻的区间
    instant = await engine.tracks(MACHINE_ID, BOOT_ID, start_ns=4 * S, end_ns=4 * S)
    assert [type(item).__name__ for item in instant.items] == ["TimelineSpan", "TimelineSpan"]

    # 范围完全在数据之外：条目为空，轨道仍然存在
    outside = await engine.tracks(MACHINE_ID, BOOT_ID, start_ns=100 * S, end_ns=200 * S)
    assert outside.items == ()
    assert all(track.status is TrackStatus.AVAILABLE for track in outside.tracks)
    assert len(outside.tracks) == len((await engine.tracks(MACHINE_ID, BOOT_ID)).tracks)

    with pytest.raises(TimelineRangeError):
        await engine.tracks(MACHINE_ID, BOOT_ID, start_ns=3 * S, end_ns=1 * S)


# ---------------------------------------------------------------------------
# 不可用与无数据
# ---------------------------------------------------------------------------


async def test_unavailable_streams_do_not_hide_other_tracks(store: DiagnosticStore) -> None:
    replayed = _replay(load_sample("resources-missing.bootrec"), MISSING_COLLECTION, SECOND_BOOT)
    await store.import_collection(replayed.collection, replayed.entities, replayed.record_lines)
    engine = TimelineEngine(store)

    result = await engine.tracks(MACHINE_ID, SECOND_BOOT)

    tracks = {track.track_id: track for track in result.tracks}
    cpu = tracks["metric:resource-sampler:cpu:metric:cpu.total.utilization"]
    assert cpu.status is TrackStatus.AVAILABLE
    assert [point.value for point in cpu.items] == [9.5, 21.75, 15.0]

    memory = tracks["metric:-:-:metric:memory.used"]
    assert memory.status is TrackStatus.UNAVAILABLE
    assert memory.unavailable_reason == "/proc/meminfo 在采集窗口内读取失败，本流没有数据"
    assert memory.items == ()

    disk = tracks["metric:-:-:metric:disk.read_bytes_per_second"]
    assert disk.status is TrackStatus.UNAVAILABLE
    assert disk.unavailable_reason == "目标机器没有 iostat，磁盘 I/O 未采集"

    # 不可用不影响服务轨道
    assert tracks["span:systemd:systemd.units:service:nginx.service"].status is (
        TrackStatus.AVAILABLE
    )


async def test_records_win_over_an_unavailable_declaration(store: DiagnosticStore) -> None:
    """同一次启动下两次采集说法不一致时，有记录就显示数据（PL 定下的读取口径）。"""
    missing = _replay(load_sample("resources-missing.bootrec"), MISSING_COLLECTION, THIRD_BOOT)
    await store.import_collection(missing.collection, missing.entities, missing.record_lines)
    full = _replay(load_sample("full.bootrec"), "collection:third-boot-full", THIRD_BOOT)
    await store.import_collection(full.collection, full.entities, full.record_lines)

    # 先写入的那份实体描述说 memory 不可用，后一份带来了采样点
    streams = {
        stream.entity_id: stream
        for stream in await store.list_metric_streams(MACHINE_ID, THIRD_BOOT)
    }
    assert streams["metric:memory.used"].available is False

    result = await TimelineEngine(store).tracks(MACHINE_ID, THIRD_BOOT)

    memory = next(track for track in result.tracks if track.entity == "metric:memory.used")
    assert memory.status is TrackStatus.AVAILABLE
    assert memory.unavailable_reason is None
    assert [round(point.value) for point in memory.items] == [
        4_200_000_000,
        4_400_000_000,
        4_100_000_000,
    ]


# ---------------------------------------------------------------------------
# 游标与指定时刻的状态
# ---------------------------------------------------------------------------


async def test_cursor_and_state_at_on_real_data(store: DiagnosticStore) -> None:
    await _import(store, load_sample("full.bootrec"))
    engine = TimelineEngine(store)

    start = await engine.cursor(MACHINE_ID, BOOT_ID)
    assert (start.start_ns, start.position_ns, start.next_ns) == (900_000_000, 900_000_000, 1 * S)
    assert start.previous_ns is None

    middle = await engine.cursor(MACHINE_ID, BOOT_ID, at_ns=4 * S)
    assert middle.previous_ns == 3 * S
    assert middle.next_ns == 5 * S

    end = await engine.cursor(MACHINE_ID, BOOT_ID, at_ns=8_500_000_000)
    assert end.position_ns == 8_500_000_000
    assert end.next_ns is None

    stepped = await engine.step_cursor(start)
    assert stepped.position_ns == 1 * S

    state = await engine.state_at(MACHINE_ID, BOOT_ID, 4 * S)
    tracks = {track.track_id: track for track in state.tracks}
    cpu = tracks["metric:resource-sampler:cpu:metric:cpu.total.utilization"]
    assert isinstance(cpu.metric, MetricPoint)
    assert cpu.metric.monotonic_ns == 3 * S
    assert cpu.metric.value == 8.0

    nginx = tracks["span:systemd:systemd.units:service:nginx.service"]
    assert [span.state for span in nginx.spans] == ["active"]

    # 事件轨道给该时刻之前最近的一条事件；区间轨道给正在进行的区间
    nginx_events = tracks["event:systemd:systemd.units:service:nginx.service"]
    assert isinstance(nginx_events.last_event, TimelineEvent)
    assert nginx_events.last_event.event_type == "systemd.unit.started"
    assert nginx_events.last_event.monotonic_ns == 1_200_000_000

    # postgresql 的区间在 5 秒结束，4 秒时仍然在运行
    postgres = tracks["span:systemd:systemd.units:service:postgresql.service"]
    assert len(postgres.spans) == 1
    assert postgres.spans[0].state == "failed"


async def test_open_span_closed_by_collection_window_in_the_store(
    store: DiagnosticStore,
) -> None:
    """开放区间按所属采集的窗口终点收尾；样例里的两条区间都已声明结束，这里补一条开放的。"""
    await _import(store, load_sample("full.bootrec"))
    open_span = Record(
        record_id="0" * 32,
        kind=RecordKind.SPAN,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        collection_id=FULL_COLLECTION,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=6 * S,
        realtime_ns=1_794_009_606 * S,
        clock_domain="boot_monotonic",
        entity="service:nginx.service",
        payload={"span_type": "service.active", "state": "active", "attributes": {}},
    )
    open_span.record_id = compute_record_id(
        open_span.model_dump(mode="json", exclude={"record_id"})
    )
    await store.append_records(FULL_COLLECTION, [open_span])

    result = await TimelineEngine(store).tracks(MACHINE_ID, BOOT_ID)

    spans = {span.record_id: span for span in result.items if isinstance(span, TimelineSpan)}
    opened = spans[open_span.record_id]
    # 收尾取这次采集内所有时间字段的最大值：nginx 的区间声明在 8.5 秒结束，窗口终点就是 8.5 秒
    assert opened.end_is_declared is False
    assert opened.end_monotonic_ns == 8_500_000_000
    assert opened.closed_by == FULL_COLLECTION
