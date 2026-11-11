"""Journal 压缩包导入的真实 PostgreSQL 集成测试。

运行方式见 ``docs/journal-archive-import.md``：起库、``alembic upgrade head``，再
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``。

导入插件是同步方法（``docs/plugin-contracts.md`` 第 3.3 节），存储层的 ``DiagnosticStore`` 是协程，
所以这里用一个同步包装 ``SyncStoreAccess`` 把它交给插件：每次调用在一条短命事件循环里执行一次
存储层协程，并用 ``NullPool`` 保证连接不跨事件循环复用。生产环境里这份包装属于 Core 的数据入口
实现（见 ``docs/journal-archive-import.md`` 第 2 节）。

链路与 Worker 将来的接法一致：``PluginRegistry.invoke`` 调插件 → 插件把整份采集交给数据入口。
断言都回读库里的实际内容，不看导入返回的计数就完事。
"""

from __future__ import annotations

import asyncio
import gzip
import io
import tarfile
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.domain.bootrec import (
    AnnotationPayload,
    ClockDomain,
    CollectionRef,
    RecordKind,
    compute_record_id,
)
from system_analyzer.importers.bootrec_importer import CollectionImportAccess
from system_analyzer.importers.journal_archive_importer import JournalArchiveImporter
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    ImportResult,
    ImportSource,
    PluginContext,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore, ImportStatus

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures" / "journal_archive"
EXPORT_FIXTURE = FIXTURES / "boot.export"
NATIVE_FIXTURE = FIXTURES / "boot.journal.gz"

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
COLLECTION_ID = f"journal-import:{MACHINE_ID}:{BOOT_ID}"
ENTRY_COUNT = 4
TABLES = ("records", "entities", "collections", "boots", "machines")


