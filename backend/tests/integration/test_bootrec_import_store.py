"""bootrec 导入的真实 PostgreSQL 集成测试。

运行方式见 ``docs/implementation-baseline.md``：先起库并执行 ``alembic upgrade head``，再执行
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``，未配置时直接报错。

导入插件是同步方法（``docs/plugin-contracts.md`` 第 3.3 节），存储层的 ``DiagnosticStore`` 是协程，
所以这里用一个同步包装 ``SyncStoreAccess`` 把它交给 ``import_bootrec``：每次调用在一条短命事件
循环里执行一次存储层协程，并用 ``NullPool`` 保证连接不跨事件循环复用。生产环境里这份包装属于
Core 的数据入口实现（见 ``docs/bootrec-import.md`` 第 6 节）。

数据用仓库里的 ``docs/samples/bootrec-v1`` 三份样例与 ``tests/bootrec_fixtures.py`` 改写的坏包；
断言都回读库里的实际内容，不看导入返回的计数就完事。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from bootrec_fixtures import (
    FULL_SAMPLE,
    SAMPLE_DIR,
    bad_checksum_package,
    count_mismatch_package,
    missing_entry_package,
    missing_field_package,
    newer_major_version_package,
    reexported_package,
)
from sample_bootrec import load_sample
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    Record,
)
from system_analyzer.importers.bootrec_importer import (
    BootrecImporter,
    CollectionImportAccess,
    import_bootrec,
)
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    ImportResult,
    ImportSource,
    PluginExecutionError,
    PluginInputError,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore, ImportStatus
from system_analyzer.storage.validation import boot_entity, machine_entity

pytestmark = pytest.mark.integration

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
FULL_COLLECTION = "collection:full-0001"
JOURNAL_ONLY_COLLECTION = "collection:journal-only-0001"
RESOURCES_MISSING_COLLECTION = "collection:resources-missing-0001"
FULL_ENTITIES = 5  # 样例 entities.jsonl 有 7 行，其中 machine 与 boot 各占一张自己的表
FULL_RECORDS = 17
OTHER_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TABLES = ("records", "entities", "collections", "boots", "machines")


class SyncStoreAccess:
    """把异步的 ``DiagnosticStore`` 包成导入插件要求的同步入口。

    每次调用新建引擎与连接池（``NullPool``），用完立刻释放，因此不会出现 asyncpg 连接跨事件
    循环复用的问题；集成测试的调用次数很少，这点开销可以接受。
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def run(self, call: Callable[[DiagnosticStore], Coroutine[Any, Any, Any]]) -> Any:
        """在存储层的事件循环里执行一个协程函数。"""

        async def body() -> Any:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                return await call(DiagnosticStore(engine))
            finally:
                await engine.dispose()

        return asyncio.run(body())

    def get_collection(self, collection_id: str) -> Any:
        return self.run(lambda store: store.get_collection(collection_id))

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Any,
        records: Any,
        *,
        source_sha256: str | None = None,
        resources: Any = None,
    ) -> Any:
        return self.run(
            lambda store: store.import_collection(
                collection,
                entities,
                records,
                source_sha256=source_sha256,
                resources=resources,
            )
        )

    def clear(self) -> None:
        """每个用例从空表开始，避免上一条用例的数据被查出来。"""

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


@pytest.fixture
def access(test_database_url: str) -> Iterator[SyncStoreAccess]:
    store = SyncStoreAccess(test_database_url)
    store.clear()
    yield store


def _by_record_id(records: list[Record]) -> dict[str, dict[str, Any]]:
    return {record.record_id: record.model_dump(mode="json") for record in records}


# ---------------------------------------------------------------------------
# 合法包：导入后能通过统一查询接口读回与文件一致的数据
# ---------------------------------------------------------------------------


