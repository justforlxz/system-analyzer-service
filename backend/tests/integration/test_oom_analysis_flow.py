"""OOM 分析的端到端集成测试：真实 Journal 归档 → 真实 PostgreSQL → 真实分析结论。

运行方式与其它集成测试相同：``scripts/dev-db.sh start``、``alembic upgrade head``，再
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``。

链路与 Worker 将来的接法一致：``JournalArchiveImporter`` 把 fixture 导入库 →
``OomAnalyzer`` 经 ``PluginRegistry.invoke`` 读同一批事件、把结论写回同一个采集。断言全部回读库里的
实际内容，不看返回的计数就完事。

数据入口是同步的（``docs/plugin-contracts.md`` 第 4、5 节）：插件入口是同步方法，存储层的
``DiagnosticStore`` 是协程，所以这里用一个同步包装把它交给插件——每次调用在一条短命事件循环里
执行一次存储层协程，并用 ``NullPool`` 保证连接不跨事件循环复用。生产环境里这份包装属于 Core
的数据入口实现（见 ``docs/oom-analysis.md`` 第 5 节）。
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

from system_analyzer.analyzers.oom import (
    ANNOTATION_TYPE_KERNEL,
    ANNOTATION_TYPE_SYSTEMD_OOMD,
    CONFIDENCE_PROXIMITY,
    CONFIDENCE_UNIT_FROM_SAME_LINE,
    OomAnalyzer,
)
from system_analyzer.domain.bootrec import Record, RecordKind
from system_analyzer.importers.journal_archive_importer import JournalArchiveImporter
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    AnalysisRequest,
    AnalysisResult,
    ImportResult,
    ImportSource,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore, ImportStatus

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures" / "oom"
EXPORT_FIXTURE = FIXTURES / "journal.export"

HOSTNAME = "sas-oom"
MACHINE_ID = f"machine:{HOSTNAME}"
BOOT_ID = "9f2c4b7d-1e6a-4c8f-a1b2-c3d4e5f60718"
COLLECTION_ID = f"journal-import:{MACHINE_ID}:{BOOT_ID}"
ENTRY_COUNT = 5
TABLES = ("records", "entities", "collections", "boots", "machines")

KERNEL_OOM_PREFIX = "Out of memory: Killed process 4310 (firefox)"
OOMD_KILL_MESSAGE = "Killed /system.slice/tracker-extract-3.service"


class SyncStoreAccess:
    """把异步的 ``DiagnosticStore`` 包成插件要求的同步入口。

    导入与分析都是同步入口（``docs/plugin-contracts.md`` 第 4 节），所以这一个包装同时满足导入的
    ``CollectionImportAccess`` 与分析用的 ``PluginDataAccess``：前者读采集号、整份写入，后者只读
    本次启动的事件、追加结论记录。

    ``PluginDataAccess`` 不带「哪次启动」，所以范围固定在这里的那次启动上——Core 会按
    ``AnalysisRequest.boot_id`` 构造对应的数据入口，这里只是把同一个决定写死成常量。
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

    # --- 导入入口 -------------------------------------------------------------

    def get_collection(self, collection_id: str) -> Any:
        return self.run(lambda store: store.get_collection(collection_id))

    def import_collection(
        self,
        collection: Any,
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

    # --- 分析入口 -------------------------------------------------------------

    def query_records(self, *, record_kinds: Any = (), limit: int | None = None) -> Any:
        kinds = [RecordKind(kind) for kind in record_kinds]
        return self.run(
            lambda store: store.list_records_in_boot(
                MACHINE_ID, BOOT_ID, kinds=kinds or None, limit=limit
            )
        )

    def append_records(self, records: Any, *, plugin_id: str, source: str) -> int:
        """按 ``collection_id`` 分组写入：``PluginDataAccess`` 不带采集号，存储层要一个。"""
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


@pytest.fixture
def access(test_database_url: str) -> Iterator[SyncStoreAccess]:
    store = SyncStoreAccess(test_database_url)
    store.clear()
    yield store


def run_import(access: SyncStoreAccess, path: Path = EXPORT_FIXTURE) -> ImportResult:
    """按 Worker 的接法导入 fixture：经 ``PluginRegistry.invoke``，数据入口是同步包装。"""
    registry = register_builtins(PluginRegistry())
    plugin = JournalArchiveImporter()
    return registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.import_source(ImportSource(path=path), context),
        data=access,
        run_id="run-import-oom",
    )