class SyncStoreAccess:
    """把异步的 ``DiagnosticStore`` 包成导入插件要求的同步入口。

    每次调用新建引擎与 ``NullPool`` 连接池，用完立刻释放，所以不会出现 asyncpg 连接跨事件循环
    复用的问题；集成测试的调用次数很少，这点开销可以接受。
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def run(self, call: Callable[[DiagnosticStore], Coroutine[Any, Any, Any]]) -> Any:
        """在存储层自己的事件循环里执行一个协程函数。"""

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


def upload(tmp_path: Path, source: Path, *, name: str | None = None) -> Path:
    """模拟「把压缩包上传到服务器」：内容原样复制到一份新文件上。"""
    target = tmp_path / (name or source.name)
    target.write_bytes(source.read_bytes())
    return target


def run_import(access: SyncStoreAccess, path: Path) -> ImportResult:
    """按 Worker 的接法调插件：经 ``PluginRegistry.invoke``，数据入口是同步包装。"""
    registry = register_builtins(PluginRegistry())
    plugin = JournalArchiveImporter()
    context = PluginContext(
        plugin_id=plugin.spec.id,
        data=access,
        config={},
        run_id="run-journal-1",
    )
    return registry.invoke(
        plugin.spec.id,
        lambda loaded, ctx: loaded.import_source(
            ImportSource(path=path, media_type="application/gzip"), ctx
        ),
        data=context.data,
        config=context.config,
        run_id=context.run_id,
    )


# ---------------------------------------------------------------------------
# 上传 → 导入 → 查询
# ---------------------------------------------------------------------------


def test_upload_import_then_query(access: SyncStoreAccess, tmp_path: Path) -> None:
    result = run_import(access, upload(tmp_path, NATIVE_FIXTURE))

    assert isinstance(result, ImportResult)
    assert result.records_written == ENTRY_COUNT

    state = access.get_collection(COLLECTION_ID)
    assert state is not None
    assert state.import_status is ImportStatus.IMPORTED
    assert state.source_sha256 is not None
    # 整份归档没有资源采样：采集上明确标注，不是「不知道」
    assert state.resources_available is False
    assert state.resources_reason

    records = access.run(lambda store: store.list_records_in_collection(COLLECTION_ID))
    assert len(records) == ENTRY_COUNT
    assert {record.kind for record in records} == {RecordKind.EVENT}
    assert {record.source_plugin for record in records} == {"journal.archive"}
    assert all(record.payload["attributes"]["derived_from"] == "journal" for record in records)

    # 时间换算：第一条事件就是 docs/time-base.md 第 5.3 节的示例值
    first = records[0]
    assert first.monotonic_ns == 175830828563000
    assert first.realtime_ns == 1794367420210127000
    assert first.clock_domain is ClockDomain.BOOT_MONOTONIC

    # 来源可追溯：原文关键字段都在
    attributes = first.payload["attributes"]
    assert attributes["unit"] == "session-674.scope"
    assert attributes["pid"] == 3439748
    assert attributes["invocation_id"] == "39b369d1e917432bb0a9bb9d4df7d03e"
    assert attributes["fields"]["_HOSTNAME"] == "uos-PC"
    assert first.payload["message"].startswith("     uos : PWD=/home/uos")

    assert access.run(lambda store: store.count_records(COLLECTION_ID)) == ENTRY_COUNT
    # 记录摘要、作用域、实体引用都自洽
    assert access.run(lambda store: store.check_collection_integrity(COLLECTION_ID)) == []


def test_import_creates_no_resource_metric_at_all(access: SyncStoreAccess, tmp_path: Path) -> None:
    """验收要求：导入结果里不出现伪造的 CPU、内存、磁盘指标。"""
    run_import(access, upload(tmp_path, NATIVE_FIXTURE))

    metrics = access.run(
        lambda store: store.list_records_in_collection(
            COLLECTION_ID, kinds=[RecordKind.METRIC, RecordKind.PROFILE]
        )
    )
    assert metrics == []
    assert access.run(lambda store: store.list_metric_streams(MACHINE_ID, BOOT_ID)) == []


def test_records_are_queryable_by_boot_and_by_entity(
    access: SyncStoreAccess, tmp_path: Path
) -> None:
    run_import(access, upload(tmp_path, NATIVE_FIXTURE))

    in_boot = access.run(lambda store: store.list_records_in_boot(MACHINE_ID, BOOT_ID))
    assert [record.monotonic_ns for record in in_boot] == sorted(
        record.monotonic_ns for record in in_boot
    )

    nginx = [record for record in in_boot if record.entity == "service:nginx.service"]
    assert len(nginx) == 1
    assert nginx[0].payload["message"].startswith("Started A high performance web server")

    services = [
        entity
        for entity in access.run(lambda store: store.list_entities(MACHINE_ID, BOOT_ID))
        if getattr(entity, "entity_type", "") == "service"
    ]
    assert {entity.entity_id for entity in services} == {
        "service:nginx.service",
        "service:session-674.scope",
        "service:sshd.service",
    }

    boot = access.run(lambda store: store.get_boot(MACHINE_ID, BOOT_ID))
    assert boot is not None
    # Journal-only 导入没有采集到启动时刻：null，不是 0，也不是估算值
    assert boot.started_realtime_ns is None


def test_export_text_and_native_file_both_import(access: SyncStoreAccess, tmp_path: Path) -> None:
    """两种输入各导一次，落到同一条采集上；游标只在导出文本里，所以记录会各算一条。"""
    run_import(access, upload(tmp_path, NATIVE_FIXTURE, name="boot.journal.gz"))
    native_count = access.run(lambda store: store.count_records(COLLECTION_ID))

    run_import(access, upload(tmp_path, EXPORT_FIXTURE, name="boot.export"))
    export_count = access.run(lambda store: store.count_records(COLLECTION_ID))

    assert native_count == ENTRY_COUNT
    assert export_count == ENTRY_COUNT * 2


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_same_archive_twice_is_idempotent_by_digest(
    access: SyncStoreAccess, tmp_path: Path
) -> None:
    path = upload(tmp_path, NATIVE_FIXTURE)

    assert run_import(access, path).records_written == ENTRY_COUNT
    assert run_import(access, path).records_written == 0

    assert access.run(lambda store: store.count_records(COLLECTION_ID)) == ENTRY_COUNT
    assert access.run(lambda store: store.check_collection_integrity(COLLECTION_ID)) == []


def test_same_content_repacked_still_adds_no_records(
    access: SyncStoreAccess, tmp_path: Path
) -> None:
    """同一份日志换个打包方式（摘要因此不同）再上传，记录数不变。

    第一级幂等看整份文件的 ``source_sha256``，第二个文件摘要不同，走不到那一层；采集与记录
    都不变多，靠的是 ``collection_id`` 由 ``(machine_id, boot_id)`` 算出、``record_id`` 由内容
    算出这两条。
    """
    assert run_import(
        access, upload(tmp_path, EXPORT_FIXTURE, name="first.export")
    ).records_written == (ENTRY_COUNT)

    tar_path = tmp_path / "archive.tar.gz"
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            data = EXPORT_FIXTURE.read_bytes()
            info = tarfile.TarInfo("journal/boot.export")
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    tar_path.write_bytes(buffer.getvalue())

    assert run_import(access, tar_path).records_written == 0
    assert access.run(lambda store: store.count_records(COLLECTION_ID)) == ENTRY_COUNT


def test_without_a_digest_the_records_still_do_not_duplicate(
    access: SyncStoreAccess, tmp_path: Path
) -> None:
    """不带 ``source_sha256``：第一级幂等失效，第二级按 ``(collection_id, record_id)`` 去重。"""
    from system_analyzer.importers import journal_archive as archive

    path = upload(tmp_path, EXPORT_FIXTURE)
    run_import(access, path)

    parsed = archive.parse_journal_archive(path)
    bundle = archive.build_bundle(parsed, source_sha256="")
    outcome = access.run(
        lambda store: store.import_collection(
            bundle.collection,
            bundle.entities,
            bundle.records,
            source_sha256=None,
            resources=bundle.resources,
        )
    )

    assert outcome.records_written == 0
    assert outcome.records_skipped == ENTRY_COUNT
    assert access.run(lambda store: store.count_records(COLLECTION_ID)) == ENTRY_COUNT


# ---------------------------------------------------------------------------
# 分析结论能指回原文
# ---------------------------------------------------------------------------


def test_evidence_refs_point_back_to_imported_records(
    access: SyncStoreAccess, tmp_path: Path
) -> None:
    """分析结论用 ``evidence_refs`` 指向事件记录的 ``record_id``，指回原文可查证。"""
    run_import(access, upload(tmp_path, NATIVE_FIXTURE))
    records = access.run(lambda store: store.list_records_in_collection(COLLECTION_ID))
    oom = next(record for record in records if "Out of memory" in record.payload["message"])

    payload: dict[str, Any] = AnnotationPayload(
        annotation_type="oom.suspected",
        analyzer="manual.check",
        severity="error",
        message="内核日志里有 OOM 记录",
        evidence_refs=[oom.record_id],
    ).model_dump(mode="json")
    annotation = {
        "kind": RecordKind.ANNOTATION.value,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "manual.check",
        "stream_id": "analysis",
        "monotonic_ns": oom.monotonic_ns,
        "realtime_ns": oom.realtime_ns,
        "clock_domain": oom.clock_domain.value,
        "entity": None,
        "payload": payload,
    }
    annotation = {"record_id": compute_record_id(annotation), **annotation}
    outcome = access.run(lambda store: store.append_records(COLLECTION_ID, [annotation]))

    assert outcome.records_written == 1
    stored = access.run(
        lambda store: store.list_records_in_collection(COLLECTION_ID, kinds=[RecordKind.ANNOTATION])
    )
    assert [ref for ref in stored[0].payload["evidence_refs"]] == [oom.record_id]
    assert oom.record_id in {
        record.record_id
        for record in access.run(lambda store: store.list_records_in_collection(COLLECTION_ID))
    }


# ---------------------------------------------------------------------------
# 契约要求的调用形状与注册表
# ---------------------------------------------------------------------------


def test_data_access_matches_the_sync_protocol(access: SyncStoreAccess) -> None:
    """数据入口满足 1.3 定下的同步协议，插件能直接用它。"""
    assert isinstance(access, CollectionImportAccess)


def test_plugin_imports_through_registry_invoke(access: SyncStoreAccess, tmp_path: Path) -> None:
    """与 Core 一样经 ``registry.invoke`` 调插件：拿到真实 ``ImportResult``，记录确实进了库。"""
    registry = register_builtins(PluginRegistry())
    plugin = JournalArchiveImporter()
    source = ImportSource(path=upload(tmp_path, NATIVE_FIXTURE))

    assert plugin.detect(source) is True
    result = registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.import_source(source, context),
        data=access,
    )

    assert isinstance(result, ImportResult)
    assert result.records_written == ENTRY_COUNT
    assert access.run(lambda store: store.count_records(COLLECTION_ID)) == ENTRY_COUNT
    assert access.run(lambda store: store.check_collection_integrity(COLLECTION_ID)) == []


def test_importer_is_listed_in_the_registry_after_startup() -> None:
    """两个进程都调 ``register_builtins``：内置清单里两个导入插件都在。"""
    registry = register_builtins(PluginRegistry())

    importer_ids = [plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)]
    assert "journal.archive" in importer_ids
    assert "bootrec.importer" in importer_ids