def test_full_sample_imports_and_reads_back(access: SyncStoreAccess) -> None:
    sample = load_sample("full.bootrec")

    outcome = import_bootrec(access, FULL_SAMPLE)

    assert outcome.status == ImportStatus.IMPORTED.value
    assert outcome.records_written == FULL_RECORDS
    assert outcome.entities_written == FULL_ENTITIES
    assert outcome.journal_entries == 3
    assert outcome.artifacts == ("artifacts/perf.data",)

    state = access.get_collection(FULL_COLLECTION)
    assert state is not None
    assert state.import_status is ImportStatus.IMPORTED
    assert state.collection == sample.collection
    assert state.source_sha256 == outcome.digest
    assert state.resources_available is True

    assert access.run(lambda store: store.count_records(FULL_COLLECTION)) == FULL_RECORDS
    in_collection = access.run(lambda store: store.list_records_in_collection(FULL_COLLECTION))
    assert _by_record_id(in_collection) == _by_record_id(sample.records)
    in_boot = access.run(lambda store: store.list_records_in_boot(MACHINE_ID, BOOT_ID))
    assert _by_record_id(in_boot) == _by_record_id(sample.records)
    assert access.run(lambda store: store.check_collection_integrity(FULL_COLLECTION)) == []

    stored = access.run(lambda store: store.list_entities(MACHINE_ID, BOOT_ID))
    assert len(stored) == FULL_ENTITIES
    assert {(entity.entity_type, entity.entity_id) for entity in stored} == {
        (entity.entity_type, entity.entity_id)
        for entity in sample.entities
        if entity.entity_type not in ("machine", "boot")
    }


def test_journal_only_sample_stores_no_resource_data(access: SyncStoreAccess) -> None:
    outcome = import_bootrec(access, SAMPLE_DIR / "journal-only.bootrec")

    assert outcome.records_written == 2
    assert outcome.journal_entries == 4
    state = access.get_collection(JOURNAL_ONLY_COLLECTION)
    assert state is not None
    assert state.resources_available is False
    assert state.resources_reason
    assert access.run(lambda store: store.list_metric_streams(MACHINE_ID, BOOT_ID)) == []
    assert access.run(lambda store: store.count_records(JOURNAL_ONLY_COLLECTION)) == 2


def test_resources_missing_sample_keeps_streams_unavailable(access: SyncStoreAccess) -> None:
    import_bootrec(access, SAMPLE_DIR / "resources-missing.bootrec")

    streams = {
        stream.entity_id: stream
        for stream in access.run(lambda store: store.list_metric_streams(MACHINE_ID, BOOT_ID))
    }
    assert [entity_id for entity_id, stream in streams.items() if not stream.available] == [
        "metric:disk.read_bytes_per_second",
        "metric:memory.used",
    ]
    assert all(stream.unavailable_reason for stream in streams.values() if not stream.available)
    assert access.run(lambda store: store.count_records(RESOURCES_MISSING_COLLECTION)) == 5


# ---------------------------------------------------------------------------
# 幂等：同一个文件再导入一次、同一份采集换一份文件导出
# ---------------------------------------------------------------------------


def test_importing_the_same_file_twice_keeps_counts(access: SyncStoreAccess) -> None:
    first = import_bootrec(access, FULL_SAMPLE)
    second = import_bootrec(access, FULL_SAMPLE)

    assert first.digest == second.digest
    assert second.records_written == 0
    assert second.skipped_reason is not None
    assert second.warnings == ()
    assert access.run(lambda store: store.count_records(FULL_COLLECTION)) == FULL_RECORDS
    assert len(access.run(lambda store: store.list_entities(MACHINE_ID, BOOT_ID))) == FULL_ENTITIES


def test_reexported_package_dedupes_by_record_key(access: SyncStoreAccess, tmp_path: Path) -> None:
    first = import_bootrec(access, FULL_SAMPLE)

    second = import_bootrec(access, reexported_package(tmp_path))

    assert second.digest != first.digest
    assert second.records_written == 0
    assert second.records_skipped == FULL_RECORDS
    assert any(FULL_COLLECTION in warning for warning in second.warnings)
    assert access.run(lambda store: store.count_records(FULL_COLLECTION)) == FULL_RECORDS


