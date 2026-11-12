"""资源采集插件的真实 PostgreSQL 集成测试。

一条测试走完整的 ``PluginRegistry.invoke``：假远程采样输出 → 插件换算 → 真实
``DiagnosticStore.append_records`` → 再从库里读回 Metric。实体由测试按插件导出的
``metric_stream_entities`` 预置，因为 ``PluginDataAccess`` 按契约只追加记录，不写实体。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.collectors.resources import (
    CGROUP_CPU,
    CGROUP_DISK_READ,
    CGROUP_DISK_WRITE,
    CGROUP_MEMORY,
    PLUGIN_ID,
    SYSTEM_CPU,
    SYSTEM_DISK_READ,
    SYSTEM_DISK_WRITE,
    SYSTEM_MEMORY,
    UNIT_CPU,
    UNIT_DISK_READ,
    UNIT_DISK_WRITE,
    UNIT_MEMORY,
    ResourceCollector,
    build_records,
    metric_stream_entities,
    parse_capture,
)
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    Record,
    RecordKind,
    ResourceAvailability,
)
from system_analyzer.plugins.contracts import CollectRequest, CollectResult
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore
from system_analyzer.timeline import MetricPoint, TimelineEngine

pytestmark = pytest.mark.integration

MACHINE_ID = "machine:sas-resources"
BOOT_ID = "abcdef12-3456-4789-abcd-ef0123456789"
COLLECTION_ID = "collection:sas-resources"
RUN_ID = "11111111-2222-4333-8444-555555555555"
TABLES = ("records", "entities", "collections", "boots", "machines")


class FixtureSampler:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def capture(self, target_id: str, command: str, *, timeout: float) -> str:
        return self.stdout


class SyncStoreAccess:
    """把 ``DiagnosticStore`` 的协程包成插件要求的同步数据入口。"""

    def __init__(self, url: str) -> None:
        self._url = url

    def run(self, call: Callable[[DiagnosticStore], Coroutine[Any, Any, Any]]) -> Any:
        async def body() -> Any:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                return await call(DiagnosticStore(engine))
            finally:
                await engine.dispose()

        return asyncio.run(body())

    def clear(self) -> None:
        async def wipe(engine: AsyncEngine) -> None:
            async with engine.begin() as conn:
                for table in TABLES:
                    await conn.execute(text(f"DELETE FROM {table}"))

        async def body() -> None:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                await wipe(engine)
            finally:
                await engine.dispose()

        asyncio.run(body())

    def import_collection(
        self,
        collection: CollectionRef,
        entities: list[Any],
        records: list[Any],
        *,
        resources: ResourceAvailability,
    ) -> Any:
        return self.run(
            lambda store: store.import_collection(
                collection,
                entities,
                records,
                resources=resources,
            )
        )

    def append_records(self, records: list[Any], *, plugin_id: str, source: str) -> int:
        grouped: dict[str, list[Any]] = {}
        for record in records:
            grouped.setdefault(record["collection_id"], []).append(record)
        written = 0
        for collection_id, batch in grouped.items():
            outcome = self.run(
                lambda store, cid=collection_id, items=batch: store.append_records(cid, items)
            )
            written += outcome.records_written
        return written

    def list_metric_records(self, collection_id: str) -> list[Record]:
        return self.run(
            lambda store: store.list_records_in_collection(collection_id, kinds=[RecordKind.METRIC])
        )

    def check_integrity(self, collection_id: str) -> list[str]:
        return self.run(lambda store: store.check_collection_integrity(collection_id))

    def timeline_tracks(self) -> Any:
        return self.run(lambda store: TimelineEngine(store).tracks(MACHINE_ID, BOOT_ID))


def _stdout(*, with_memory: bool = True) -> str:
    meta = {
        "type": "meta",
        "hostname": "sas-resources",
        "boot_id": BOOT_ID,
        "cpu_count": 16,
        "kernel_version": "6.6.0-test",
    }
    previous_values = {
        "cpu_usage_usec": 0,
        "memory_current": None if not with_memory else 64 * 1024**2,
        "io_rbytes": 0,
        "io_wbytes": 0,
    }
    current_values = {
        "cpu_usage_usec": 16_000_000,
        "memory_current": None if not with_memory else 100 * 1024**2,
        "io_rbytes": 2048,
        "io_wbytes": 1024,
    }
    sample = {
        "type": "sample",
        "monotonic_ns": 1_794_367_420_210_127_000,
        "realtime_ns": 1_794_367_420_210_127_000,
        "elapsed_ns": 1_000_000_000,
        "system": {
            "cpu": {"total": 1600, "idle": 1200},
            "memory": None if not with_memory else {"used_bytes": 4 * 1024**3},
            "disk": {"read_bytes": 10_240, "write_bytes": 5_120},
        },
        "units": {"/system.slice/sshd.service": current_values},
        "cgroups": {
            "/system.slice/sshd.service": current_values,
            "/user.slice": {"memory_current": None if not with_memory else 64 * 1024**2},
        },
        "previous": {
            "system": {
                "cpu": {"total": 1000, "idle": 900},
                "memory": None,
                "disk": {"read_bytes": 1024, "write_bytes": 512},
            },
            "units": {"/system.slice/sshd.service": previous_values},
            "cgroups": {
                "/system.slice/sshd.service": previous_values,
                "/user.slice": {"memory_current": None if not with_memory else 64 * 1024**2},
            },
        },
    }
    return "\n".join((json.dumps(meta), json.dumps(sample))) + "\n"


def _collection() -> CollectionRef:
    return CollectionRef(
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        mode=CollectionMode.SSH_COLLECTION,
        created_realtime_ns=1_794_367_420_210_127_000,
    )


def _entities(records: list[dict[str, Any]]) -> list[Any]:
    return [
        MachineEntity(entity_type="machine", machine_id=MACHINE_ID, hostname="sas-resources"),
        BootEntity(entity_type="boot", boot_id=BOOT_ID, machine_id=MACHINE_ID),
        *metric_stream_entities(records),
    ]


@pytest.fixture
def access(test_database_url: str) -> Iterator[SyncStoreAccess]:
    store = SyncStoreAccess(test_database_url)
    store.clear()
    yield store


def _run_collect(access: SyncStoreAccess, stdout: str) -> CollectResult:
    registry = PluginRegistry()
    registry.register(ResourceCollector(lambda _url: FixtureSampler(stdout)))
    request = CollectRequest(
        target_id="target:resources",
        run_id=RUN_ID,
        options={
            "collection_id": COLLECTION_ID,
            "machine_id": MACHINE_ID,
            "boot_id": BOOT_ID,
            "window_seconds": 2,
        },
    )
    return registry.invoke(
        PLUGIN_ID,
        lambda plugin, context: plugin.collect(request, context),
        data=access,
        run_id=RUN_ID,
    )


def test_invoke_writes_metrics_into_postgres_and_reads_them_back(
    access: SyncStoreAccess,
) -> None:
    expected = build_records(
        parse_capture(_stdout()),
        plugin_id=PLUGIN_ID,
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
    )
    access.import_collection(
        _collection(),
        _entities(expected),
        [],
        resources=ResourceAvailability(available=True),
    )

    result = _run_collect(access, _stdout())

    assert isinstance(result, CollectResult)
    assert result.records_written == len(expected) > 0
    stored = access.list_metric_records(COLLECTION_ID)
    assert {record.record_id for record in stored} == {record["record_id"] for record in expected}
    assert access.check_integrity(COLLECTION_ID) == []

    by_name = {record.payload["name"]: record for record in stored}
    assert set(by_name) == {
        SYSTEM_CPU,
        SYSTEM_MEMORY,
        SYSTEM_DISK_READ,
        SYSTEM_DISK_WRITE,
        UNIT_CPU,
        UNIT_MEMORY,
        UNIT_DISK_READ,
        UNIT_DISK_WRITE,
        CGROUP_CPU,
        CGROUP_MEMORY,
        CGROUP_DISK_READ,
        CGROUP_DISK_WRITE,
    }
    for record in stored:
        assert record.monotonic_ns is not None
        assert record.realtime_ns is not None
        assert record.clock_domain.value == "boot_monotonic"
        assert record.entity
    assert by_name[SYSTEM_CPU].stream_id == "cpu"
    assert by_name[UNIT_CPU].stream_id.startswith("cpu.unit:")
    assert by_name[CGROUP_CPU].stream_id.startswith("cpu.cgroup:")


def test_missing_metrics_leave_no_zero_rows_in_postgres(access: SyncStoreAccess) -> None:
    expected = build_records(
        parse_capture(_stdout(with_memory=False)),
        plugin_id=PLUGIN_ID,
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
    )
    access.import_collection(
        _collection(),
        _entities(expected),
        [],
        resources=ResourceAvailability(available=True),
    )

    _run_collect(access, _stdout(with_memory=False))

    names = {record.payload["name"] for record in access.list_metric_records(COLLECTION_ID)}
    assert SYSTEM_MEMORY not in names
    assert UNIT_MEMORY not in names
    assert CGROUP_MEMORY not in names
    assert SYSTEM_CPU in names


def test_metrics_are_read_back_through_the_timeline_query(access: SyncStoreAccess) -> None:
    expected = build_records(
        parse_capture(_stdout()),
        plugin_id=PLUGIN_ID,
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
    )
    access.import_collection(
        _collection(),
        _entities(expected),
        [],
        resources=ResourceAvailability(available=True),
    )
    _run_collect(access, _stdout())

    tracks = access.timeline_tracks()
    points = [item for item in tracks.items if isinstance(item, MetricPoint)]
    by_name = {point.name: point for point in points}

    assert set(by_name) == {record["payload"]["name"] for record in expected}
    assert by_name[SYSTEM_CPU].stream_id == "cpu"
    assert by_name[UNIT_CPU].stream_id.startswith("cpu.unit:")
    assert by_name[CGROUP_CPU].stream_id.startswith("cpu.cgroup:")
    assert by_name[UNIT_CPU].entity != by_name[CGROUP_CPU].entity
    for point in points:
        assert point.monotonic_ns is not None
        assert point.realtime_ns is not None
        assert point.value >= 0
        assert point.unit
