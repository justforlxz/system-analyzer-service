"""诊断数据的写入与查询。

写入路径有两处，入口都在 ``DiagnosticStore``：

- ``import_collection``：一次写入一整份采集的实体与记录。先登记采集（``pending``），再校验数据，
  最后在一个事务里写实体、写记录、把状态推进到 ``imported``；任何一步失败都整批回滚并把状态
  标成 ``failed``。
- ``append_records``：向已有采集追加记录。用于同一份采集的补充数据（采集重试补上最后几秒）与
  分析插件产出的 Annotation（data-model 第 8 节）。

幂等靠两级键，与 ``docs/data-model.md`` 第 8 节一致：整份文件的摘要 ``source_sha256``（第一级，
同一个文件再导入直接跳过）与记录唯一键 ``(collection_id, record_id)``（第二级，重复记录
``ON CONFLICT DO NOTHING``）。同一份数据导入两次，记录数不变。

查询路径按机器、按启动、按采集、按时间范围。按 ``monotonic_ns`` 的排序一律限定在
``(machine_id, boot_id)`` 之内（``docs/time-base.md`` 第 3.2、7.1、10 节），排序键是
``(monotonic_ns, collection_id, record_id)``。只有墙钟的记录不与 boot 内的记录混排，
用 ``clock_domain=wall_realtime`` 单独查询，按 ``realtime_ns`` 排序。

数据缺失用「不可用」表达：``metric_stream`` 实体的 ``available = false`` 与 ``unavailable_reason``
原样读出，缺失的采样点根本没有行，查询不会返回 0，也不会补默认值。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Insert, Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from system_analyzer.core.db import create_session_factory
from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    CollectionRef,
    EntityType,
    GenericEntity,
    MachineEntity,
    MetricStreamEntity,
    Record,
    RecordKind,
    ResourceAvailability,
)
from system_analyzer.storage.models import (
    BootRow,
    CollectionRow,
    EntityRow,
    ImportStatus,
    MachineRow,
    RecordRow,
    can_transition,
)
from system_analyzer.storage.validation import (
    boot_entity,
    bundle_problems,
    identity_problems,
    incoming_records,
    machine_entity,
    record_problems,
    resources_problems,
    stored_content_problems,
)

INSERT_BATCH_SIZE = 500
"""一条 ``INSERT`` 语句最多写多少行。

