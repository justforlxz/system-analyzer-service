"""Systemd 生命周期分析的真实 PostgreSQL 集成测试。

链路与将来的 Worker 接法一致：先把固定 Journal fixture 经任务 1.4 的导入路径落库，再经
``PluginRegistry.invoke`` 调分析插件，插件通过同步数据入口读事件记录、把区间与结论追加回同一次
采集，最后从库里回读，逐条核对：

- 结论与区间真的写进了库（不是只拿到 ``AnalysisResult`` 计数）；
- 每条结论的 ``evidence_refs`` 都能在库里找到对应的原始 Journal 记录；
- 库里的记录通过 1.1 的完整性复核（``check_collection_integrity``），说明产出符合 0.2 / 0.3 的规则；
- 重复分析是幂等的：第二次不新增结论，也不会写出互相矛盾的区间。

运行方式见 ``docs/implementation-baseline.md``：先 ``scripts/dev-db.sh start`` 与
``alembic upgrade head``，再 ``uv run pytest -m integration``。测试库地址取自
``SAS_TEST_DATABASE_URL``。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.analyzers.systemd_lifecycle import (
    ANNOTATION_LIFECYCLE_INCOMPLETE,
    ANNOTATION_SERVICE_FAILED,
    ANNOTATION_UNATTRIBUTED,
    PLUGIN_ID,
    SPAN_ACTIVE,
)
from system_analyzer.analyzers.systemd_lifecycle_analyzer import (
    MACHINE_ID_OPTION,
    SystemdLifecycleAnalyzer,
)
from system_analyzer.domain.bootrec import Record, RecordKind, compute_record_id
from system_analyzer.importers import journal_archive as archive
from system_analyzer.plugins.contracts import AnalysisRequest, AnalysisResult
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "systemd_lifecycle" / "boot.export"
REAL_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "systemd_lifecycle" / "real-boot.journal.gz"
)
"""真实 ``journalctl -o export`` 截取：Unit 取自 ``UNIT=`` / ``USER_UNIT=``，导入只为
``_SYSTEMD_UNIT`` 建过实体（PID 1 的 ``init.scope``），分析必须自己补齐被分析 Unit 的实体。"""

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "7f2c5d4e-9a1b-4c3d-8e2f-1a2b3c4d5e6f"
COLLECTION_ID = f"journal-import:{MACHINE_ID}:{BOOT_ID}"
TABLES = ("records", "entities", "collections", "boots", "machines", "jobs")

EXPECTED_SPANS = 5
EXPECTED_ANNOTATIONS = 7


class SyncLifecycleAccess:
    """把异步存储层包成分析插件要求的同步入口（测试专用桥接）。

    生产环境里这份桥接属于 Core 的数据入口实现，与 ``api/sync_store.py`` 对导入插件做的事一样：
    每次调用开一条短命事件循环，连接池用 ``NullPool``，连接不跨事件循环复用。插件只看到同步方法，
    不会出现「调用成功但什么都没写」的静默错配。
    """

    def __init__(self, url: str) -> None:
        self._url = url

    def list_records_in_boot(
        self,
        machine_id: str,
        boot_id: str,
        *,
        kinds: Sequence[RecordKind] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        return self._run(
            lambda store: store.list_records_in_boot(machine_id, boot_id, kinds=kinds, limit=limit)
        )

    def ensure_entities(self, collection_id: str, entities: Sequence[Any]) -> int:
        return self._run(lambda store: _ensure_entities(store, collection_id, entities))

    def append_records(self, collection_id: str, records: Sequence[Any]) -> int:
        outcome = self._run(lambda store: store.append_records(collection_id, records))
        return outcome.records_written

    def _run(self, call: Callable[[DiagnosticStore], Coroutine[Any, Any, Any]]) -> Any:
        async def body() -> Any:
            engine = create_async_engine(self._url, poolclass=NullPool)
            try:
                return await call(DiagnosticStore(engine))
            finally:
                await engine.dispose()

        return asyncio.run(body())


async def _ensure_entities(
    store: DiagnosticStore, collection_id: str, entities: Sequence[Any]
) -> int:
    """把实体补进这次启动：1.1 目前只有整份采集写入能写实体，这里复用它（幂等）。

    Core 接线时应当提供等价的入口（专用方法或复用 ``import_collection``），插件只声明需要它。
    """
    state = await store.get_collection(collection_id)
    if state is None:
        raise RuntimeError(f"采集 {collection_id} 不在库里，无法补实体")
    machine = await store.get_machine(state.collection.machine_id)
    boot = await store.get_boot(state.collection.machine_id, state.collection.boot_id)
    if machine is None or boot is None:
        raise RuntimeError(f"采集 {collection_id} 的机器或启动行缺失")
    existing = await store.list_entities(state.collection.machine_id, state.collection.boot_id)
    merged = {entity.entity_id: entity for entity in [*existing, *entities]}
    outcome = await store.import_collection(state.collection, [machine, boot, *merged.values()], ())
    return outcome.entities_written


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例从空表开始。"""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def import_journal_fixture(engine: AsyncEngine, fixture: Path = FIXTURE) -> DiagnosticStore:
    """按任务 1.4 的归一化路径把 fixture 落库，返回可用的存储层对象。"""
    parsed = archive.parse_journal_archive(fixture)
    bundle = archive.build_bundle(parsed, source_sha256=archive.archive_digest(fixture))
    store = DiagnosticStore(engine)
    outcome = await store.import_collection(
        bundle.collection,
        bundle.entities,
        bundle.records,
        source_sha256=bundle.source_sha256,
        resources=bundle.resources,
    )
    assert outcome.records_written == len(bundle.records)
    return store