def run_analysis(access: SyncStoreAccess) -> AnalysisResult:
    """按 Worker 的接法调分析插件：经 ``PluginRegistry.invoke``，结论写进同一个采集。"""
    registry = register_builtins(PluginRegistry())
    return registry.invoke(
        OomAnalyzer.spec.id,
        lambda plugin, context: plugin.analyze(AnalysisRequest(boot_id=BOOT_ID), context),
        data=access,
        run_id="run-analyze-oom",
    )


def stored_records(access: SyncStoreAccess, *kinds: RecordKind) -> list[Record]:
    return access.run(
        lambda store: store.list_records_in_collection(
            COLLECTION_ID, kinds=list(kinds) if kinds else None
        )
    )


def single_annotation(access: SyncStoreAccess, annotation_type: str) -> Record:
    annotations = [
        record
        for record in stored_records(access, RecordKind.ANNOTATION)
        if record.payload["annotation_type"] == annotation_type
    ]
    assert len(annotations) == 1, f"{annotation_type} 应该有且只有一条结论，实际 {len(annotations)}"
    return annotations[0]


# ---------------------------------------------------------------------------
# fixture 里两类 OOM 都在库里
# ---------------------------------------------------------------------------


def test_fixture_imports_both_oom_sources_as_events(access: SyncStoreAccess) -> None:
    result = run_import(access)

    assert result.records_written == ENTRY_COUNT
    state = access.get_collection(COLLECTION_ID)
    assert state is not None
    assert state.import_status is ImportStatus.IMPORTED

    events = stored_records(access, RecordKind.EVENT)
    kernel = next(item for item in events if item.payload["message"].startswith(KERNEL_OOM_PREFIX))
    oomd = next(item for item in events if OOMD_KILL_MESSAGE in item.payload["message"])

    # 内核 OOM 那一行没有 Unit 字段（_TRANSPORT=kernel）
    assert kernel.payload["attributes"]["fields"]["_TRANSPORT"] == "kernel"
    assert kernel.entity is None
    # systemd-oomd 那一行来自 oomd 服务本身，被杀的 cgroup 只在 MESSAGE 里
    assert oomd.payload["attributes"]["identifier"] == "systemd-oomd"
    assert oomd.entity == "service:systemd-oomd.service"


# ---------------------------------------------------------------------------
# 分析：两类 OOM 各一条结论，能指回原文
# ---------------------------------------------------------------------------


def test_analysis_writes_one_annotation_per_oom_into_the_database(
    access: SyncStoreAccess,
) -> None:
    run_import(access)

    result = run_analysis(access)

    assert isinstance(result, AnalysisResult)
    assert result.annotations_written == 2

    annotations = stored_records(access, RecordKind.ANNOTATION)
    assert len(annotations) == 2
    assert {item.payload["annotation_type"] for item in annotations} == {
        ANNOTATION_TYPE_KERNEL,
        ANNOTATION_TYPE_SYSTEMD_OOMD,
    }
    events = {item.record_id: item for item in stored_records(access, RecordKind.EVENT)}
    for annotation in annotations:
        # evidence_refs 指回同一个采集里的真实事件
        assert annotation.payload["evidence_refs"]
        for ref in annotation.payload["evidence_refs"]:
            assert ref in events
        assert annotation.payload["analyzer"] == OomAnalyzer.spec.id
        assert annotation.source_plugin == OomAnalyzer.spec.id
        assert annotation.collection_id == COLLECTION_ID
        assert annotation.payload["severity"] == "error"


