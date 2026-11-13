"""诊断数据存储的集成测试，需要真实 PostgreSQL。

运行方式见 ``docs/implementation-baseline.md``：先起库并执行 ``alembic upgrade head``，再执行
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``，未配置时直接报错。

数据用仓库里的 ``docs/samples/bootrec-v1`` 样例，另外补一份只有墙钟时钟域的构造数据。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sample_bootrec import load_sample
from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    CollectionRef,
    GenericEntity,
    MachineEntity,
    Record,
    RecordKind,
    ResourceAvailability,
    compute_record_id,
)
from system_analyzer.storage import (
    INTERNAL_IMPORT_FAILURE_MESSAGE,
    Base,
    DiagnosticStore,
    ImportStatus,
    StorageStateError,
    StorageValidationError,
)
from system_analyzer.storage.validation import boot_entity, machine_entity

pytestmark = pytest.mark.integration

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
FULL_COLLECTION = "collection:full-0001"
FULL_RECORDS = 17
FULL_ENTITIES = 5  # 样例 entities.jsonl 有 7 行，其中 machine 与 boot 各占一张自己的表


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


def _by_record_id(records: list[Record]) -> dict[str, dict[str, Any]]:
    """按 record_id 索引比较，逐字段比较全部 11 个统一字段与 payload。"""
    return {record.record_id: record.model_dump(mode="json") for record in records}


# ---------------------------------------------------------------------------
# 写入后按机器、按启动、按采集取回等价数据
# ---------------------------------------------------------------------------


async def test_import_then_query_by_machine_boot_and_collection(store: DiagnosticStore) -> None:
    sample = load_sample("full.bootrec")

    outcome = await store.import_collection(
        sample.collection, sample.entities, sample.record_lines, source_sha256=sample.source_sha256
    )

    assert outcome.status is ImportStatus.IMPORTED
    assert outcome.records_written == FULL_RECORDS
    assert outcome.records_skipped == 0
    assert outcome.entities_written == FULL_ENTITIES

    assert await store.get_machine(MACHINE_ID) == machine_entity(sample.entities)
    assert await store.get_boot(MACHINE_ID, BOOT_ID) == boot_entity(sample.entities)

    by_machine = await store.list_collections(machine_id=MACHINE_ID)
    assert [state.collection.collection_id for state in by_machine] == [FULL_COLLECTION]
    assert by_machine[0].collection == sample.collection
    assert by_machine[0].import_status is ImportStatus.IMPORTED
    assert by_machine[0].source_sha256 == sample.source_sha256

    by_boot = await store.list_collections(machine_id=MACHINE_ID, boot_id=BOOT_ID)
    assert [state.collection.collection_id for state in by_boot] == [FULL_COLLECTION]

    in_collection = await store.list_records_in_collection(FULL_COLLECTION)
    assert _by_record_id(in_collection) == _by_record_id(sample.records)

    in_boot = await store.list_records_in_boot(MACHINE_ID, BOOT_ID)
    assert _by_record_id(in_boot) == _by_record_id(sample.records)

    assert await store.count_records(FULL_COLLECTION) == FULL_RECORDS
    assert await store.check_collection_integrity(FULL_COLLECTION) == []


async def test_importing_the_same_data_twice_keeps_record_count(store: DiagnosticStore) -> None:
    sample = load_sample("full.bootrec")
    await store.import_collection(
        sample.collection, sample.entities, sample.record_lines, source_sha256=sample.source_sha256
    )

    # 第一级幂等：整份文件的摘要相同，整批跳过，连记录都不读
    same_file = await store.import_collection(
        sample.collection, sample.entities, sample.record_lines, source_sha256=sample.source_sha256
    )
    assert same_file.skipped_reason is not None
    assert same_file.records_written == 0

    # 第二级幂等：没有摘要时按 (collection_id, record_id) 逐条跳过
    no_digest = await store.import_collection(
        sample.collection, sample.entities, sample.record_lines
    )
    assert no_digest.records_written == 0
    assert no_digest.records_skipped == FULL_RECORDS
    assert no_digest.entities_skipped == FULL_ENTITIES

    assert await store.count_records(FULL_COLLECTION) == FULL_RECORDS
    assert len(await store.list_entities(MACHINE_ID, BOOT_ID)) == FULL_ENTITIES
    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None and state.import_status is ImportStatus.IMPORTED


# ---------------------------------------------------------------------------
# 缺失指标读出为「不可用」，不是 0
# ---------------------------------------------------------------------------


async def test_missing_metrics_read_as_unavailable_not_zero(store: DiagnosticStore) -> None:
    sample = load_sample("resources-missing.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)

    streams = {
        stream.entity_id: stream for stream in await store.list_metric_streams(MACHINE_ID, BOOT_ID)
    }
    assert streams["metric:cpu.total.utilization"].available is True
    assert streams["metric:cpu.total.utilization"].unavailable_reason is None

    memory = streams["metric:memory.used"]
    assert memory.available is False
    assert memory.unavailable_reason == "/proc/meminfo 在采集窗口内读取失败，本流没有数据"

    disk = streams["metric:disk.read_bytes_per_second"]
    assert disk.available is False
    assert disk.unavailable_reason == "目标机器没有 iostat，磁盘 I/O 未采集"

    metrics = await store.list_records_in_collection(
        sample.collection.collection_id, kinds=[RecordKind.METRIC]
    )
    # 不可用的两条流没有任何采样点：既没有行，也没有 value=0 的占位记录
    assert {record.entity for record in metrics} == {"metric:cpu.total.utilization"}
    assert [record.payload["value"] for record in metrics] == [9.5, 21.75, 15.0]
    assert all(record.payload["value"] != 0 for record in metrics)


async def test_journal_only_import_records_that_resources_are_absent(
    store: DiagnosticStore,
) -> None:
    """整份文件没有资源数据这件事要能查出来，与「某条指标流不可用」区分开。"""
    sample = load_sample("journal-only.bootrec")
    await store.import_collection(
        sample.collection, sample.entities, sample.record_lines, resources=sample.resources
    )

    state = await store.get_collection(sample.collection.collection_id)
    assert state is not None
    assert state.resources_available is False
    assert state.resources_reason == (
        "journal_import：压缩包只含 Journal，没有采集 CPU、内存、磁盘 I/O"
    )
    assert await store.list_metric_streams(MACHINE_ID, BOOT_ID) == []
    assert await store.check_collection_integrity(sample.collection.collection_id) == []


async def test_claiming_no_resources_while_writing_metrics_is_rejected(
    store: DiagnosticStore,
) -> None:
    sample = load_sample("full.bootrec")
    with pytest.raises(StorageValidationError) as error:
        await store.import_collection(
            sample.collection,
            sample.entities,
            sample.record_lines,
            resources=ResourceAvailability(available=False, reason="声明没有资源数据"),
        )
    assert any("metric 记录" in problem for problem in error.value.problems)

    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None and state.import_status is ImportStatus.FAILED
    assert await store.count_records(FULL_COLLECTION) == 0


# ---------------------------------------------------------------------------
# 时间范围与排序键
# ---------------------------------------------------------------------------


async def test_time_range_and_ordering_follow_time_base(store: DiagnosticStore) -> None:
    sample = load_sample("full.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)

    start_ns, end_ns = 1_000_000_000, 3_000_000_000
    records = await store.list_records_in_boot(
        MACHINE_ID, BOOT_ID, start_ns=start_ns, end_ns=end_ns
    )
    expected = [
        record
        for record in sample.records
        if record.monotonic_ns is not None and start_ns <= record.monotonic_ns <= end_ns
    ]
    assert _by_record_id(records) == _by_record_id(expected)
    assert 0 < len(records) < FULL_RECORDS

    # 排序键就是 time-base 第 7.1 节的 (monotonic_ns, collection_id, record_id)
    keys = [(record.monotonic_ns, record.collection_id, record.record_id) for record in records]
    assert keys == sorted(keys)


async def test_wall_realtime_records_are_queried_in_their_own_domain(
    store: DiagnosticStore,
) -> None:
    """只有墙钟的记录不与 boot 内的记录混排，单独按 realtime_ns 排序。"""
    collection = CollectionRef(
        collection_id="collection:wall-0001",
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        mode=CollectionMode.JOURNAL_IMPORT,
    )
    entities = [
        MachineEntity(entity_type="machine", machine_id=MACHINE_ID, hostname="uos-PC"),
        BootEntity(entity_type="boot", boot_id=BOOT_ID, machine_id=MACHINE_ID),
        GenericEntity(
            entity_type="service",
            entity_id="service:foreign.service",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            name="foreign.service",
        ),
    ]
    records = [
        _record(
            collection=collection,
            monotonic_ns=None,
            realtime_ns=2_000_000_000,
            clock_domain=ClockDomain.WALL_REALTIME,
        ),
        _record(
            collection=collection,
            monotonic_ns=None,
            realtime_ns=1_000_000_000,
            clock_domain=ClockDomain.WALL_REALTIME,
        ),
        _record(collection=collection, monotonic_ns=5_000_000_000),
    ]
    await store.import_collection(collection, entities, records)

    wall = await store.list_records_in_boot(
        MACHINE_ID, BOOT_ID, clock_domain=ClockDomain.WALL_REALTIME
    )
    assert [record.realtime_ns for record in wall] == [1_000_000_000, 2_000_000_000]
    assert all(record.clock_domain is ClockDomain.WALL_REALTIME for record in wall)

    monotonic = await store.list_records_in_boot(MACHINE_ID, BOOT_ID)
    assert [record.monotonic_ns for record in monotonic] == [5_000_000_000]


# ---------------------------------------------------------------------------
# 失败与回滚
# ---------------------------------------------------------------------------


async def test_invalid_record_marks_collection_failed_and_writes_nothing(
    store: DiagnosticStore,
) -> None:
    sample = load_sample("full.bootrec")
    broken = {
        **sample.record_lines[0],
        "monotonic_ns": None,
        "realtime_ns": None,
        "clock_domain": ClockDomain.WALL_REALTIME.value,
    }
    broken["record_id"] = compute_record_id(
        {key: value for key, value in broken.items() if key != "record_id"}
    )

    with pytest.raises(StorageValidationError) as error:
        await store.import_collection(
            sample.collection, sample.entities, [*sample.record_lines, broken]
        )
    assert any("都为空" in problem for problem in error.value.problems)

    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None
    assert state.import_status is ImportStatus.FAILED
    assert state.import_error is not None and "都为空" in state.import_error
    assert await store.count_records(FULL_COLLECTION) == 0


async def test_failed_write_rolls_back_entities(
    store: DiagnosticStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记录写入中途出错时，同一事务里已经写入的实体也要回滚。"""
    sample = load_sample("full.bootrec")

    async def explode(self: DiagnosticStore, conn: Any, records: Any) -> tuple[int, int]:
        raise RuntimeError("模拟写记录时数据库出错")

    monkeypatch.setattr(DiagnosticStore, "_insert_records", explode)

    with pytest.raises(RuntimeError):
        await store.import_collection(sample.collection, sample.entities, sample.record_lines)

    assert await store.list_entities(MACHINE_ID, BOOT_ID) == []
    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None
    assert state.import_status is ImportStatus.FAILED
    assert state.import_error == INTERNAL_IMPORT_FAILURE_MESSAGE
    assert "模拟写记录时数据库出错" not in state.import_error