async def import_old_style_journal(engine: AsyncEngine) -> tuple[DiagnosticStore, Any]:
    """按旧版 systemd 导出的形状落库：生命周期消息没有 UNIT= / USER_UNIT= / INVOCATION_ID=。

    1.3 的导入校验只验 ``EventPayload`` 的形状，不会拦住这种输入——这正是分析器要自己兜住的情况。
    """
    parsed = archive.parse_journal_archive(REAL_FIXTURE)
    bundle = archive.build_bundle(parsed, source_sha256=archive.archive_digest(REAL_FIXTURE))
    stripped: list[dict[str, Any]] = []
    for record in bundle.records:
        copy = deepcopy(record)
        fields = copy["payload"]["attributes"]["fields"]
        for name in ("UNIT", "USER_UNIT", "INVOCATION_ID", "USER_INVOCATION_ID"):
            fields.pop(name, None)
        # 内容变了，record_id 要按内容重算：1.1 的写入校验会核对 id 与内容是否一致
        copy["record_id"] = compute_record_id(copy)
        stripped.append(copy)
    store = DiagnosticStore(engine)
    outcome = await store.import_collection(
        bundle.collection,
        bundle.entities,
        tuple(stripped),
        source_sha256=bundle.source_sha256,
        resources=bundle.resources,
    )
    assert outcome.records_written == len(stripped)
    return store, bundle


async def run_analysis(
    database_url: str,
    *,
    record_ids: tuple[str, ...] = (),
    machine_id: str = MACHINE_ID,
    boot_id: str = BOOT_ID,
) -> AnalysisResult:
    """按 Worker 的接法调分析插件：经 ``PluginRegistry.invoke``，在 Worker 线程里同步执行。"""
    registry = PluginRegistry()
    registry.register(SystemdLifecycleAnalyzer())
    request = AnalysisRequest(
        boot_id=boot_id,
        record_ids=record_ids,
        options={MACHINE_ID_OPTION: machine_id},
    )
    access = SyncLifecycleAccess(database_url)
    return await asyncio.to_thread(
        registry.invoke,
        PLUGIN_ID,
        lambda plugin, context: plugin.analyze(request, context),
        data=access,
    )


async def derived_records(
    store: DiagnosticStore, *, machine_id: str = MACHINE_ID, boot_id: str = BOOT_ID
) -> list[Record]:
    records = await store.list_records_in_boot(
        machine_id,
        boot_id,
        kinds=[RecordKind.SPAN, RecordKind.ANNOTATION],
    )
    return [record for record in records if record.source_plugin == PLUGIN_ID]