def test_kernel_oom_annotation_says_what_evidence_it_used(access: SyncStoreAccess) -> None:
    run_import(access)
    run_analysis(access)

    annotation = single_annotation(access, ANNOTATION_TYPE_KERNEL)
    message = annotation.payload["message"]

    # 进程：PID 与进程名来自 OOM 那一行自己
    assert "Killed process 4310 (firefox)" in message
    assert "PID 4310 与进程名 firefox 由 payload.message 同一行给出" in message
    # Unit 只靠时间接近补出来：标成疑似，不写成因果
    assert annotation.payload["confidence"] == CONFIDENCE_PROXIMITY
    assert "只凭时间接近" in message
    assert "不能当作因果结论" in message
    # 仍然挂在对应 Unit 的服务实体上
    assert annotation.entity == "service:firefox.service"

    # 两条证据都在库里：OOM 那一行 + 结果为 oom-kill 的 Unit 失败日志
    refs = annotation.payload["evidence_refs"]
    assert len(refs) == 2
    messages = {
        item.record_id: item.payload["message"] for item in stored_records(access, RecordKind.EVENT)
    }
    assert messages[refs[0]].startswith(KERNEL_OOM_PREFIX)
    assert "Failed with result 'oom-kill'" in messages[refs[1]]


def test_oomd_annotation_is_direct_evidence_from_its_own_line(access: SyncStoreAccess) -> None:
    run_import(access)
    run_analysis(access)

    annotation = single_annotation(access, ANNOTATION_TYPE_SYSTEMD_OOMD)
    message = annotation.payload["message"]

    assert annotation.payload["confidence"] == CONFIDENCE_UNIT_FROM_SAME_LINE
    assert annotation.payload["confidence"] > CONFIDENCE_PROXIMITY
    assert "只凭时间接近" not in message
    assert "payload.attributes.identifier=systemd-oomd" in message
    assert "Unit tracker-extract-3.service 由payload.message给出" in message
    # systemd-oomd 的 Killed 行不写 PID：不猜，并在结论里说明
    assert "不给出进程归属" in message
    assert annotation.entity == "service:tracker-extract-3.service"


def test_annotations_follow_the_documented_time_axis(access: SyncStoreAccess) -> None:
    """结论落在证据那条事件的时间点上，且与证据同属一次启动、同一时钟域。"""
    run_import(access)
    run_analysis(access)

    events = {item.record_id: item for item in stored_records(access, RecordKind.EVENT)}
    for annotation in stored_records(access, RecordKind.ANNOTATION):
        source = events[annotation.payload["evidence_refs"][0]]
        assert annotation.monotonic_ns == source.monotonic_ns
        assert annotation.realtime_ns == source.realtime_ns
        assert annotation.clock_domain is source.clock_domain
        assert (annotation.machine_id, annotation.boot_id) == (MACHINE_ID, BOOT_ID)


# ---------------------------------------------------------------------------
# 可重复：重复分析不叠加互相矛盾的结论
# ---------------------------------------------------------------------------


def test_rerunning_the_analysis_does_not_duplicate_annotations(
    access: SyncStoreAccess,
) -> None:
    run_import(access)

    first = run_analysis(access)
    after_first = stored_records(access, RecordKind.ANNOTATION)
    second = run_analysis(access)
    after_second = stored_records(access, RecordKind.ANNOTATION)

    assert first.annotations_written == 2
    # 同一批输入得到同一批 record_id，第二次全部按幂等键跳过
    assert second.annotations_written == 0
    assert {item.record_id for item in after_first} == {item.record_id for item in after_second}
    assert len(after_second) == 2


def test_collection_integrity_holds_after_analysis(access: SyncStoreAccess) -> None:
    """结论记录满足数据模型：record_id 摘要、作用域、实体引用都能复核。"""
    run_import(access)
    run_analysis(access)

    assert access.run(lambda store: store.check_collection_integrity(COLLECTION_ID)) == []


def test_analyzer_is_registered_as_a_builtin_analyzer(access: SyncStoreAccess) -> None:
    registry = register_builtins(PluginRegistry())

    analyzer_ids = [plugin.spec.id for plugin in registry.by_kind(PluginKind.ANALYZER)]
    assert OomAnalyzer.spec.id in analyzer_ids
    # 既有导入插件一个都没少
    importer_ids = {plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)}
    assert {"bootrec.importer", "journal.archive"} <= importer_ids
    assert registry.by_capability("system.oom") == (registry.require(OomAnalyzer.spec.id),)