# ---------------------------------------------------------------------------
# 坏包：整份拒绝，存储里没有残留
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (missing_field_package, "E_MANIFEST_FIELD"),
        (missing_entry_package, "E_MISSING_ENTRY"),
        (count_mismatch_package, "E_COUNT_MISMATCH"),
        (bad_checksum_package, "E_SHA256_MISMATCH"),
        (newer_major_version_package, "E_UNSUPPORTED_VERSION"),
    ],
)
def test_broken_package_leaves_nothing_behind(
    access: SyncStoreAccess, tmp_path: Path, build: Any, code: str
) -> None:
    with pytest.raises(PluginInputError) as rejected:
        import_bootrec(access, build(tmp_path))

    assert code in str(rejected.value)
    assert access.get_collection(FULL_COLLECTION) is None
    assert access.run(lambda store: store.count_records(FULL_COLLECTION)) == 0
    assert access.run(lambda store: store.list_collections()) == []
    assert access.run(lambda store: store.get_machine(MACHINE_ID)) is None


def test_collection_id_used_by_another_boot_is_rejected_and_original_kept(
    access: SyncStoreAccess,
) -> None:
    sample = load_sample("full.bootrec")
    machine = machine_entity(sample.entities)
    boot = boot_entity(sample.entities)
    assert isinstance(machine, MachineEntity)
    assert isinstance(boot, BootEntity)
    access.run(
        lambda store: store.register_collection(
            CollectionRef(
                collection_id=FULL_COLLECTION,
                machine_id=MACHINE_ID,
                boot_id=OTHER_BOOT,
                mode=CollectionMode.SSH_COLLECTION,
                created_realtime_ns=sample.collection.created_realtime_ns,
            ),
            machine,
            BootEntity(
                entity_type="boot",
                boot_id=OTHER_BOOT,
                machine_id=MACHINE_ID,
                started_realtime_ns=boot.started_realtime_ns,
            ),
        )
    )

    with pytest.raises(PluginInputError) as rejected:
        import_bootrec(access, FULL_SAMPLE)

    assert OTHER_BOOT in str(rejected.value)
    state = access.get_collection(FULL_COLLECTION)
    assert state is not None
    assert state.collection.boot_id == OTHER_BOOT
    assert access.run(lambda store: store.count_records(FULL_COLLECTION)) == 0
    assert access.run(lambda store: store.list_records_in_boot(MACHINE_ID, BOOT_ID)) == []
    assert access.run(lambda store: store.list_records_in_boot(MACHINE_ID, OTHER_BOOT)) == []


# ---------------------------------------------------------------------------
# 插件路径：和 Core 一样经 registry.invoke 调插件，数据入口是同步包装
# ---------------------------------------------------------------------------


def test_plugin_imports_through_registry_invoke(access: SyncStoreAccess) -> None:
    registry = register_builtins(PluginRegistry())
    plugin = BootrecImporter()
    source = ImportSource(path=FULL_SAMPLE, media_type="application/zip")

    assert isinstance(access, CollectionImportAccess)
    assert plugin.detect(source) is True
    result = registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.import_source(source, context),
        data=access,
    )

    assert isinstance(result, ImportResult)
    assert result.records_written == FULL_RECORDS
    assert access.run(lambda store: store.count_records(FULL_COLLECTION)) == FULL_RECORDS
    assert access.run(lambda store: store.check_collection_integrity(FULL_COLLECTION)) == []


def test_plugin_reports_a_failing_storage_layer() -> None:
    """把数据入口换成「读采集就抛异常」的替身：错误由契约边界收敛成 PluginExecutionError。"""

    class BrokenAccess(SyncStoreAccess):
        def get_collection(self, collection_id: str) -> Any:
            raise RuntimeError("QA 注入的存储层缺陷")

    registry = register_builtins(PluginRegistry())
    plugin = BootrecImporter()

    with pytest.raises(PluginExecutionError) as wrapped:
        registry.invoke(
            plugin.spec.id,
            lambda loaded, context: loaded.import_source(ImportSource(path=FULL_SAMPLE), context),
            data=BrokenAccess(""),
        )

    assert isinstance(wrapped.value.cause, RuntimeError)