async def test_analysis_writes_spans_and_conclusions_into_the_database(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store = await import_journal_fixture(engine)

    result = await run_analysis(test_database_url)

    assert result == AnalysisResult(annotations_written=EXPECTED_ANNOTATIONS)
    records = await derived_records(store)
    spans = [record for record in records if record.kind is RecordKind.SPAN]
    annotations = [record for record in records if record.kind is RecordKind.ANNOTATION]
    assert len(spans) == EXPECTED_SPANS
    assert len(annotations) == EXPECTED_ANNOTATIONS
    assert {record.collection_id for record in records} == {COLLECTION_ID}


async def test_conclusions_point_back_to_the_original_journal_records(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store = await import_journal_fixture(engine)
    journal_records = await store.list_records_in_boot(
        MACHINE_ID, BOOT_ID, kinds=[RecordKind.EVENT]
    )
    journal_ids = {record.record_id for record in journal_records}

    await run_analysis(test_database_url)

    annotations = [
        record for record in await derived_records(store) if record.kind is RecordKind.ANNOTATION
    ]
    assert annotations
    for annotation in annotations:
        evidence = annotation.payload["evidence_refs"]
        assert evidence, f"{annotation.payload['annotation_type']} 没有证据"
        assert set(evidence) <= journal_ids
        assert annotation.payload["analyzer"] == PLUGIN_ID


async def test_written_records_pass_the_storage_integrity_check(
    engine: AsyncEngine, test_database_url: str
) -> None:
    """产出的记录必须仍然满足 0.2 的字段与关联规则，1.1 的复核一句话都不该报。"""
    store = await import_journal_fixture(engine)

    await run_analysis(test_database_url)

    assert await store.check_collection_integrity(COLLECTION_ID) == []


async def test_repeated_analysis_is_idempotent(engine: AsyncEngine, test_database_url: str) -> None:
    store = await import_journal_fixture(engine)

    first = await run_analysis(test_database_url)
    before = {(record.record_id, record.kind) for record in await derived_records(store)}
    second = await run_analysis(test_database_url)
    after = {(record.record_id, record.kind) for record in await derived_records(store)}

    assert first.annotations_written == EXPECTED_ANNOTATIONS
    # 第二次一条都没新增：结论按内容摘要幂等，不会叠加出互相矛盾的记录
    assert second.annotations_written == 0
    assert before == after


async def test_the_rebuilt_intervals_are_readable_from_the_database(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store = await import_journal_fixture(engine)

    await run_analysis(test_database_url)

    spans = [record for record in await derived_records(store) if record.kind is RecordKind.SPAN]
    nginx_active = next(
        record
        for record in spans
        if record.payload["span_type"] == SPAN_ACTIVE
        and record.payload["attributes"]["unit"] == "nginx.service"
    )
    assert nginx_active.entity == "service:nginx.service"
    assert nginx_active.monotonic_ns == 1_480_000_000
    assert nginx_active.payload["end_monotonic_ns"] == 9_120_000_000
    assert nginx_active.payload["state"] == "inactive"
    assert nginx_active.payload["attributes"]["invocation_id"] == (
        "2f9c6a0d1b3e4f5a8c7d9e0f1a2b3c4d"
    )

    redis_spans = [
        record
        for record in spans
        if record.payload["span_type"] == SPAN_ACTIVE
        and record.payload["attributes"]["unit"] == "redis.service"
    ]
    # 两次激活的 INVOCATION_ID 不同：库里是两条独立的区间，第一条没有结束时刻
    assert len(redis_spans) == 2
    assert {record.payload["attributes"]["invocation_id"] for record in redis_spans} == {
        "11111111111111111111111111111111",
        "22222222222222222222222222222222",
    }
    assert sorted(record.payload["end_monotonic_ns"] is None for record in redis_spans) == [
        False,
        True,
    ]


async def test_incomplete_lifecycle_annotation_reaches_the_database(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store = await import_journal_fixture(engine)

    await run_analysis(test_database_url)

    messages = {
        record.payload["annotation_type"]: record.payload["message"]
        for record in await derived_records(store)
        if record.kind is RecordKind.ANNOTATION
    }
    assert ANNOTATION_SERVICE_FAILED in messages
    assert "生命周期不完整" in messages[ANNOTATION_LIFECYCLE_INCOMPLETE]


async def test_record_scope_narrowing_writes_only_the_selected_evidence(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store = await import_journal_fixture(engine)
    events = await store.list_records_in_boot(MACHINE_ID, BOOT_ID, kinds=[RecordKind.EVENT])
    nginx_started = next(
        record
        for record in events
        if "Started A high performance" in (record.payload.get("message") or "")
    )

    result = await run_analysis(test_database_url, record_ids=(nginx_started.record_id,))

    records = await derived_records(store)
    assert result.annotations_written == 1
    assert sorted(record.kind.value for record in records) == ["annotation", "span"]
    span = next(record for record in records if record.kind is RecordKind.SPAN)
    annotation = next(record for record in records if record.kind is RecordKind.ANNOTATION)
    # 只有 Started：运行区间从这条记录开始，结束时刻保持 null，结论的整条证据就是这条记录
    assert span.monotonic_ns == nginx_started.monotonic_ns
    assert span.payload["end_monotonic_ns"] is None
    assert annotation.payload["evidence_refs"] == [nginx_started.record_id]


# --- 真实 Journal：Unit 取自 UNIT= / USER_UNIT=，实体由分析补齐 -----------------


async def test_real_journal_per_unit_spans_are_written_with_their_own_entities(
    engine: AsyncEngine, test_database_url: str
) -> None:
    """真实 Journal 的导入只有 init.scope / user@1000.service 两个实体；分析要重建真正的 Unit。

    这条用例同时证明：分析要写入的 service 实体（这里是 NetworkManager-dispatcher.service 等）
    可以通过数据入口补进库，之后引用它们的区间才能通过 1.1 的写入校验。
    """
    store = await import_journal_fixture(engine, REAL_FIXTURE)
    bundle = archive.build_bundle(
        archive.parse_journal_archive(REAL_FIXTURE),
        source_sha256=archive.archive_digest(REAL_FIXTURE),
    )
    machine_id = bundle.collection.machine_id
    boot_id = bundle.collection.boot_id

    before = {entity.entity_id for entity in await store.list_entities(machine_id, boot_id)}
    assert "service:NetworkManager-dispatcher.service" not in before

    result = await run_analysis(test_database_url, machine_id=machine_id, boot_id=boot_id)

    records = await derived_records(store, machine_id=machine_id, boot_id=boot_id)
    spans = [record for record in records if record.kind is RecordKind.SPAN]
    assert result.annotations_written == sum(
        1 for record in records if record.kind is RecordKind.ANNOTATION
    )
    assert spans
    units = {record.payload["attributes"]["unit"] for record in spans}
    assert "NetworkManager-dispatcher.service" in units
    assert "init.scope" not in units
    assert "user@1000.service" not in units

    entities = {entity.entity_id for entity in await store.list_entities(machine_id, boot_id)}
    assert {record.entity for record in spans} <= entities
    assert await store.check_collection_integrity(bundle.collection.collection_id) == []


async def test_real_journal_restarts_are_stored_as_separate_spans(
    engine: AsyncEngine, test_database_url: str
) -> None:
    store = await import_journal_fixture(engine, REAL_FIXTURE)
    bundle = archive.build_bundle(
        archive.parse_journal_archive(REAL_FIXTURE),
        source_sha256=archive.archive_digest(REAL_FIXTURE),
    )
    machine_id = bundle.collection.machine_id
    boot_id = bundle.collection.boot_id

    await run_analysis(test_database_url, machine_id=machine_id, boot_id=boot_id)

    active = [
        record
        for record in await derived_records(store, machine_id=machine_id, boot_id=boot_id)
        if record.kind is RecordKind.SPAN
        and record.payload["span_type"] == SPAN_ACTIVE
        and record.payload["attributes"]["unit"] == "NetworkManager-dispatcher.service"
    ]
    assert len(active) == 4
    assert len({record.payload["attributes"]["invocation_id"] for record in active}) == 4


# --- 缺结构化 Unit 字段：报数量与原因，不记到 init.scope 头上 -------------------


async def test_old_style_journal_reports_unattributed_messages_not_fake_spans(
    engine: AsyncEngine, test_database_url: str
) -> None:
    """缺 UNIT= 的输入必须产出「多少条没归属、为什么」，而不是挂在 init.scope 上的区间。"""
    store, bundle = await import_old_style_journal(engine)
    machine_id = bundle.collection.machine_id
    boot_id = bundle.collection.boot_id

    result = await run_analysis(test_database_url, machine_id=machine_id, boot_id=boot_id)

    records = await derived_records(store, machine_id=machine_id, boot_id=boot_id)
    assert [record for record in records if record.kind is RecordKind.SPAN] == []
    annotations = [record for record in records if record.kind is RecordKind.ANNOTATION]
    assert [record.payload["annotation_type"] for record in annotations] == [
        ANNOTATION_UNATTRIBUTED
    ]
    assert result.annotations_written == 1
    assert "条生命周期消息缺少结构化 Unit 字段" in annotations[0].payload["message"]
    assert annotations[0].entity is None
    # 产出的记录仍要通过 1.1 的完整性复核
    assert await store.check_collection_integrity(bundle.collection.collection_id) == []
