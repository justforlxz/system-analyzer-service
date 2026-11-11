"""Journal 压缩包导入的集成测试，需要真实 PostgreSQL。

跑法见 ``docs/journal-archive-import.md``：起库、``alembic upgrade head``，再
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``。

这里走的是真实链路：上传（把样例复制成一份待导入文件）→ 插件 ``import_source`` → 存储层
``import_collection`` → 按采集 / 启动 / 实体查询。插件本身不碰数据库：它通过 ``context.data``
调存储层的整份写入入口，测试里传的就是 ``DiagnosticStore`` 实例。
"""

from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from system_analyzer.domain.bootrec import (
    AnnotationPayload,
    ClockDomain,
    RecordKind,
    compute_record_id,
)
from system_analyzer.importers.journal_archive_importer import JournalArchiveImporter
from system_analyzer.plugins.contracts import ImportSource, PluginContext
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


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
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
async def store(engine: AsyncEngine) -> DiagnosticStore:
    return DiagnosticStore(engine)


def upload(tmp_path: Path, source: Path, *, name: str | None = None) -> Path:
    """模拟「把压缩包上传到服务器」：内容原样复制到一份新文件上。"""
    target = tmp_path / (name or source.name)
    target.write_bytes(source.read_bytes())
    return target


async def run_import(store: DiagnosticStore, path: Path) -> int:
    """按 Worker 的接法调插件：``context.data`` 就是存储层，插件入口是 async。"""
    plugin = JournalArchiveImporter()
    context = PluginContext(
        plugin_id=plugin.spec.id,
        data=store,
        config={},
        run_id="run-journal-1",
    )
    result = await plugin.import_source(
        ImportSource(path=path, media_type="application/gzip"), context
    )
    return result.records_written


# ---------------------------------------------------------------------------
# 上传 → 导入 → 查询
# ---------------------------------------------------------------------------


async def test_upload_import_then_query(store: DiagnosticStore, tmp_path: Path) -> None:
    written = await run_import(store, upload(tmp_path, NATIVE_FIXTURE))

    assert written == ENTRY_COUNT

    state = await store.get_collection(COLLECTION_ID)
    assert state is not None
    assert state.import_status is ImportStatus.IMPORTED
    assert state.source_sha256 is not None
    # 整份归档没有资源采样：采集上明确标注，不是「不知道」
    assert state.resources_available is False
    assert state.resources_reason

    records = await store.list_records_in_collection(COLLECTION_ID)
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

    assert await store.count_records(COLLECTION_ID) == ENTRY_COUNT
    # 记录摘要、作用域、实体引用都自洽
    assert await store.check_collection_integrity(COLLECTION_ID) == []


