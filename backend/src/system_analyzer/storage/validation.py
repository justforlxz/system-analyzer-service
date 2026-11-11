"""诊断数据的写入前校验与完整性检查。

全部是纯函数，不访问数据库。判定规则来自 ``docs/data-model.md`` 与 ``docs/time-base.md``，
这些函数被两条路径共用：

- 写入前（``store.DiagnosticStore.import_collection``、``append_records``）：不合法的数据不落库，
  不静默补默认值。
- 读出后（``store.DiagnosticStore.check_collection_integrity``）：把库里的记录重建一遍再复核，
  发现偏离只报告，不修复。

``record_id`` 的核对要单独说明：它是「除它之外全部字段」的摘要（data-model 第 8 节），算它的
输入是文件里的**原始字典**，``domain.bootrec.validate_bootrec`` 用的就是原始字典与同一个
``compute_record_id``。存储层因此接受两种输入（``IncomingRecord``）：导入方手上有原始字典时传
字典，摘要就能逐字节核对，含本主版本不认识的字段也一样；只有模型时按「模型里真的写了的字段」
核对，模型已经丢掉的未知字段无法参与，这类记录请改传原始字典。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from system_analyzer.domain.bootrec import (
    PAYLOAD_MODELS,
    RECORD_ID_PATTERN,
    BootEntity,
    ClockDomain,
    CollectionRef,
    MachineEntity,
    MetricStreamEntity,
    Record,
    RecordKind,
    compute_record_id,
)

_OPTIONAL_RECORD_KEYS = ("entity", "monotonic_ns", "realtime_ns")
"""0.2 里允许缺省的顶层字段：其余字段必填，缺了在写入前就会被拒。"""


class OriginalRecord(Record):
    """带原始内容的记录，用来按文件里的内容核对 ``record_id``。

    比 0.2 的 ``Record`` 只放宽一处：允许携带本主版本不认识的字段（``docs/data-model.md``
    第 3 节要求读取方忽略它们），这样重算摘要时它们仍然参与，不会被静默丢掉。
    """

    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True, slots=True)
class IncomingRecord:
    """要写入的一条记录，连同重算 ``record_id`` 所用的内容。"""

    record: Record
    content: dict[str, Any]

    @property
    def from_original_content(self) -> bool:
        """内容是否就是调用方给的原始字典（未知字段还在）。"""
        return isinstance(self.record, OriginalRecord)


def incoming_record(value: Record | Mapping[str, Any]) -> IncomingRecord:
    """把调用方给的记录统一成「记录 + 重算摘要用的内容」。

    导入方应传原始字典：只有它能让摘要逐字段核对。传模型时只能按模型里真的写了的字段重算，
    模型已经丢掉的未知字段无从恢复。
    """
    if isinstance(value, OriginalRecord):
        record: Record = value
    elif isinstance(value, Mapping):
        record = OriginalRecord.model_validate(value)
    else:
        record = value
    content = record.model_dump(mode="json", exclude_unset=True, exclude={"record_id"})
    return IncomingRecord(record=record, content=content)


def incoming_records(
    values: Sequence[Record | Mapping[str, Any]],
) -> tuple[list[IncomingRecord], list[str]]:
    """把一批记录转成 ``IncomingRecord``；字段不合法的记成问题，不抛异常。

    校验失败要让调用方走与其他不合法数据相同的路（标 failed + ``StorageValidationError``），
    所以这里把 Pydantic 的 ``ValidationError`` 翻译成人能看懂的问题列表。
    """
    incoming: list[IncomingRecord] = []
    problems: list[str] = []
    for index, value in enumerate(values, start=1):
        try:
            incoming.append(incoming_record(value))
        except ValidationError as error:
            for detail in error.errors():
                location = ".".join(str(part) for part in detail["loc"]) or "整条记录"
                problems.append(f"第 {index} 条记录不合法：{location} {detail['msg']}")
    return incoming, problems


def stored_content_problems(record_id: str, content: Mapping[str, Any]) -> list[str]:
    """库里重建的一条记录，是否仍与它的 ``record_id`` 对得上。

    存储层保存 0.2 的 11 个字段与本主版本不认识的字段，但**没有**保存「原始文件里哪些
    可空字段真的写了」。摘要按原始内容算，所以这里按「``entity`` / ``monotonic_ns`` /
    ``realtime_ns`` 中取值为 null 的那几个键可能被省略」的每种组合各算一次，命中任一
    即认为自洽。``payload`` 是必填字段，按一定存在处理。

    这个核对有两个界限，写在 ``store.check_collection_integrity`` 的文档里：只能发现
    通过本模块写入之后被改动的记录；原始内容若还省略了别的字段（当前规范里只有这三个可空
    字段会缺省），会被报成不一致。
    """
    if not RECORD_ID_PATTERN.fullmatch(record_id):
        return [f"record_id={record_id} 不是 32 位小写十六进制"]
    optional_keys = [key for key in _OPTIONAL_RECORD_KEYS if content.get(key, "") is None]
    candidates: set[str] = set()
    for count in range(len(optional_keys) + 1):
        for omitted in combinations(optional_keys, count):
            body = {key: value for key, value in content.items() if key not in omitted}
            candidates.add(compute_record_id(body))
    if record_id in candidates:
        return []
    return [
        f"record_id={record_id} 与内容算出的摘要都不一致（可能：{', '.join(sorted(candidates))}），"
        "记录在写入之后被改过"
    ]


def machine_entity(entities: Sequence[BaseModel]) -> MachineEntity | None:
    """从一份采集的实体里取出 Machine 行；没有就返回 ``None``。"""
    for entity in entities:
        if isinstance(entity, MachineEntity):
            return entity
    return None


def boot_entity(entities: Sequence[BaseModel]) -> BootEntity | None:
    """从一份采集的实体里取出 Boot 行；没有就返回 ``None``。"""
    for entity in entities:
        if isinstance(entity, BootEntity):
            return entity
    return None


def entity_problems(collection: CollectionRef, entities: Sequence[BaseModel]) -> list[str]:
    """实体是否与这次采集的作用域一致、实体之间是否自相矛盾。"""
    problems: list[str] = []
    seen: set[str] = set()
    machines: set[str] = set()
    boots: set[str] = set()
    for entity in entities:
        label = getattr(entity, "entity_id", getattr(entity, "machine_id", "?"))
        if isinstance(entity, MachineEntity):
            if entity.machine_id in machines:
                problems.append(f"machine {entity.machine_id} 出现两行")
            machines.add(entity.machine_id)
            if entity.machine_id != collection.machine_id:
                problems.append(f"machine {entity.machine_id} 与采集的 machine_id 不一致")
            continue
        if isinstance(entity, BootEntity):
            if entity.boot_id in boots:
                problems.append(f"boot {entity.boot_id} 出现两行")
            boots.add(entity.boot_id)
            if entity.boot_id != collection.boot_id:
                problems.append(f"boot {entity.boot_id} 与采集的 boot_id 不一致")
            if entity.machine_id != collection.machine_id:
                problems.append(
                    f"boot {entity.boot_id} 的 machine_id {entity.machine_id} 与采集不一致"
                )
            continue
        if entity.entity_id in seen:
            problems.append(f"entity {label} 出现两行")
        seen.add(entity.entity_id)
        if entity.machine_id != collection.machine_id:
            problems.append(f"entity {label} 的 machine_id 与采集不一致")
        if entity.boot_id is not None and entity.boot_id != collection.boot_id:
            problems.append(f"entity {label} 的 boot_id 与采集不一致")
        if isinstance(entity, MetricStreamEntity):
            if entity.available and entity.unavailable_reason:
                problems.append(f"指标流 {label} 标记为可用，却写了不可用原因")
            if not entity.available and not entity.unavailable_reason:
                problems.append(f"指标流 {label} 标记为不可用，必须写明不可用原因")
    if not machines:
        problems.append("缺少这台机器的 machine 行")
    if not boots:
        problems.append("缺少这次启动的 boot 行")
    return problems


def record_problems(
    collection: CollectionRef,
    entities: Sequence[BaseModel],
    records: Sequence[Record],
) -> list[str]:
    """记录与采集、实体、幂等键的关系是否正确（不核对内容摘要）。"""
    problems: list[str] = []
    by_entity_id = {entity.entity_id: entity for entity in entities if hasattr(entity, "entity_id")}
    seen_record_ids: set[str] = set()
    for record in records:
        problems.extend(_scope_problems(collection, record))
        problems.extend(_timestamp_problems(record))
        if record.record_id in seen_record_ids:
            problems.append(f"record_id={record.record_id} 在同一批数据里出现两次")
        seen_record_ids.add(record.record_id)
        problems.extend(_payload_problems(record))
        problems.extend(_reference_problems(record, by_entity_id))
    return problems


def identity_problems(incoming: IncomingRecord) -> list[str]:
    """``record_id`` 的格式，以及它与内容算出的摘要是否一致。

    与 ``domain.bootrec.validate_bootrec`` 用的是同一个 ``compute_record_id``；区别只在输入：
    这里优先用调用方给的原始内容。
    """
    record = incoming.record
    label = f"record_id={record.record_id}"
    if not RECORD_ID_PATTERN.fullmatch(record.record_id):
        return [f"{label} 不是 32 位小写十六进制"]
    expected = compute_record_id(incoming.content)
    if expected == record.record_id:
        return []
    hint = (
        "记录被改动过，或生成方式不符合 data-model 第 8 节"
        if incoming.from_original_content
        else "记录被改动过，或记录带了本主版本不认识的字段；核对摘要请传原始字典"
    )
    return [f"{label} 与内容算出的 {expected} 不一致：{hint}"]


def resources_problems(
    available: bool | None,
    reason: str | None,
    records: Sequence[Record],
) -> list[str]:
    """这份数据整体的资源数据可用性与记录是否自相矛盾（data-model 第 7 节第一行）。

    ``available`` 取自 ``manifest.resources``：``false`` 表示这次采集根本没有资源数据，
    ``null`` 表示不知道。知道「没有资源数据」却有 metric 记录，就是伪造数据。
    """
    metric_count = sum(1 for record in records if record.kind is RecordKind.METRIC)
    if available is None:
        return []
    if not available and not reason:
        return ["标记为没有资源数据，必须写明原因"]
    if available and reason:
        return ["标记为有资源数据，却写了没有资源数据的原因"]
    if not available and metric_count:
        return [f"标记为没有资源数据，却有 {metric_count} 条 metric 记录"]
    return []


def bundle_problems(
    collection: CollectionRef,
    entities: Sequence[BaseModel],
    records: Sequence[IncomingRecord],
) -> list[str]:
    """写入一份采集之前的完整检查。返回空列表表示可以写入。"""
    return (
        entity_problems(collection, entities)
        + record_problems(collection, entities, [item.record for item in records])
        + [problem for item in records for problem in identity_problems(item)]
    )


def _scope_problems(collection: CollectionRef, record: Record) -> list[str]:
    label = f"record_id={record.record_id}"
    problems: list[str] = []
    if record.collection_id != collection.collection_id:
        problems.append(f"{label} 的 collection_id 与采集不一致")
    if record.machine_id != collection.machine_id:
        problems.append(f"{label} 的 machine_id 与采集不一致")
    if record.boot_id != collection.boot_id:
        problems.append(f"{label} 的 boot_id 与采集不一致；跨启动的数据必须拆成多份")
    return problems


def _timestamp_problems(record: Record) -> list[str]:
    label = f"record_id={record.record_id}"
    if record.monotonic_ns is None and record.realtime_ns is None:
        return [f"{label} 的 monotonic_ns 与 realtime_ns 都为空，无法定位到时间轴"]
    expected = (
        ClockDomain.BOOT_MONOTONIC if record.monotonic_ns is not None else ClockDomain.WALL_REALTIME
    )
    if record.clock_domain is not expected:
        return [
            f"{label} 的 clock_domain 应为 {expected.value}"
            "（monotonic_ns 非空即为 boot_monotonic，为空即为 wall_realtime）"
        ]
    return []


def _payload_problems(record: Record) -> list[str]:
    payload_model = PAYLOAD_MODELS[record.kind]
    try:
        payload_model.model_validate(record.payload)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        return [f"record_id={record.record_id} 的 payload 不合法：{location} {first['msg']}"]
    return []


def _reference_problems(record: Record, by_entity_id: dict[str, BaseModel]) -> list[str]:
    label = f"record_id={record.record_id}"
    if record.entity is None:
        if record.kind in (RecordKind.METRIC, RecordKind.SPAN):
            return [f"{label} 的 {record.kind.value} 记录必须关联实体"]
        return []
    entity = by_entity_id.get(record.entity)
    if entity is None:
        return [f"{label} 指向的实体 {record.entity} 不在这次采集里"]
    if record.kind is not RecordKind.METRIC:
        return []
    if not isinstance(entity, MetricStreamEntity):
        return [f"{label} 是 metric 记录，但 {record.entity} 不是 metric_stream 实体"]
    problems: list[str] = []
    if not entity.available:
        problems.append(
            f"{label} 写入了指标流 {record.entity} 的数据，但这条流标记为不可用；"
            "缺失不得用 0 或占位值代替"
        )
    if record.payload.get("name") != entity.name:
        problems.append(f"{label} 的指标名与实体 {record.entity} 的 name 不一致")
    if record.payload.get("unit") != entity.unit:
        problems.append(f"{label} 的指标单位与实体 {record.entity} 的 unit 不一致")
    return problems