# ---------------------------------------------------------------------------
# 完整性检查与追加
# ---------------------------------------------------------------------------


async def test_integrity_check_detects_a_changed_record(
    store: DiagnosticStore, engine: AsyncEngine
) -> None:
    """写入之后被改动的记录，读出侧要能报出来（data-model 第 8 节的摘要核对）。"""
    sample = load_sample("full.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)
    assert await store.check_collection_integrity(FULL_COLLECTION) == []

    target = sample.record_lines[0]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE records SET payload = payload || '{\"changed\": true}'::jsonb"
                " WHERE record_id = :record_id"
            ),
            {"record_id": target["record_id"]},
        )

    problems = await store.check_collection_integrity(FULL_COLLECTION)
    assert len(problems) == 1
    assert target["record_id"] in problems[0]
    assert "写入之后被改过" in problems[0]


async def test_unknown_fields_are_kept_so_the_digest_still_checks_out(
    store: DiagnosticStore,
) -> None:
    """本主版本不认识的字段要留着：丢掉它，读出侧就再也核对不了摘要。"""
    sample = load_sample("full.bootrec")
    lines = list(sample.record_lines)
    line = {**lines[0], "future_field": {"added": "in a later minor version"}}
    line["record_id"] = compute_record_id(
        {key: value for key, value in line.items() if key != "record_id"}
    )
    lines[0] = line

    await store.import_collection(sample.collection, sample.entities, lines)

    assert await store.check_collection_integrity(FULL_COLLECTION) == []
    stored = await store.list_records_in_collection(FULL_COLLECTION)
    assert line["record_id"] in {record.record_id for record in stored}