async def test_import_creates_no_resource_metric_at_all(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    """验收要求：导入结果里不出现伪造的 CPU、内存、磁盘指标。"""
    await run_import(store, upload(tmp_path, NATIVE_FIXTURE))

    metrics = await store.list_records_in_collection(
        COLLECTION_ID, kinds=[RecordKind.METRIC, RecordKind.PROFILE]
    )
    assert metrics == []
    assert await store.list_metric_streams(MACHINE_ID, BOOT_ID) == []


async def test_records_are_queryable_by_boot_and_by_entity(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    await run_import(store, upload(tmp_path, NATIVE_FIXTURE))

    in_boot = await store.list_records_in_boot(MACHINE_ID, BOOT_ID)
    assert [record.monotonic_ns for record in in_boot] == sorted(
        record.monotonic_ns for record in in_boot
    )

    nginx = [record for record in in_boot if record.entity == "service:nginx.service"]
    assert len(nginx) == 1
    assert nginx[0].payload["message"].startswith("Started A high performance web server")

    services = [
        entity
        for entity in await store.list_entities(MACHINE_ID, BOOT_ID)
        if getattr(entity, "entity_type", "") == "service"
    ]
    assert {entity.entity_id for entity in services} == {
        "service:nginx.service",
        "service:session-674.scope",
        "service:sshd.service",
    }

    boot = await store.get_boot(MACHINE_ID, BOOT_ID)
    assert boot is not None
    # Journal-only 导入没有采集到启动时刻：null，不是 0，也不是估算值
    assert boot.started_realtime_ns is None


async def test_export_text_and_native_file_both_import(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    """两种输入各导一次，落到同一条采集上，事件条数不翻倍以外的差异说明在文档里。"""
    await run_import(store, upload(tmp_path, NATIVE_FIXTURE, name="boot.journal.gz"))
    native_count = await store.count_records(COLLECTION_ID)

    await run_import(store, upload(tmp_path, EXPORT_FIXTURE, name="boot.export"))
    export_count = await store.count_records(COLLECTION_ID)

    assert native_count == ENTRY_COUNT
    # 导出文本带 __CURSOR、原生文件不存游标，两边的 record_id 因此不同（见文档「已知差异」）
    assert export_count == ENTRY_COUNT * 2


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


async def test_same_archive_twice_is_idempotent_by_digest(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    path = upload(tmp_path, NATIVE_FIXTURE)

    assert await run_import(store, path) == ENTRY_COUNT
    assert await run_import(store, path) == 0

    assert await store.count_records(COLLECTION_ID) == ENTRY_COUNT
    assert await store.check_collection_integrity(COLLECTION_ID) == []


async def test_same_content_repacked_still_adds_no_records(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    """同一份日志换个打包方式（摘要因此不同）再上传，记录数不变。

    第一级幂等看整份文件的 ``source_sha256``，第二个文件摘要不同，走不到那一层；采集与记录
    都不变多，靠的是 ``collection_id`` 由 ``(machine_id, boot_id)`` 算出、``record_id`` 由内容
    算出这两条。
    """
    assert await run_import(store, upload(tmp_path, EXPORT_FIXTURE, name="first.export")) == (
        ENTRY_COUNT
    )

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

    assert await run_import(store, tar_path) == 0
    assert await store.count_records(COLLECTION_ID) == ENTRY_COUNT


async def test_without_a_digest_the_records_still_do_not_duplicate(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    """不带 ``source_sha256``：第一级幂等失效，第二级按 ``(collection_id, record_id)`` 去重。"""
    path = upload(tmp_path, EXPORT_FIXTURE)
    await run_import(store, path)

    from system_analyzer.importers import journal_archive as archive

    parsed = archive.parse_journal_archive(path)
    bundle = archive.build_bundle(parsed, source_sha256="")
    outcome = await store.import_collection(
        bundle.collection,
        bundle.entities,
        bundle.records,
        source_sha256=None,
        resources=bundle.resources,
    )

    assert outcome.records_written == 0
    assert outcome.records_skipped == ENTRY_COUNT
    assert await store.count_records(COLLECTION_ID) == ENTRY_COUNT


# ---------------------------------------------------------------------------
# 分析结论能指回原文
# ---------------------------------------------------------------------------


async def test_evidence_refs_point_back_to_imported_records(
    store: DiagnosticStore, tmp_path: Path
) -> None:
    """分析结论用 ``evidence_refs`` 指向事件记录的 ``record_id``，指回原文可查证。"""
    await run_import(store, upload(tmp_path, NATIVE_FIXTURE))
    records = await store.list_records_in_collection(COLLECTION_ID)
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
    outcome = await store.append_records(COLLECTION_ID, [annotation])

    assert outcome.records_written == 1
    stored = await store.list_records_in_collection(COLLECTION_ID, kinds=[RecordKind.ANNOTATION])
    assert [ref for ref in stored[0].payload["evidence_refs"]] == [oom.record_id]
    assert oom.record_id in {
        record.record_id for record in await store.list_records_in_collection(COLLECTION_ID)
    }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


async def test_importer_is_listed_in_the_registry_after_startup() -> None:
    """两个进程都调 ``register_builtins``，清单里至少要有本插件，且类别正确。"""
    from system_analyzer.plugins.builtins import register_builtins
    from system_analyzer.plugins.contracts import PluginKind

    registry = register_builtins(PluginRegistry())

    importer_ids = [plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)]
    assert "journal.archive" in importer_ids
