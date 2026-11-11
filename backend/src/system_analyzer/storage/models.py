"""诊断数据的表结构与导入状态机。

字段与判定规则来自 ``docs/data-model.md``（第 3 节统一记录字段、第 4 节六个 payload、第 5 节实体）
与 ``docs/time-base.md``（第 2 节时钟域、第 7 节排序键）。本模块只声明表结构，以及「导入状态的
取值与允许的迁移」这一处纯计算；读写代码在 ``store.py``，写入前校验在 ``validation.py``。

五张表与 0.2 的对象一一对应：

| 表 | 对应对象 | 唯一键 |
| --- | --- | --- |
| ``machines`` | Machine（data-model 第 5.1 节） | ``machine_id`` |
| ``boots`` | Boot（第 5.2 节） | ``(machine_id, boot_id)`` |
| ``collections`` | Collection（第 5.3 节） | ``collection_id`` |
| ``entities`` | Entity（第 5.4 节，含 metric_stream） | ``(machine_id, boot_id, entity_id)`` |
| ``records`` | 统一记录（第 3 节） | ``(collection_id, record_id)`` |

三处结构决定的理由：

- ``boots`` 用 ``(machine_id, boot_id)`` 而不是只靠 ``boot_id``：虚拟机从快照恢复会把同一个
  ``boot_id`` 带回另一台机器，单列做主键会让两台机器互相覆盖（``docs/time-base.md`` 第 9 节）。
- ``records`` 直接把 0.2 第 8 节的导入唯一键 ``(collection_id, record_id)`` 当主键，幂等写入用它
  做 ``ON CONFLICT DO NOTHING``，不需要另加代理键。
- ``entities`` 的 ``boot_id`` 按 0.2 允许为空（跨启动的实体），所以主键用代理列，唯一约束带
  ``NULLS NOT DISTINCT``：否则 ``boot_id`` 为空的实体重复导入会各写一行。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from system_analyzer.domain.bootrec import (
    ClockDomain,
    CollectionMode,
    EntityType,
    RecordKind,
)


class Base(DeclarativeBase):
    """全部表共用的元数据，``migrations/env.py`` 用它做迁移与模型的对照。"""

    metadata = MetaData()


class ImportStatus(StrEnum):
    """Collection 级导入状态，与 ``collections.import_status`` 列的取值一一对应。

    - ``pending``：这次采集已知，但还没有开始导入。
    - ``importing``：正在写入实体与记录。
    - ``imported``：数据已完整写入。
    - ``failed``：导入失败，``import_error`` 写明原因；可以重试。
    """

    PENDING = "pending"
    IMPORTING = "importing"
    IMPORTED = "imported"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[ImportStatus, frozenset[ImportStatus]] = {
    ImportStatus.PENDING: frozenset({ImportStatus.IMPORTING, ImportStatus.FAILED}),
    ImportStatus.IMPORTING: frozenset({ImportStatus.IMPORTED, ImportStatus.FAILED}),
    # 失败的采集可以重试；已导入的采集可以再导入一次以补齐补充数据
    ImportStatus.FAILED: frozenset({ImportStatus.IMPORTING}),
    ImportStatus.IMPORTED: frozenset({ImportStatus.IMPORTING}),
}


def can_transition(current: ImportStatus, target: ImportStatus) -> bool:
    """``current`` 能否迁移到 ``target``。迁移到自身视为无操作，返回 ``True``。"""
    if current is target:
        return True
    return target in ALLOWED_TRANSITIONS[current]


def _values(enum_type: type[StrEnum]) -> str:
    """把枚举的取值拼成 SQL ``IN`` 列表，检查约束由此与领域枚举保持同一份真值。"""
    return ", ".join(f"'{member.value}'" for member in enum_type)


_ENTITY_TYPES_WITHOUT_MACHINE_AND_BOOT = (
    EntityType.SERVICE,
    EntityType.PROCESS,
    EntityType.USER_SESSION,
    EntityType.CGROUP,
    EntityType.METRIC_STREAM,
)

# 只有需要修改状态、因此需要记录写入时刻的表才有时间列；记录与实体是不可变的事实，
# 它们的时刻由记录自己的 monotonic_ns / realtime_ns 表达，不再重复存一份入库时间。


class MachineRow(Base):
    """一台被诊断的机器（``docs/data-model.md`` 第 5.1 节）。"""

    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    os_release: Mapped[str | None] = mapped_column(Text)
    kernel_version: Mapped[str | None] = mapped_column(Text)
    architecture: Mapped[str | None] = mapped_column(Text)


class BootRow(Base):
    """一次系统启动（``docs/data-model.md`` 第 5.2 节）。"""

    __tablename__ = "boots"

    machine_id: Mapped[str] = mapped_column(
        Text, ForeignKey("machines.machine_id", ondelete="CASCADE"), primary_key=True
    )
    boot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_realtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    kernel_version: Mapped[str | None] = mapped_column(Text)
    boot_index: Mapped[int | None] = mapped_column(BigInteger)


class CollectionRow(Base):
    """一次采集（``docs/data-model.md`` 第 5.3 节），外加导入状态。"""

    __tablename__ = "collections"

    collection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    machine_id: Mapped[str] = mapped_column(Text, nullable=False)
    boot_id: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    created_realtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    import_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{ImportStatus.PENDING.value}'")
    )
    import_error: Mapped[str | None] = mapped_column(Text)
    # 第一级幂等：整份文件的摘要（data-model 第 8 节），同一个文件再上传一次时直接跳过
    source_sha256: Mapped[str | None] = mapped_column(Text)
    # 这份数据整体有没有资源指标。取自 manifest.resources（data-model 第 7 节）：
    # false 表示这次采集根本没有资源数据（如 Journal-only 导入），与「某条流不可用」是两件事。
    # 不知道时留空，不用 false 冒充「没有资源数据」。
    resources_available: Mapped[bool | None] = mapped_column(Boolean)
    resources_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["machine_id", "boot_id"],
            ["boots.machine_id", "boots.boot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(f"mode IN ({_values(CollectionMode)})", name="collections_mode_check"),
        CheckConstraint(
            f"import_status IN ({_values(ImportStatus)})",
            name="collections_import_status_check",
        ),
        CheckConstraint(
            "resources_available IS NULL"
            " OR (resources_available AND resources_reason IS NULL)"
            " OR (NOT resources_available AND resources_reason IS NOT NULL)",
            name="collections_resources_reason_check",
        ),
        Index("collections_boot_idx", "machine_id", "boot_id"),
        Index("collections_digest_idx", "machine_id", "source_sha256"),
    )


class EntityRow(Base):
    """服务、进程、用户会话、cgroup、指标流（``docs/data-model.md`` 第 5.4 节）。

    Machine 与 Boot 有独立的表，不写在这里。``entity_id`` 只在同一个 boot 内唯一，
    所以唯一约束带上 ``(machine_id, boot_id)``。
    """

    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    machine_id: Mapped[str] = mapped_column(
        Text, ForeignKey("machines.machine_id", ondelete="CASCADE"), nullable=False
    )
    boot_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # 只有 metric_stream 用这两列：available=false 表示这条流没有数据，原因必须写明。
    # 其它实体类型这两列必须为空，避免把「不可用」这个语义扩散到没有指标含义的实体上。
    available: Mapped[bool | None] = mapped_column(Boolean)
    unavailable_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "machine_id",
            "boot_id",
            "entity_id",
            name="entities_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            f"entity_type IN ({_values(_ENTITY_TYPES_WITHOUT_MACHINE_AND_BOOT)})",
            name="entities_type_check",
        ),
        CheckConstraint(
            "(entity_type = 'metric_stream' AND available IS NOT NULL"
            " AND name IS NOT NULL AND unit IS NOT NULL)"
            " OR (entity_type <> 'metric_stream'"
            " AND available IS NULL AND unavailable_reason IS NULL)",
            name="entities_metric_stream_columns_check",
        ),
        CheckConstraint(
            "available IS NULL"
            " OR (available AND unavailable_reason IS NULL)"
            " OR (NOT available AND unavailable_reason IS NOT NULL)",
            name="entities_metric_stream_reason_check",
        ),
    )


class RecordRow(Base):
    """统一记录（``docs/data-model.md`` 第 3 节）。

    六种 ``kind`` 共用这张表，差异全在 ``payload`` 里；这样时间线只需要按 ``monotonic_ns``
    排序一条查询路径，不需要为每种对象各写一条。
    """

    __tablename__ = "records"

    collection_id: Mapped[str] = mapped_column(
        Text, ForeignKey("collections.collection_id", ondelete="CASCADE"), primary_key=True
    )
    record_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    machine_id: Mapped[str] = mapped_column(Text, nullable=False)
    boot_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_plugin: Mapped[str] = mapped_column(Text, nullable=False)
    stream_id: Mapped[str] = mapped_column(Text, nullable=False)
    monotonic_ns: Mapped[int | None] = mapped_column(BigInteger)
    realtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    clock_domain: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 本主版本不认识的顶层字段，原样留着。data-model 第 3 节要求读取方忽略它们，但
    # ``record_id`` 是按「含这些字段的原始内容」算的：不留下来，读出来就再也核对不了摘要，
    # 也无法原样导出。查询与时间线都不读这一列。
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["machine_id", "boot_id"],
            ["boots.machine_id", "boots.boot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(f"kind IN ({_values(RecordKind)})", name="records_kind_check"),
        CheckConstraint(
            f"clock_domain IN ({_values(ClockDomain)})", name="records_clock_domain_check"
        ),
        # 0.2 第 3 节：monotonic_ns 与 realtime_ns 至少有一个非空
        CheckConstraint(
            "monotonic_ns IS NOT NULL OR realtime_ns IS NOT NULL",
            name="records_timestamp_check",
        ),
        # time-base 第 2 节：monotonic_ns 非空当且仅当 clock_domain = boot_monotonic
        CheckConstraint(
            "(clock_domain = 'boot_monotonic') = (monotonic_ns IS NOT NULL)",
            name="records_clock_domain_matches_monotonic_check",
        ),
        # 排序键（time-base 第 7.1 节）：boot 内按 (monotonic_ns, collection_id, record_id)
        Index(
            "records_boot_order_idx",
            "machine_id",
            "boot_id",
            "monotonic_ns",
            "collection_id",
            "record_id",
        ),
        # 只有墙钟的记录按 realtime_ns 单独排（time-base 第 10 节）
        Index(
            "records_boot_wall_idx",
            "machine_id",
            "boot_id",
            "realtime_ns",
            "collection_id",
            "record_id",
        ),
        # 按采集取有序记录（前缀 collection_id 已在主键里，这里补齐排序与时间范围）
        Index("records_collection_order_idx", "collection_id", "monotonic_ns", "record_id"),
        # 按指标流取一条序列
        Index("records_entity_idx", "machine_id", "boot_id", "entity", "monotonic_ns"),
    )