async def test_integrity_check_reports_record_pointing_to_a_missing_entity(
    store: DiagnosticStore, engine: AsyncEngine
) -> None:
    sample = load_sample("full.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)
    assert await store.check_collection_integrity(FULL_COLLECTION) == []

    target = next(line for line in sample.record_lines if line["kind"] == "event")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE records SET entity = 'service:ghost.service' WHERE record_id = :record_id"
            ),
            {"record_id": target["record_id"]},
        )

    problems = await store.check_collection_integrity(FULL_COLLECTION)
    assert any(
        target["record_id"] in problem and "service:ghost.service" in problem
        for problem in problems
    )
    # 改 entity 也改了内容，摘要核对同时报警：两条检查各报各的
    assert any("写入之后被改过" in problem for problem in problems)


async def test_append_records_adds_supplementary_data(store: DiagnosticStore) -> None:
    sample = load_sample("full.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)

    extra = _record(
        collection=sample.collection,
        kind=RecordKind.ANNOTATION,
        entity=None,
        source_plugin="analyzer.oom",
        stream_id="oom.detected",
        monotonic_ns=1_500_000_000,
        payload={
            "annotation_type": "oom.detected",
            "analyzer": "analyzer.oom",
            "evidence_refs": [sample.records[0].record_id],
        },
    )

    first = await store.append_records(FULL_COLLECTION, [extra])
    assert first.records_written == 1
    assert await store.count_records(FULL_COLLECTION) == FULL_RECORDS + 1

    again = await store.append_records(FULL_COLLECTION, [extra])
    assert again.records_written == 0
    assert again.records_skipped == 1
    assert await store.count_records(FULL_COLLECTION) == FULL_RECORDS + 1

    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None and state.import_status is ImportStatus.IMPORTED


async def test_ensure_entities_adds_missing_service_entities_idempotently(
    store: DiagnosticStore,
) -> None:
    """分析插件补实体：第一次写入，第二次与已有定义都不变。"""
    sample = load_sample("full.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)
    entity = GenericEntity(
        entity_type="service",
        entity_id="service:redis.service",
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        name="redis.service",
        attributes={"unit": "redis.service"},
    )

    assert await store.ensure_entities(FULL_COLLECTION, [entity]) == 1
    assert entity in await store.list_entities(MACHINE_ID, BOOT_ID)
    assert await store.ensure_entities(FULL_COLLECTION, [entity]) == 0
    assert len(await store.list_entities(MACHINE_ID, BOOT_ID)) == FULL_ENTITIES + 1


async def test_ensure_entities_rejects_entities_from_another_boot(
    store: DiagnosticStore,
) -> None:
    sample = load_sample("full.bootrec")
    await store.import_collection(sample.collection, sample.entities, sample.record_lines)
    foreign = GenericEntity(
        entity_type="service",
        entity_id="service:redis.service",
        machine_id=MACHINE_ID,
        boot_id="another-boot",
        name="redis.service",
    )

    with pytest.raises(StorageValidationError) as error:
        await store.ensure_entities(FULL_COLLECTION, [foreign])
    assert any("boot_id" in problem for problem in error.value.problems)
    assert all(
        not isinstance(entity, GenericEntity) or entity.entity_id != "service:redis.service"
        for entity in await store.list_entities(MACHINE_ID, BOOT_ID)
    )


async def test_records_without_optional_fields_import_fine(store: DiagnosticStore) -> None:
    """可选字段省略的原始记录必须能导入（0.2 校验器就是按原始字典算摘要的）。"""
    sample = load_sample("full.bootrec")
    lines = []
    for line in sample.record_lines:
        trimmed = {key: value for key, value in line.items() if key != "realtime_ns"}
        if trimmed["kind"] not in ("metric", "span"):
            trimmed.pop("entity")
        trimmed["record_id"] = compute_record_id(
            {key: value for key, value in trimmed.items() if key != "record_id"}
        )
        lines.append(trimmed)

    outcome = await store.import_collection(sample.collection, sample.entities, lines)

    assert outcome.records_written == FULL_RECORDS
    assert await store.count_records(FULL_COLLECTION) == FULL_RECORDS
    assert await store.check_collection_integrity(FULL_COLLECTION) == []


async def test_invalid_record_dict_marks_collection_failed(store: DiagnosticStore) -> None:
    """字典字段不合法时，走与其他不合法数据相同的路：标 failed、抛存储层异常。"""
    sample = load_sample("full.bootrec")
    lines = list(sample.record_lines)
    broken = {**lines[0], "kind": "not-a-kind"}
    broken["record_id"] = compute_record_id(
        {key: value for key, value in broken.items() if key != "record_id"}
    )
    lines[0] = broken

    with pytest.raises(StorageValidationError) as error:
        await store.import_collection(sample.collection, sample.entities, lines)

    assert any("第 1 条记录不合法" in problem for problem in error.value.problems)
    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None
    assert state.import_status is ImportStatus.FAILED
    assert state.import_error is not None and "第 1 条记录不合法" in state.import_error
    assert await store.count_records(FULL_COLLECTION) == 0


async def test_tampered_record_content_is_rejected_at_write_time(
    store: DiagnosticStore,
) -> None:
    """内容改了但 ``record_id`` 没重算：整份采集不写入，状态标成 failed。"""
    sample = load_sample("full.bootrec")
    lines = list(sample.record_lines)
    lines[0] = {**lines[0], "payload": {"event_type": "systemd.unit.failed"}}

    with pytest.raises(StorageValidationError) as error:
        await store.import_collection(sample.collection, sample.entities, lines)

    assert any("与内容算出的" in problem for problem in error.value.problems)
    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None and state.import_status is ImportStatus.FAILED
    assert await store.count_records(FULL_COLLECTION) == 0


async def test_large_collection_is_written_in_batches(store: DiagnosticStore) -> None:
    """记录数超过单条 ``INSERT`` 的参数上限时仍然能导入（分批写入是同一事务）。"""
    count = 6000  # 12 个参数一行，单条语句的上限约 5461 行
    collection = CollectionRef(
        collection_id="collection:large-0001",
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        mode=CollectionMode.SSH_COLLECTION,
    )
    entities = [
        MachineEntity(entity_type="machine", machine_id=MACHINE_ID, hostname="uos-PC"),
        BootEntity(entity_type="boot", boot_id=BOOT_ID, machine_id=MACHINE_ID),
        GenericEntity(
            entity_type="service",
            entity_id="service:batch.service",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            name="batch.service",
        ),
    ]
    lines = [
        _raw_record(
            collection=collection,
            monotonic_ns=1_000_000_000 + index,
            entity="service:batch.service",
        )
        for index in range(count)
    ]

    outcome = await store.import_collection(collection, entities, lines)

    assert outcome.records_written == count
    assert await store.count_records(collection.collection_id) == count
    assert await store.check_collection_integrity(collection.collection_id) == []


async def test_appending_metrics_to_a_collection_without_resources_is_rejected(
    store: DiagnosticStore,
) -> None:
    sample = load_sample("journal-only.bootrec")
    await store.import_collection(
        sample.collection,
        sample.entities,
        sample.record_lines,
        resources=sample.resources,
    )

    metric = _raw_record(
        collection=sample.collection,
        kind=RecordKind.METRIC,
        entity=None,
        payload={"name": "cpu.total.utilization", "value": 0.0, "unit": "percent"},
    )
    with pytest.raises(StorageValidationError) as error:
        await store.append_records(sample.collection.collection_id, [metric])

    assert any("没有资源数据" in problem for problem in error.value.problems)
    assert await store.count_records(sample.collection.collection_id) == 2


async def test_cannot_append_to_a_collection_that_did_not_import(
    store: DiagnosticStore,
) -> None:
    sample = load_sample("full.bootrec")
    broken = {
        **sample.record_lines[0],
        "monotonic_ns": None,
        "realtime_ns": None,
        "clock_domain": ClockDomain.WALL_REALTIME.value,
    }
    broken["record_id"] = compute_record_id(
        {key: value for key, value in broken.items() if key != "record_id"}
    )
    with pytest.raises(StorageValidationError):
        await store.import_collection(sample.collection, sample.entities, [broken])

    state = await store.get_collection(FULL_COLLECTION)
    assert state is not None and state.import_status is ImportStatus.FAILED
    with pytest.raises(StorageStateError):
        await store.append_records(FULL_COLLECTION, [sample.record_lines[0]])


# ---------------------------------------------------------------------------
# 迁移与模型一致
# ---------------------------------------------------------------------------


def _skip_other_tables(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """本文件只对照诊断数据的五张表。

    ``jobs``（0001）用原生 SQL 访问、没有 ORM 模型；``ssh_targets``（0003，任务 3.1）的模型在
    ``remote/models.py``，与迁移的对照由 ``test_ssh_targets_store.py`` 负责。
    """
    return name not in (
        "jobs",
        "jobs_claimable_idx",
        "ssh_targets",
        "ssh_targets_port_check",
        "ssh_targets_host_check",
        "ssh_targets_username_check",
        "ssh_targets_password_env_var_check",
    )


async def test_migration_matches_models(engine: AsyncEngine) -> None:
    def diff(sync_connection: Any) -> list[Any]:
        context = MigrationContext.configure(
            sync_connection, opts={"include_object": _skip_other_tables}
        )
        return compare_metadata(context, Base.metadata)

    async with engine.connect() as connection:
        assert await connection.run_sync(diff) == []


# ---------------------------------------------------------------------------
# 构造数据
# ---------------------------------------------------------------------------


def _raw_record(
    *,
    collection: CollectionRef,
    monotonic_ns: int | None = 1_000_000_000,
    realtime_ns: int | None = 1_794_009_601_000_000_000,
    clock_domain: ClockDomain = ClockDomain.BOOT_MONOTONIC,
    kind: RecordKind = RecordKind.EVENT,
    entity: str | None = "service:foreign.service",
    source_plugin: str = "journal-importer",
    stream_id: str = "systemd.units",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按规范组装一条原始记录字典：``record_id`` 由这个字典自己算出。"""
    values: dict[str, Any] = {
        "kind": kind.value,
        "machine_id": collection.machine_id,
        "boot_id": collection.boot_id,
        "collection_id": collection.collection_id,
        "source_plugin": source_plugin,
        "stream_id": stream_id,
        "monotonic_ns": monotonic_ns,
        "realtime_ns": realtime_ns,
        "clock_domain": clock_domain.value,
        "entity": entity,
        "payload": payload or {"event_type": "systemd.unit.started"},
    }
    values["record_id"] = compute_record_id(values)
    return values


def _record(**overrides: Any) -> Record:
    """同 ``_raw_record``，但返回模型，用于直接构造记录的场景。"""
    return Record.model_validate(_raw_record(**overrides))