PostgreSQL 单条语句的绑定参数上限是 65535；``records`` 每行 12 个参数、``entities`` 每行 9 个，
一批 500 行分别只要 6000 与 4500 个参数，离上限足够远。写入仍然是同一个事务，分批只影响语句
条数，不影响原子性。
"""


class StorageError(Exception):
    """存储层的基类异常。"""


class StorageValidationError(StorageError):
    """数据不合法，没有写入。``problems`` 是一条条具体原因。"""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


class StorageStateError(StorageError):
    """导入状态不允许这次迁移。"""


@dataclass(frozen=True, slots=True)
class CollectionState:
    """一次采集的完整状态：0.2 的 Collection 字段加上导入状态。"""

    collection: CollectionRef
    import_status: ImportStatus
    import_error: str | None
    source_sha256: str | None
    resources_available: bool | None
    resources_reason: str | None


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """一次写入的结果。``*_skipped`` 是按幂等键跳过的行数。"""

    collection_id: str
    status: ImportStatus
    records_written: int = 0
    records_skipped: int = 0
    entities_written: int = 0
    entities_skipped: int = 0
    skipped_reason: str | None = None


class DiagnosticStore:
    """诊断数据的写入与查询，基于 PostgreSQL。"""

    def __init__(self, engine: AsyncEngine) -> None:
        # 读写都走 ORM 会话：``select(RecordRow)`` 这类语句只有在 ORM 会话里执行才会返回
        # 模型对象；直接拿到 Connection 上执行会退化成按列返回元组。
        self._sessions = create_session_factory(engine)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def register_collection(
        self,
        collection: CollectionRef,
        machine: MachineEntity,
        boot: BootEntity,
        *,
        source_sha256: str | None = None,
        resources: ResourceAvailability | None = None,
    ) -> CollectionState:
        """登记一次采集，状态为 ``pending``；重复登记不改变已有状态。

        导入方可以先登记、稍后在 Worker 里导入，这样「已上传、尚未导入」也有一条可查询的记录。
        ``source_sha256`` 是整份文件的摘要，用于第一级幂等（data-model 第 8 节）；
        ``resources`` 是这份数据整体的资源数据可用性（``manifest.resources``）。

        重复登记不改写已有的行，``source_sha256`` 也保留第一次的值：它说明的是「这次采集的数据
        最初来自哪份文件」。同一个 ``collection_id`` 换了另一份文件时，导入方用
        ``get_collection().source_sha256`` 与新文件的摘要比对，就能发现 data-model 第 8 节
        要求的「collection_id 被重复使用」并在任务结果里报出来。
        """
        problems = _registration_problems(collection, machine, boot)
        if problems:
            raise StorageValidationError(problems)
        async with self._sessions() as session, session.begin():
            await session.execute(_machine_statement(machine).on_conflict_do_nothing())
            await session.execute(_boot_statement(boot).on_conflict_do_nothing())
            await session.execute(
                pg_insert(CollectionRow)
                .values(
                    collection_id=collection.collection_id,
                    machine_id=collection.machine_id,
                    boot_id=collection.boot_id,
                    mode=collection.mode.value,
                    created_realtime_ns=collection.created_realtime_ns,
                    import_status=ImportStatus.PENDING.value,
                    source_sha256=source_sha256,
                    resources_available=None if resources is None else resources.available,
                    resources_reason=None if resources is None else resources.reason,
                )
                .on_conflict_do_nothing(index_elements=["collection_id"])
            )
        state = await self.get_collection(collection.collection_id)
        if state is None:  # pragma: no cover - 写入刚成功，读不到只可能是数据库异常
            raise StorageStateError(f"采集 {collection.collection_id} 登记后读不到")
        return state

    async def import_collection(
        self,
        collection: CollectionRef,
        entities: Sequence[BaseModel],
        records: Sequence[Record | Mapping[str, Any]],
        *,
        source_sha256: str | None = None,
        resources: ResourceAvailability | None = None,
    ) -> ImportOutcome:
        """写入一整份采集的实体与记录。

        ``records`` 里的元素可以是原始记录字典（推荐，导入方解析文件时手上的就是它）或
        ``Record`` 模型。传字典时 ``record_id`` 会按原始内容逐字段核对，含本主版本不认识的
        字段；传模型时模型已经丢了未知字段，这类记录会被判为摘要不一致。

        实体、记录与最终状态在同一个事务里提交，失败整批回滚。同一份数据重复导入不会新增行：
        实体按 ``(machine_id, boot_id, entity_id)`` 去重，记录按 ``(collection_id, record_id)``
        去重。

        登记采集是单独一次提交（见 ``register_collection``），不是笔误：导入失败后要能查到
        「这次采集导入失败、原因是什么」，如果登记也随失败回滚，失败本身就没有记录了。
        """
        machine = machine_entity(entities)
        boot = boot_entity(entities)
        if machine is None or boot is None:
            raise StorageValidationError(
                [
                    f"缺少 {name} 行"
                    for name, found in (("machine", machine), ("boot", boot))
                    if found is None
                ]
            )

        if source_sha256 is not None:
            already = await self.find_collection_by_digest(collection.machine_id, source_sha256)
            if (
                already is not None
                and already.collection.collection_id == collection.collection_id
                and already.import_status is ImportStatus.IMPORTED
            ):
                return ImportOutcome(
                    collection_id=collection.collection_id,
                    status=ImportStatus.IMPORTED,
                    skipped_reason="同一份文件已经导入过（source_sha256 相同）",
                )

        await self.register_collection(
            collection, machine, boot, source_sha256=source_sha256, resources=resources
        )
        incoming, parse_problems = incoming_records(records)
        if parse_problems:
            await self._mark_failed_quietly(collection.collection_id, "；".join(parse_problems))
            raise StorageValidationError(parse_problems)
        problems = bundle_problems(collection, entities, incoming) + resources_problems(
            None if resources is None else resources.available,
            None if resources is None else resources.reason,
            [item.record for item in incoming],
        )
        if problems:
            await self._mark_failed_quietly(collection.collection_id, "；".join(problems))
            raise StorageValidationError(problems)

        try:
            async with self._sessions() as session, session.begin():
                await self._transition(session, collection.collection_id, ImportStatus.IMPORTING)
                entities_written, entities_skipped = await self._insert_entities(session, entities)
                records_written, records_skipped = await self._insert_records(
                    session, [item.record for item in incoming]
                )
                await self._transition(session, collection.collection_id, ImportStatus.IMPORTED)
        except Exception as error:
            await self._mark_failed_quietly(
                collection.collection_id, f"{type(error).__name__}: {error}"
            )
            raise
        return ImportOutcome(
            collection_id=collection.collection_id,
            status=ImportStatus.IMPORTED,
            records_written=records_written,
            records_skipped=records_skipped,
            entities_written=entities_written,
            entities_skipped=entities_skipped,
        )

    async def append_records(
        self, collection_id: str, records: Sequence[Record | Mapping[str, Any]]
    ) -> ImportOutcome:
        """向已有采集追加记录（补充数据、分析结论）。

        只允许向 ``imported`` 的采集追加：向 ``failed`` 的采集追加会留下「导入失败但已有数据」
        这种自相矛盾的状态，向 ``pending`` / ``importing`` 的采集追加则与正在进行的导入交错。
        追加不改变采集的导入状态，重复记录按幂等键跳过。
        """
        state = await self.get_collection(collection_id)
        if state is None:
            raise StorageStateError(f"采集 {collection_id} 不在库里，不能追加记录")
        if state.import_status is not ImportStatus.IMPORTED:
            raise StorageStateError(
                f"采集 {collection_id} 的导入状态是 {state.import_status.value}，"
                "只有导入完成的采集才能追加记录"
            )
        entities = await self.list_entities(state.collection.machine_id, state.collection.boot_id)
        incoming, parse_problems = incoming_records(records)
        if parse_problems:
            raise StorageValidationError(parse_problems)
        problems = (
            record_problems(state.collection, entities, [item.record for item in incoming])
            + [problem for item in incoming for problem in identity_problems(item)]
            + resources_problems(
                state.resources_available,
                state.resources_reason,
                [item.record for item in incoming],
            )
        )
        if problems:
            raise StorageValidationError(problems)
        async with self._sessions() as session, session.begin():
            written, skipped = await self._insert_records(
                session, [item.record for item in incoming]
            )
        return ImportOutcome(
            collection_id=collection_id,
            status=state.import_status,
            records_written=written,
            records_skipped=skipped,
        )

    async def ensure_entities(self, collection_id: str, entities: Sequence[BaseModel]) -> int:
        """向已有采集所属的启动补齐实体，返回真正写入的行数。

        分析插件只产出 ``GenericEntity``（service 等），而真实 Journal 导入只为
        ``_SYSTEMD_UNIT`` 建过实体；被分析的 Unit 往往没有实体行，不补就引用不到。

        幂等：实体唯一键是 ``(machine_id, boot_id, entity_id)``，重复写入走
        ``ON CONFLICT DO NOTHING``，已有定义不被改写。只接受与这次采集同一台机器、同一次启动
        的实体；跨启动或跨机器的实体直接拒绝，避免一次分析把数据写进别的启动。
        """
        state = await self.get_collection(collection_id)
        if state is None:
            raise StorageStateError(f"采集 {collection_id} 不在库里，不能补实体")
        collection = state.collection
        problems: list[str] = []
        seen: set[str] = set()
        for entity in entities:
            label = getattr(entity, "entity_id", type(entity).__name__)
            if isinstance(entity, (MachineEntity, BootEntity)):
                problems.append(f"{label} 是机器或启动行，ensure_entities 只接受普通实体")
                continue
            if getattr(entity, "machine_id", None) != collection.machine_id:
                problems.append(f"实体 {label} 的 machine_id 与采集不一致")
            boot_id = getattr(entity, "boot_id", None)
            if boot_id is not None and boot_id != collection.boot_id:
                problems.append(f"实体 {label} 的 boot_id 与采集不一致")
            if label in seen:
                problems.append(f"实体 {label} 在同一批数据里出现两次")
            seen.add(label)
        if problems:
            raise StorageValidationError(problems)
        if not entities:
            return 0
        async with self._sessions() as session, session.begin():
            written, _skipped = await self._insert_entities(session, entities)
        return written

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get_machine(self, machine_id: str) -> MachineEntity | None:
        async with self._sessions() as session:
            row = (
                await session.execute(select(MachineRow).where(MachineRow.machine_id == machine_id))
            ).scalar_one_or_none()
        return None if row is None else _machine_entity(row)

    async def list_boots(self, machine_id: str) -> list[BootEntity]:
        """一台机器的全部启动，按 ``boot_index`` 升序，没有 ``boot_index`` 的排在最后。"""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(BootRow)
                    .where(BootRow.machine_id == machine_id)
                    .order_by(BootRow.boot_index.asc().nulls_last(), BootRow.boot_id)
                )
            ).scalars()
            return [_boot_entity(row) for row in rows]

    async def get_boot(self, machine_id: str, boot_id: str) -> BootEntity | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(BootRow).where(
                        BootRow.machine_id == machine_id, BootRow.boot_id == boot_id
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else _boot_entity(row)

    async def get_collection(self, collection_id: str) -> CollectionState | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(CollectionRow).where(CollectionRow.collection_id == collection_id)
                )
            ).scalar_one_or_none()
        return None if row is None else _collection_state(row)

    async def list_collections(
        self,
        *,
        machine_id: str | None = None,
        boot_id: str | None = None,
        status: ImportStatus | None = None,
    ) -> list[CollectionState]:
        """按机器、按启动、按导入状态筛选采集。不带条件时返回全部。"""
        statement = select(CollectionRow)
        if machine_id is not None:
            statement = statement.where(CollectionRow.machine_id == machine_id)
        if boot_id is not None:
            statement = statement.where(CollectionRow.boot_id == boot_id)
        if status is not None:
            statement = statement.where(CollectionRow.import_status == status.value)
        statement = statement.order_by(
            CollectionRow.created_realtime_ns.asc().nulls_last(), CollectionRow.collection_id
        )
        async with self._sessions() as session:
            rows = (await session.execute(statement)).scalars()
            return [_collection_state(row) for row in rows]

    async def find_collection_by_digest(
        self, machine_id: str, source_sha256: str
    ) -> CollectionState | None:
        """按整份文件的摘要找采集，用于第一级幂等：同一个文件已经导入过就直接跳过。"""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(CollectionRow)
                    .where(
                        CollectionRow.machine_id == machine_id,
                        CollectionRow.source_sha256 == source_sha256,
                    )
                    .order_by(CollectionRow.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return None if row is None else _collection_state(row)

    async def list_entities(self, machine_id: str, boot_id: str) -> list[BaseModel]:
        """一次启动用到的实体，含 ``boot_id`` 为空（不绑定某次启动）的实体。"""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(EntityRow)
                    .where(
                        EntityRow.machine_id == machine_id,
                        or_(EntityRow.boot_id == boot_id, EntityRow.boot_id.is_(None)),
                    )
                    .order_by(EntityRow.entity_id)
                )
            ).scalars()
            return [_entity_model(row) for row in rows]

    async def list_metric_streams(self, machine_id: str, boot_id: str) -> list[MetricStreamEntity]:
        """一次启动的全部指标流。

        ``available = false`` 的流表示这段数据没有采到，``unavailable_reason`` 写明原因；
        读取方据此显示「不可用」，不能画成 0。
        """
        streams = [
            entity
            for entity in await self.list_entities(machine_id, boot_id)
            if isinstance(entity, MetricStreamEntity)
        ]
        return streams

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
        """一次启动内的记录，按 ``(时间, collection_id, record_id)`` 排序。

        默认查询 ``boot_monotonic`` 时钟域并以其 ``monotonic_ns`` 排序与筛选；传入
        ``clock_domain=wall_realtime`` 时改为按 ``realtime_ns`` 排序与筛选。两种时钟域不能混排，
        因为它们的零点不同（time-base 第 3.2、10 节）。
        """
        column = _time_column(clock_domain)
        statement = select(RecordRow).where(
            RecordRow.machine_id == machine_id,
            RecordRow.boot_id == boot_id,
            RecordRow.clock_domain == clock_domain.value,
        )
        return await self._records(
            statement, column, start_ns=start_ns, end_ns=end_ns, kinds=kinds, limit=limit
        )

    async def list_records_in_collection(
        self,
        collection_id: str,
        *,
        clock_domain: ClockDomain = ClockDomain.BOOT_MONOTONIC,
        start_ns: int | None = None,
        end_ns: int | None = None,
        kinds: Sequence[RecordKind] | None = None,
        limit: int | None = None,
    ) -> list[Record]:
        """一次采集内的记录，排序与筛选规则同 ``list_records_in_boot``。"""
        column = _time_column(clock_domain)
        statement = select(RecordRow).where(
            RecordRow.collection_id == collection_id,
            RecordRow.clock_domain == clock_domain.value,
        )
        return await self._records(
            statement, column, start_ns=start_ns, end_ns=end_ns, kinds=kinds, limit=limit
        )

    async def count_records(self, collection_id: str) -> int:
        async with self._sessions() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(RecordRow)
                .where(RecordRow.collection_id == collection_id)
            )
        return int(total or 0)

    async def check_collection_integrity(self, collection_id: str) -> list[str]:
        """复核库里这次采集的数据，返回发现的问题；空列表表示一致。

        复核的内容是 0.2 与 0.3 的硬性规则：记录的作用域字段与采集一致、``record_id`` 的格式
        与内容摘要、记录指向的实体存在、不可用的指标流没有数据、与 ``metric_stream`` 的指标名
        与单位一致。只报告，不修改、不补默认值。

        摘要核对的界限：库里存的是 0.2 的 11 个字段加本主版本不认识的字段，没有存「原始文件里哪
        些可空字段真的写了」，所以按「``entity`` / ``monotonic_ns`` / ``realtime_ns`` 这几个
        取值为 null 的键可能被省略」的每种组合各算一次，命中任一即通过（见
        ``validation.stored_content_problems``）。这套规则能发现写入之后被改动的记录；原始
        内容若还省略了别的字段，会被报成不一致，需要回到原始文件用 ``validate_bootrec`` 核对。
        """
        state = await self.get_collection(collection_id)
        if state is None:
            return [f"采集 {collection_id} 不在库里"]
        entities = await self.list_entities(state.collection.machine_id, state.collection.boot_id)
        async with self._sessions() as session:
            # 结果集只能读一遍，先取成列表：下面要按同一批行走两条核对
            rows = list(
                (
                    await session.execute(
                        select(RecordRow)
                        .where(RecordRow.collection_id == collection_id)
                        .order_by(RecordRow.record_id)
                    )
                ).scalars()
            )
            records = [_record(row) for row in rows]
            stored = [(row.record_id, _stored_content(row)) for row in rows]
        return (
            record_problems(state.collection, entities, records)
            + resources_problems(state.resources_available, state.resources_reason, records)
            + [
                problem
                for record_id, content in stored
                for problem in stored_content_problems(record_id, content)
            ]
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _records(
        self,
        statement: Select,
        column,
        *,
        start_ns: int | None,
        end_ns: int | None,
        kinds: Sequence[RecordKind] | None,
        limit: int | None,
    ) -> list[Record]:
        if start_ns is not None:
            statement = statement.where(column >= start_ns)
        if end_ns is not None:
            statement = statement.where(column <= end_ns)
        if kinds:
            statement = statement.where(RecordRow.kind.in_([kind.value for kind in kinds]))
        statement = statement.order_by(column, RecordRow.collection_id, RecordRow.record_id)
        if limit is not None:
            statement = statement.limit(limit)
        async with self._sessions() as session:
            rows = (await session.execute(statement)).scalars()
            return [_record(row) for row in rows]

    async def _insert_in_batches(
        self,
        session: AsyncSession,
        build: Callable[[Sequence[dict[str, Any]]], Insert],
        rows: Sequence[dict[str, Any]],
    ) -> int:
        """按 ``INSERT_BATCH_SIZE`` 分批执行同一条插入语句，返回真正写入的行数。"""
        written = 0
        for start in range(0, len(rows), INSERT_BATCH_SIZE):
            result = await session.execute(build(rows[start : start + INSERT_BATCH_SIZE]))
            written += int(result.rowcount)
        return written

    async def _insert_entities(
        self, session: AsyncSession, entities: Sequence[BaseModel]
    ) -> tuple[int, int]:
        """写入实体；同一个实体重复出现时保留第一次写入的定义。

        实体的唯一键是 ``(machine_id, boot_id, entity_id)``，不含 ``collection_id``：
        ``docs/data-model.md`` 第 6 节规定 ``entity_id`` 只在同一个 boot 内唯一，同一次启动的
        多次采集共用一套实体定义。两份采集对同一个实体的描述不一致时（例如一条指标流一次
        可用、一次不可用），0.2 没有规定哪一份为准，这里不改写已有行、也不合并，只保留先写入的
        那一份；这个冲突目前不会自动报出来，属于规范未覆盖的情况。
        """
        rows = [
            _entity_row_values(entity)
            for entity in entities
            if not isinstance(entity, (MachineEntity, BootEntity))
        ]
        written = await self._insert_in_batches(
            session,
            lambda batch: (
                pg_insert(EntityRow)
                .values(batch)
                .on_conflict_do_nothing(constraint="entities_scope_key")
            ),
            rows,
        )
        return written, len(rows) - written

    async def _insert_records(
        self, session: AsyncSession, records: Sequence[Record]
    ) -> tuple[int, int]:
        rows = [_record_row_values(record) for record in records]
        written = await self._insert_in_batches(
            session,
            lambda batch: (
                pg_insert(RecordRow)
                .values(batch)
                .on_conflict_do_nothing(index_elements=["collection_id", "record_id"])
            ),
            rows,
        )
        return written, len(rows) - written

    async def _transition(
        self, session: AsyncSession, collection_id: str, target: ImportStatus
    ) -> None:
        """在事务里锁住采集行并推进状态；不允许的迁移直接报错。"""
        current = (
            await session.execute(
                select(CollectionRow.import_status)
                .where(CollectionRow.collection_id == collection_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise StorageStateError(f"采集 {collection_id} 不在库里")
        if not can_transition(ImportStatus(current), target):
            raise StorageStateError(
                f"采集 {collection_id} 的导入状态不能从 {current} 变成 {target.value}"
            )
        if current == target.value:
            return
        await session.execute(
            update(CollectionRow)
            .where(CollectionRow.collection_id == collection_id)
            .values(import_status=target.value, updated_at=func.now())
        )

    async def _mark_failed_quietly(self, collection_id: str, message: str) -> None:
        """标记失败，但不让「标记失败」本身掩盖真正的失败原因。

        数据库整体不可用时 ``_mark_failed`` 也会抛；调用方要看到的是最初的错误，不是这次写状态
        的错误。标记丢失是可接受的（采集行仍在，重试时会再走一遍）。
        """
        try:
            await self._mark_failed(collection_id, message)
        except Exception:  # 原始异常必须原样抛给调用方
            pass

    async def _mark_failed(self, collection_id: str, message: str) -> None:
        """记录失败原因。只在 ``pending`` / ``importing`` 上生效，与状态迁移表一致。"""
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(CollectionRow)
                .where(
                    CollectionRow.collection_id == collection_id,
                    CollectionRow.import_status.in_(
                        [ImportStatus.PENDING.value, ImportStatus.IMPORTING.value]
                    ),
                )
                .values(
                    import_status=ImportStatus.FAILED.value,
                    import_error=message[:4000],
                    updated_at=func.now(),
                )
            )


# ----------------------------------------------------------------------
# 行与领域对象的互相转换
# ----------------------------------------------------------------------


def _time_column(clock_domain: ClockDomain):
    """时间范围筛选与排序用的列，由时钟域决定，不允许两者混用。"""
    if clock_domain is ClockDomain.BOOT_MONOTONIC:
        return RecordRow.monotonic_ns
    return RecordRow.realtime_ns


def _registration_problems(
    collection: CollectionRef, machine: MachineEntity, boot: BootEntity
) -> list[str]:
    problems: list[str] = []
    if machine.machine_id != collection.machine_id:
        problems.append(
            f"machine {machine.machine_id} 与采集的 machine_id {collection.machine_id} 不一致"
        )
    if boot.boot_id != collection.boot_id:
        problems.append(f"boot {boot.boot_id} 与采集的 boot_id {collection.boot_id} 不一致")
    if boot.machine_id != collection.machine_id:
        problems.append(f"boot {boot.boot_id} 的 machine_id {boot.machine_id} 与采集不一致")
    return problems


def _machine_statement(machine: MachineEntity):
    return pg_insert(MachineRow).values(
        machine_id=machine.machine_id,
        hostname=machine.hostname,
        os_release=machine.os_release,
        kernel_version=machine.kernel_version,
        architecture=machine.architecture,
    )


def _boot_statement(boot: BootEntity):
    return pg_insert(BootRow).values(
        machine_id=boot.machine_id,
        boot_id=boot.boot_id,
        started_realtime_ns=boot.started_realtime_ns,
        kernel_version=boot.kernel_version,
        boot_index=boot.boot_index,
    )


def _entity_row_values(entity: BaseModel) -> dict:
    if isinstance(entity, MetricStreamEntity):
        return {
            "entity_type": entity.entity_type,
            "entity_id": entity.entity_id,
            "machine_id": entity.machine_id,
            "boot_id": entity.boot_id,
            "name": entity.name,
            "unit": entity.unit,
            "available": entity.available,
            "unavailable_reason": entity.unavailable_reason,
            "attributes": {},
        }
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "machine_id": entity.machine_id,
        "boot_id": entity.boot_id,
        "name": entity.name,
        "unit": None,
        "available": None,
        "unavailable_reason": None,
        "attributes": entity.attributes,
    }


def _record_row_values(record: Record) -> dict:
    return {
        "collection_id": record.collection_id,
        "record_id": record.record_id,
        "kind": record.kind.value,
        "machine_id": record.machine_id,
        "boot_id": record.boot_id,
        "source_plugin": record.source_plugin,
        "stream_id": record.stream_id,
        "monotonic_ns": record.monotonic_ns,
        "realtime_ns": record.realtime_ns,
        "clock_domain": record.clock_domain.value,
        "entity": record.entity,
        "payload": record.payload,
        # 本主版本不认识的字段原样留着，摘要才核对得回来（见 RecordRow.extra）
        "extra": dict(record.model_extra or {}),
    }


def _record(row: RecordRow) -> Record:
    return Record(
        record_id=row.record_id,
        kind=RecordKind(row.kind),
        machine_id=row.machine_id,
        boot_id=row.boot_id,
        collection_id=row.collection_id,
        source_plugin=row.source_plugin,
        stream_id=row.stream_id,
        monotonic_ns=row.monotonic_ns,
        realtime_ns=row.realtime_ns,
        clock_domain=ClockDomain(row.clock_domain),
        entity=row.entity,
        payload=row.payload,
    )


def _stored_content(row: RecordRow) -> dict[str, Any]:
    """从库里的一行还原「重算 ``record_id`` 用的内容」：11 个字段加不认识的字段。"""
    content = _record(row).model_dump(mode="json", exclude={"record_id"})
    content.update(row.extra or {})
    return content


def _entity_model(row: EntityRow) -> BaseModel:
    if row.entity_type == EntityType.METRIC_STREAM.value:
        return MetricStreamEntity(
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            machine_id=row.machine_id,
            boot_id=row.boot_id,
            name=row.name,
            unit=row.unit,
            available=row.available,
            unavailable_reason=row.unavailable_reason,
        )
    return GenericEntity(
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        machine_id=row.machine_id,
        boot_id=row.boot_id,
        name=row.name,
        attributes=row.attributes,
    )


def _machine_entity(row: MachineRow) -> MachineEntity:
    return MachineEntity(
        entity_type="machine",
        machine_id=row.machine_id,
        hostname=row.hostname,
        os_release=row.os_release,
        kernel_version=row.kernel_version,
        architecture=row.architecture,
    )


def _boot_entity(row: BootRow) -> BootEntity:
    return BootEntity(
        entity_type="boot",
        boot_id=row.boot_id,
        machine_id=row.machine_id,
        started_realtime_ns=row.started_realtime_ns,
        kernel_version=row.kernel_version,
        boot_index=row.boot_index,
    )


def _collection_state(row: CollectionRow) -> CollectionState:
    return CollectionState(
        collection=CollectionRef(
            collection_id=row.collection_id,
            machine_id=row.machine_id,
            boot_id=row.boot_id,
            mode=CollectionMode(row.mode),
            created_realtime_ns=row.created_realtime_ns,
        ),
        import_status=ImportStatus(row.import_status),
        import_error=row.import_error,
        source_sha256=row.source_sha256,
        resources_available=row.resources_available,
        resources_reason=row.resources_reason,
    )
