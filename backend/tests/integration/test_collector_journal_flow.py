"""远程 Journal 采集的真实 PostgreSQL 集成测试（任务 3.5）。

覆盖两件事：

1. 经 ``PluginRegistry.invoke`` 调采集插件，记录追加到同一次采集并能从库里读回，
   且与资源采样按 ``monotonic_ns`` 排在同一条 boot 时间轴上；
2. 插件留下的导出文件能被任务 1.4 的 ``JournalArchiveImporter`` 识别并导入。

远程入口是假的，数据库是真的。测试库地址取自 ``SAS_TEST_DATABASE_URL``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.collectors.journal import PLUGIN_ID, JournalCollector
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    GenericEntity,
    MachineEntity,
    MetricStreamEntity,
    RecordKind,
    ResourceAvailability,
    compute_record_id,
)
from system_analyzer.importers.journal_archive_importer import JournalArchiveImporter
from system_analyzer.plugins.contracts import (
    CollectRequest,
    CollectResult,
    ImportSource,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore, ImportStatus
from test_collector_journal import BOOT_ID, MACHINE_ID, FakeJournalRemote, _entry

pytestmark = pytest.mark.integration

COLLECTION_ID = "collection:ssh-3-5"
RUN_ID = "run-3-5-integration"
TABLES = ("records", "entities", "collections", "boots", "machines")


class StoreHarness:
    """用短命事件循环驱动异步存储层，模拟 Worker 的同步数据入口。"""

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
        async def wipe() -> None:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    for table in TABLES:
                        await connection.execute(text(f"DELETE FROM {table}"))
            finally:
                await engine.dispose()

        asyncio.run(wipe())

    def append_records(
        self,
        records: Sequence[Any],
        *,
        plugin_id: str,
        source: str,
    ) -> int:
        outcome = self.run(lambda store: store.append_records(COLLECTION_ID, list(records)))
        return outcome.records_written

    def query_records(
        self,
        *,
        record_kinds: Sequence[str] = (),
        limit: int | None = None,
    ) -> Sequence[Any]:
        kinds = [RecordKind(kind) for kind in record_kinds] or None
        return self.run(
            lambda store: store.list_records_in_collection(COLLECTION_ID, kinds=kinds, limit=limit)
        )

    def import_collection(self, collection: Any, entities: Any, records: Any, **kwargs: Any) -> Any:
        return self.run(
            lambda store: store.import_collection(collection, entities, records, **kwargs)
        )


@pytest.fixture
def harness(test_database_url: str):
    store = StoreHarness(test_database_url)
    store.clear()
    return store


def _metric_record(collection_id: str) -> dict[str, Any]:
    body = {
        "kind": RecordKind.METRIC.value,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": collection_id,
        "source_plugin": "resources.collector",
        "stream_id": "resources.cpu",
        "monotonic_ns": 1_700_000_000,
        "realtime_ns": 1_794_367_100_000_000,
        "clock_domain": "boot_monotonic",
        "entity": "metric:cpu.total.utilization",
        "payload": {
            "name": "cpu.total.utilization",
            "value": 12.5,
            "unit": "percent",
        },
    }
    return {"record_id": compute_record_id(body), **body}


def _base_entities() -> tuple[Any, ...]:
    return (
        MachineEntity(
            entity_type="machine",
            machine_id=MACHINE_ID,
            hostname="uos-PC",
            kernel_version="6.6.0-amd64-desktop",
        ),
        BootEntity(
            entity_type="boot",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            started_realtime_ns=1_794_367_000_000_000,
            kernel_version="6.6.0-amd64-desktop",
        ),
        MetricStreamEntity(
            entity_type="metric_stream",
            entity_id="metric:cpu.total.utilization",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            name="cpu.total.utilization",
            unit="percent",
            available=True,
        ),
        GenericEntity(
            entity_type="service",
            entity_id="service:firefox.service",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            name="firefox.service",
            attributes={"unit": "firefox.service"},
        ),
    )


def _journal_entries() -> list[dict[str, str]]:
    return [
        _entry(
            index,
            message=f"firefox.service: journal line {index}",
            extra={
                "_SYSTEMD_UNIT": "firefox.service",
                "_SYSTEMD_CGROUP": "/system.slice/firefox.service",
                "SYSLOG_IDENTIFIER": "systemd",
            },
            monotonic_us=1_700_500_000 + index * 100_000,
        )
        for index in (1, 2)
    ]


def _import_base(harness: StoreHarness) -> None:
    collection = CollectionRef(
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        mode=CollectionMode.SSH_COLLECTION,
        created_realtime_ns=1_794_367_000_000_000,
    )
    outcome = harness.import_collection(
        collection,
        _base_entities(),
        [_metric_record(COLLECTION_ID)],
        resources=ResourceAvailability(available=True),
    )
    assert outcome.status is ImportStatus.IMPORTED
    assert outcome.records_written == 1


def _invoke_collector(harness: StoreHarness, tmp_path: Path) -> CollectResult:
    remote = FakeJournalRemote(_journal_entries())
    plugin = JournalCollector(remote_factory=lambda _options: remote)
    registry = PluginRegistry()
    registry.register(plugin)
    request = CollectRequest(
        target_id="uos-152",
        run_id=RUN_ID,
        options={
            "collection_id": COLLECTION_ID,
            "machine_id": MACHINE_ID,
            "work_dir": str(tmp_path),
        },
    )
    return registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.collect(request, context),
        data=harness,
        run_id=RUN_ID,
    )


def test_collector_writes_journal_into_the_same_collection(
    harness: StoreHarness, tmp_path: Path
) -> None:
    _import_base(harness)

    result = _invoke_collector(harness, tmp_path)

    assert isinstance(result, CollectResult)
    assert result.records_written == 2
    entities_file = tmp_path / f"journal-{RUN_ID}.entities.jsonl"
    assert str(entities_file) in result.artifacts
    exported = [
        json.loads(line) for line in entities_file.read_text(encoding="utf-8").splitlines() if line
    ]
    exported_ids = {row.get("entity_id") for row in exported}
    assert "service:firefox.service" in exported_ids
    assert all(row["machine_id"] == MACHINE_ID for row in exported)
    assert all(row.get("boot_id", BOOT_ID) == BOOT_ID for row in exported)
    assert harness.run(lambda store: store.count_records(COLLECTION_ID)) == 3
    assert harness.run(lambda store: store.check_collection_integrity(COLLECTION_ID)) == []

    stored = harness.run(
        lambda store: store.list_records_in_boot(MACHINE_ID, BOOT_ID, kinds=[RecordKind.EVENT])
    )
    assert [record.payload["attributes"]["fields"]["__CURSOR"] for record in stored] == [
        "cursor-1",
        "cursor-2",
    ]
    assert [record.payload["attributes"]["unit"] for record in stored] == [
        "firefox.service",
        "firefox.service",
    ]

    timeline = harness.run(lambda store: store.list_records_in_boot(MACHINE_ID, BOOT_ID))
    assert [record.kind for record in timeline] == [
        RecordKind.METRIC,
        RecordKind.EVENT,
        RecordKind.EVENT,
    ]
    assert [record.monotonic_ns for record in timeline] == [
        1_700_000_000,
        1_700_600_000_000,
        1_700_700_000_000,
    ]


def test_export_artifact_is_accepted_by_the_1_4_importer(
    harness: StoreHarness, tmp_path: Path
) -> None:
    _import_base(harness)
    _invoke_collector(harness, tmp_path)

    export = tmp_path / f"journal-{RUN_ID}.export"
    importer = JournalArchiveImporter()
    assert importer.detect(ImportSource(path=export)) is True

    registry = PluginRegistry()
    registry.register(importer)
    result = registry.invoke(
        importer.spec.id,
        lambda loaded, context: loaded.import_source(ImportSource(path=export), context),
        data=_ImportAccess(harness),
    )

    imported_collection = f"journal-import:{MACHINE_ID}:{BOOT_ID}"
    assert result.records_written == 2
    assert harness.run(lambda store: store.count_records(imported_collection)) == 2


class _ImportAccess:
    """1.4 导入插件要求的同步整份写入入口。"""

    last_outcome: Any = None

    def __init__(self, harness: StoreHarness) -> None:
        self._harness = harness

    def get_collection(self, collection_id: str) -> Any:
        return self._harness.run(lambda store: store.get_collection(collection_id))

    def import_collection(self, collection: Any, entities: Any, records: Any, **kwargs: Any) -> Any:
        outcome = self._harness.import_collection(collection, entities, records, **kwargs)
        self.last_outcome = outcome
        return outcome


def test_builtins_still_register_the_journal_collector() -> None:
    registry = PluginRegistry()
    from system_analyzer.plugins.builtins import register_builtins

    register_builtins(registry)
    assert PLUGIN_ID in [plugin.spec.id for plugin in registry.by_kind(PluginKind.COLLECTOR)]
