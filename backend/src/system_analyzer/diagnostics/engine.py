"""按时间游标重建服务状态并汇总诊断证据。

读侧只做三件事：

1. 从 2.1 写下的生命周期区间（``span``）与 2.2 / 2.1 写下的结论（``annotation``）里找出与
   所选服务相关的记录；
2. 用 4.2 的游标时刻定位该服务的状态、PID、Invocation ID、cgroup 与最近的资源采样；
3. 把结论引用的原始 Journal 记录解析出来；记录不在库里时明确标成「无法确定」，不补推断。

不重新分析 Journal，也不改分析结论：没有证据就返回 ``None`` 与原因，不写确定性答案。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from system_analyzer.diagnostics.models import (
    AnnotationView,
    DiagnosticsBootNotFoundError,
    DiagnosticsServiceNotFoundError,
    Evidence,
    Fact,
    JournalContext,
    JournalEntry,
    LifecycleSpan,
    ResourceLatest,
    ResourcePoint,
    ResourceSeries,
    ResourceUsage,
    ServiceDiagnostics,
    ServiceList,
    ServiceState,
    ServiceSummary,
    UnavailableResource,
    Uncertainty,
)
from system_analyzer.domain.bootrec import BootEntity, Record, RecordKind

SERVICE_PREFIX = "service:"
DEFAULT_JOURNAL_WINDOW_NS = 60_000_000_000
"""关联 Journal 的默认时间窗口：游标前后各一分钟。"""

MAX_SERIES_POINTS = 600
"""单条资源流最多返回的采样点数，避免一次请求把整次启动的采样搬进响应。"""

OOM_ANNOTATION_TYPES = frozenset({"oom.kernel.killed_process", "oom.systemd_oomd.killed_cgroup"})

STALE_STATE_NOTE = (
    "采集已经结束，游标之后的这段时间没有新的生命周期记录，无法确定当时是否仍是这个状态"
)


class DiagnosticsReader(Protocol):
    """诊断查询需要的最小只读接口，由 ``DiagnosticStore`` 满足。"""

    async def get_boot(self, machine_id: str, boot_id: str) -> BootEntity | None: ...

    async def list_records_in_boot(
        self,
        machine_id: str,
        boot_id: str,
        *,
        kinds: Sequence[RecordKind] | None = None,
    ) -> Sequence[Record]: ...

    async def list_entities(self, machine_id: str, boot_id: str) -> Sequence[Any]: ...


class ServiceDiagnosticsEngine:
    """服务状态重建与诊断结果读取。无状态：数据每次都从只读入口取。"""

    def __init__(self, reader: DiagnosticsReader) -> None:
        self._reader = reader

    async def services(self, machine_id: str, boot_id: str) -> ServiceList:
        """这次启动里能被诊断的服务。

        来源是实体表里的 service 实体，加上区间与结论记录上出现的 ``service:`` 实体。事件记录
        不参与：按数据契约，记录引用的实体必须已经写入实体表。
        """
        await self._require_boot(machine_id, boot_id)
        # 服务列表只用到区间与结论，不读事件与采样点。真实采集里事件占九成以上，整表读出来
        # 既慢又会在行转对象的阶段占住事件循环。
        records = list(
            await self._reader.list_records_in_boot(
                machine_id,
                boot_id,
                kinds=(RecordKind.SPAN, RecordKind.ANNOTATION),
            )
        )
        entities = list(await self._reader.list_entities(machine_id, boot_id))
        # 汇总要遍历数万条记录，是纯计算。放到工作线程执行，避免一次诊断请求把 API 的
        # 事件循环占满，导致同一时刻的健康检查、任务查询等请求都没有响应。
        services = await asyncio.to_thread(_summarize_services, records, entities)
        return ServiceList(
            machine_id=machine_id,
            boot_id=boot_id,
            services=services,
        )

    async def service_at(
        self,
        machine_id: str,
        boot_id: str,
        entity_id: str,
        *,
        at_ns: int | None = None,
        journal_window_ns: int = DEFAULT_JOURNAL_WINDOW_NS,
    ) -> ServiceDiagnostics:
        """指定服务在 ``at_ns`` 时刻的状态与诊断证据；缺省停在这次启动的最早时刻。"""
        await self._require_boot(machine_id, boot_id)
        if not entity_id.startswith(SERVICE_PREFIX):
            raise DiagnosticsServiceNotFoundError(
                f"{entity_id} 不是服务实体：诊断视图只接受 service:<unit> 形式的实体"
            )
        records = list(await self._reader.list_records_in_boot(machine_id, boot_id))
        entities = list(await self._reader.list_entities(machine_id, boot_id))
        return await asyncio.to_thread(
            _service_diagnostics,
            machine_id,
            boot_id,
            entity_id,
            records,
            entities,
            at_ns,
            journal_window_ns,
        )

    async def _require_boot(self, machine_id: str, boot_id: str) -> None:
        if await self._reader.get_boot(machine_id, boot_id) is None:
            raise DiagnosticsBootNotFoundError(f"启动 {machine_id}/{boot_id} 不在库里")


def _service_diagnostics(
    machine_id: str,
    boot_id: str,
    entity_id: str,
    records: Sequence[Record],
    entities: Sequence[Any],
    at_ns: int | None,
    journal_window_ns: int,
) -> ServiceDiagnostics:
    """``service_at`` 的纯计算部分：在 Worker 线程里重建状态与证据。"""
    moment = _default_at(records) if at_ns is None else at_ns
    unit = entity_id[len(SERVICE_PREFIX) :]
    if not _exists(entity_id, records, entities):
        raise DiagnosticsServiceNotFoundError(f"启动 {machine_id}/{boot_id} 里没有服务 {unit}")

    service_events = [
        record for record in records if record.kind is RecordKind.EVENT and _belongs(record, unit)
    ]
    service_event_ids = {record.record_id for record in service_events}
    spans = sorted(
        (
            record
            for record in records
            if record.kind is RecordKind.SPAN and record.entity == entity_id
        ),
        key=_record_order,
    )
    annotations = [
        record
        for record in records
        if record.kind is RecordKind.ANNOTATION
        and _annotation_relevant(record, entity_id, service_event_ids)
    ]
    lifecycle = tuple(_span_view(record) for record in spans)
    state = _state_at(spans, moment, _data_end(records))
    identity = _identity_at(spans, service_events, unit, moment)
    annotation_views = tuple(_annotation_view(record, records) for record in annotations)
    journal = _journal(service_events, records, annotations, moment, journal_window_ns)
    resources = _resources(records, entities, unit, identity, moment, journal_window_ns)

    uncertain = _uncertainties(state, identity, resources, annotation_views)
    return ServiceDiagnostics(
        machine_id=machine_id,
        boot_id=boot_id,
        entity=entity_id,
        unit=unit,
        at_ns=moment,
        state=state,
        identity=identity,
        lifecycle=lifecycle,
        journal=journal,
        resources=resources,
        annotations=annotation_views,
        uncertain=uncertain,
    )


def _summarize_services(
    records: Sequence[Record], entities: Sequence[Any]
) -> tuple[ServiceSummary, ...]:
    """一次遍历记录，按实体累计生命周期起止与结论条数。"""
    summaries: dict[str, _ServiceAccumulator] = {}
    for entity in entities:
        entity_id = getattr(entity, "entity_id", None)
        if getattr(entity, "entity_type", None) != "service" or not entity_id:
            continue
        summaries[entity_id] = _ServiceAccumulator()
    for record in records:
        entity_id = record.entity
        if not entity_id or not entity_id.startswith(SERVICE_PREFIX):
            continue
        accumulator = summaries.setdefault(entity_id, _ServiceAccumulator())
        if record.kind is RecordKind.SPAN:
            accumulator.add_span(record)
        elif record.kind is RecordKind.ANNOTATION:
            accumulator.annotation_count += 1
    return tuple(
        accumulator.summary(entity_id) for entity_id, accumulator in sorted(summaries.items())
    )


class _ServiceAccumulator:
    """一个服务在列表里的累计值：生命周期起止与结论条数。"""

    def __init__(self) -> None:
        self.lifecycle_start_ns: int | None = None
        self.lifecycle_end_ns: int | None = None
        self.annotation_count = 0

    def add_span(self, record: Record) -> None:
        start = record.monotonic_ns
        if start is not None and (
            self.lifecycle_start_ns is None or start < self.lifecycle_start_ns
        ):
            self.lifecycle_start_ns = start
        end = _first_int(record.payload.get("end_monotonic_ns"))
        if end is not None and (self.lifecycle_end_ns is None or end > self.lifecycle_end_ns):
            self.lifecycle_end_ns = end

    def summary(self, entity_id: str) -> ServiceSummary:
        return ServiceSummary(
            entity=entity_id,
            unit=entity_id[len(SERVICE_PREFIX) :],
            lifecycle_start_ns=self.lifecycle_start_ns,
            lifecycle_end_ns=self.lifecycle_end_ns,
            annotation_count=self.annotation_count,
        )


def _span_view(record: Record) -> LifecycleSpan:
    attributes = _attributes(record)
    end_ns = _first_int(record.payload.get("end_monotonic_ns"))
    return LifecycleSpan(
        record_id=record.record_id,
        collection_id=record.collection_id,
        monotonic_ns=record.monotonic_ns,
        end_monotonic_ns=end_ns,
        end_is_declared=end_ns is not None or record.payload.get("end_realtime_ns") is not None,
        span_type=str(record.payload.get("span_type") or ""),
        state=_text(record.payload.get("state")),
        invocation_id=_text(attributes.get("invocation_id")),
        pid=_text(attributes.get("pid")),
        cgroup=_text(attributes.get("cgroup")),
        attributes=dict(attributes),
    )


def _state_at(spans: Sequence[Record], at_ns: int, data_end_ns: int | None) -> ServiceState:
    """按游标重建状态：区间内是 active / activating，区间结束后才是终态。

    生命周期区间上的 ``state`` 不是「区间内每时每刻的状态」：已结束的 active 区间把终态
    （inactive / failed）写在区间上，终态只在区间结束时刻生效。这里按开始与结束时刻重新定位，
    不能直接拿 ``payload.state`` 当整段区间的状态。
    """
    prior = [record for record in spans if _start(record) <= at_ns]
    if not prior:
        return ServiceState(
            state="unknown",
            since_ns=None,
            record_id=None,
            stale=False,
            note=f"游标 {at_ns} 之前没有该服务的生命周期记录，无法确定当时的状态",
        )
    active = [
        record
        for record in prior
        if _span_type(record) == "service.active" and (_end(record) is None or at_ns < _end(record))
    ]
    if active:
        chosen = max(active, key=_record_order)
        return ServiceState(
            state="active",
            since_ns=chosen.monotonic_ns,
            record_id=chosen.record_id,
            stale=_end(chosen) is None and data_end_ns is not None and at_ns > data_end_ns,
            note=None,
        )
    activating = [
        record
        for record in prior
        if _span_type(record) == "service.activating"
        and (_end(record) is None or at_ns < _end(record))
    ]
    if activating:
        chosen = max(activating, key=_record_order)
        return ServiceState(
            state="activating",
            since_ns=chosen.monotonic_ns,
            record_id=chosen.record_id,
            stale=_end(chosen) is None and data_end_ns is not None and at_ns > data_end_ns,
            note=None,
        )
    finished = [record for record in prior if _end(record) is not None and _end(record) <= at_ns]
    if finished:
        latest = max(
            finished,
            key=lambda record: (_end(record) or 0, record.collection_id, record.record_id),
        )
        terminal = _text(latest.payload.get("state")) or "unknown"
        if terminal in {"inactive", "failed"}:
            return ServiceState(
                state=terminal,
                since_ns=_end(latest),
                record_id=latest.record_id,
                stale=False,
                note=None,
            )
    return ServiceState(
        state="unknown",
        since_ns=None,
        record_id=None,
        stale=False,
        note="生命周期区间已经结束，但没有终态记录，无法确定游标之后的状态",
    )


def _identity_at(
    spans: Sequence[Record], events: Sequence[Record], unit: str, at_ns: int
) -> dict[str, Fact]:
    prior_spans = [record for record in spans if _start(record) <= at_ns]
    span = max(prior_spans, key=_record_order) if prior_spans else None
    span_attributes = _attributes(span) if span is not None else {}
    prior_events = sorted(
        (record for record in events if (record.monotonic_ns or 0) <= at_ns),
        key=_record_order,
        reverse=True,
    )

    def from_span(name: str) -> tuple[str | None, Record | None]:
        value = _text(span_attributes.get(name))
        return (value, span) if value is not None else (None, None)

    def from_object_fields(*fields: str) -> tuple[str | None, Record | None]:
        """被操作 Unit 自己写在消息里的字段，与日志发送方无关。"""
        for record in prior_events:
            raw = _fields(record)
            value = next((_text(raw.get(field)) for field in fields if _text(raw.get(field))), None)
            if value is not None:
                return value, record
        return None, None

    def from_sender(name: str, *fields: str) -> tuple[str | None, Record | None]:
        """描述「谁写了这条日志」的字段；只有发送方就是该 Unit 时才采用。

        systemd 由 PID 1 打印生命周期消息时，``_SYSTEMD_UNIT`` 是 ``init.scope``，
        ``_SYSTEMD_CGROUP`` 是 ``/init.scope``，``_SYSTEMD_INVOCATION_ID`` 也指向发送方。
        这些字段不属于被操作的 Unit，不能拿来当作它的进程身份或资源匹配依据。
        这条规则与 ``analyzers/systemd_lifecycle.py`` 的 ``from_unit_itself`` 判断一致。
        """
        for record in prior_events:
            attributes = _attributes(record)
            raw = _fields(record)
            if not _sent_by_unit(attributes, raw, unit):
                continue
            value = _text(attributes.get(name))
            if value is None:
                value = next(
                    (_text(raw.get(field)) for field in fields if _text(raw.get(field))), None
                )
            if value is not None:
                return value, record
        return None, None

    def invocation_from_events() -> tuple[str | None, Record | None]:
        # INVOCATION_ID / USER_INVOCATION_ID 描述被操作的 Unit，发送方是 systemd 也可采用；
        # 只有归一化进 attributes["invocation_id"] 的 _SYSTEMD_INVOCATION_ID 属于发送方。
        value, source = from_object_fields("INVOCATION_ID", "USER_INVOCATION_ID")
        if value is not None:
            return value, source
        return from_sender("invocation_id", "_SYSTEMD_INVOCATION_ID")

    def fact(name: str, lookup: Callable[[], tuple[str | None, Record | None]]) -> Fact:
        value, source = from_span(name)
        if value is None:
            value, source = lookup()
        if value is None:
            return Fact(
                value=None, note=f"生命周期区间与游标之前的关联 Journal 都没有 {name}，无法确定"
            )
        return Fact(
            value=value,
            record_id=source.record_id if source is not None else None,
            monotonic_ns=source.monotonic_ns if source is not None else None,
        )

    return {
        "pid": fact("pid", lambda: from_sender("pid", "_PID")),
        "invocation_id": fact("invocation_id", invocation_from_events),
        "cgroup": fact("cgroup", lambda: from_sender("cgroup", "_SYSTEMD_CGROUP")),
    }


def _journal(
    service_events: Sequence[Record],
    records: Sequence[Record],
    annotations: Sequence[Record],
    at_ns: int,
    window_ns: int,
) -> JournalContext:
    lower = max(0, at_ns - window_ns)
    upper = at_ns + window_ns
    by_id = {record.record_id: record for record in records}
    selected = {
        record.record_id: record
        for record in service_events
        if record.monotonic_ns is not None and lower <= record.monotonic_ns <= upper
    }
    evidence_ids = {
        ref
        for record in annotations
        for ref in _evidence_refs(record)
        if ref in by_id and by_id[ref].kind is RecordKind.EVENT
    }
    for record_id in evidence_ids:
        selected[record_id] = by_id[record_id]
    items = tuple(
        _journal_entry(record, record.record_id in evidence_ids)
        for record in sorted(selected.values(), key=_record_order)
    )
    return JournalContext(window_start_ns=lower, window_end_ns=upper, total=len(items), items=items)


def _journal_entry(record: Record, from_evidence: bool) -> JournalEntry:
    attributes = _attributes(record)
    raw = _fields(record)
    return JournalEntry(
        record_id=record.record_id,
        collection_id=record.collection_id,
        monotonic_ns=record.monotonic_ns or 0,
        realtime_ns=record.realtime_ns,
        message=_text(record.payload.get("message")),
        identifier=_text(attributes.get("identifier")) or _text(raw.get("SYSLOG_IDENTIFIER")),
        unit=_text(attributes.get("unit"))
        or _text(raw.get("UNIT"))
        or _text(raw.get("_SYSTEMD_UNIT")),
        priority=_text(attributes.get("priority")) or _text(raw.get("PRIORITY")),
        from_evidence=from_evidence,
        attributes=dict(attributes),
    )


def _annotation_view(record: Record, records: Sequence[Record]) -> AnnotationView:
    by_id = {item.record_id: item for item in records}
    evidence: list[Evidence] = []
    for ref in _evidence_refs(record):
        source = by_id.get(ref)
        if source is None:
            evidence.append(
                Evidence(record_id=ref, found=False, kind=None, monotonic_ns=None, message=None)
            )
            continue
        evidence.append(
            Evidence(
                record_id=source.record_id,
                found=True,
                kind=source.kind.value,
                monotonic_ns=source.monotonic_ns,
                message=_text(source.payload.get("message")),
            )
        )
    return AnnotationView(
        record_id=record.record_id,
        annotation_type=str(record.payload.get("annotation_type") or ""),
        analyzer=str(record.payload.get("analyzer") or record.source_plugin),
        severity=str(record.payload.get("severity") or "info"),
        message=_text(record.payload.get("message")),
        confidence=_confidence(record.payload.get("confidence")),
        monotonic_ns=record.monotonic_ns,
        evidence=tuple(evidence),
        evidence_missing=any(not item.found for item in evidence),
    )


def _resources(
    records: Sequence[Record],
    entities: Sequence[Any],
    unit: str,
    identity: Mapping[str, Fact],
    at_ns: int,
    window_ns: int,
) -> ResourceUsage:
    cgroup_fact = identity.get("cgroup")
    cgroup = cgroup_fact.value if cgroup_fact is not None else None
    lower = max(0, at_ns - window_ns)
    grouped: dict[str, list[Record]] = {}
    matched_any = False
    for record in records:
        if record.kind is not RecordKind.METRIC or record.monotonic_ns is None:
            continue
        target = _metric_target(record.entity)
        if target is None or not _target_matches(target, unit, cgroup):
            continue
        matched_any = True
        if lower <= record.monotonic_ns <= at_ns:
            grouped.setdefault(record.entity or "", []).append(record)

    series: list[ResourceSeries] = []
    latest: list[ResourceLatest] = []
    for entity_id, points in sorted(grouped.items()):
        ordered = sorted(points, key=_record_order)[-MAX_SERIES_POINTS:]
        last = ordered[-1]
        name = str(last.payload.get("name") or "")
        unit_name = str(last.payload.get("unit") or "")
        series.append(
            ResourceSeries(
                entity=entity_id,
                name=name,
                unit=unit_name,
                points=tuple(
                    ResourcePoint(
                        monotonic_ns=item.monotonic_ns or 0, value=float(item.payload["value"])
                    )
                    for item in ordered
                ),
            )
        )
        latest.append(
            ResourceLatest(
                entity=entity_id,
                name=name,
                unit=unit_name,
                value=float(last.payload["value"]),
                record_id=last.record_id,
                monotonic_ns=last.monotonic_ns or 0,
            )
        )

    unavailable: list[UnavailableResource] = []
    for entity in entities:
        if getattr(entity, "entity_type", None) != "metric_stream":
            continue
        target = _metric_target(getattr(entity, "entity_id", None))
        if target is None or not _target_matches(target, unit, cgroup):
            continue
        if getattr(entity, "available", True):
            continue
        unavailable.append(
            UnavailableResource(
                entity=entity.entity_id,
                name=entity.name,
                unit=entity.unit,
                reason=entity.unavailable_reason,
            )
        )

    if latest:
        note = None
    elif unavailable:
        note = None
    elif matched_any:
        note = "游标之前没有该服务的资源采样，无法确定当时的资源使用"
    else:
        note = "这次启动没有该服务的资源采样，无法确定资源使用"
    return ResourceUsage(
        at_ns=at_ns,
        latest=tuple(latest),
        series=tuple(series),
        unavailable=tuple(unavailable),
        note=note,
    )


def _uncertainties(
    state: ServiceState,
    identity: Mapping[str, Fact],
    resources: ResourceUsage,
    annotations: Sequence[AnnotationView],
) -> tuple[Uncertainty, ...]:
    items: list[Uncertainty] = []
    if state.note is not None or state.stale:
        items.append(Uncertainty(field="state", note=state.note or STALE_STATE_NOTE))
    for name, fact in identity.items():
        if fact.value is None and fact.note is not None:
            items.append(Uncertainty(field=name, note=fact.note))
    if resources.note is not None:
        items.append(Uncertainty(field="resources", note=resources.note))
    for annotation in annotations:
        if annotation.evidence_missing:
            missing = [item.record_id for item in annotation.evidence if not item.found]
            missing_text = "、".join(missing)
            items.append(
                Uncertainty(
                    field="evidence",
                    note=(
                        f"结论 {annotation.record_id} 引用的记录不在库里：{missing_text}，"
                        "无法确定该结论的证据"
                    ),
                )
            )
    return tuple(items)


def _exists(entity_id: str, records: Sequence[Record], entities: Sequence[Any]) -> bool:
    if any(getattr(entity, "entity_id", None) == entity_id for entity in entities):
        return True
    return any(record.entity == entity_id for record in records)


def _default_at(records: Sequence[Record]) -> int:
    moments = [record.monotonic_ns for record in records if record.monotonic_ns is not None]
    return min(moments, default=0)


def _data_end(records: Sequence[Record]) -> int | None:
    moments = [record.monotonic_ns for record in records if record.monotonic_ns is not None]
    return max(moments, default=None)


def _span_type(record: Record) -> str:
    return str(record.payload.get("span_type") or "")


def _annotation_relevant(record: Record, entity_id: str, service_event_ids: set[str]) -> bool:
    if record.entity == entity_id:
        return True
    annotation_type = str(record.payload.get("annotation_type") or "")
    if annotation_type not in OOM_ANNOTATION_TYPES:
        return False
    return bool(set(_evidence_refs(record)) & service_event_ids)


def _belongs(record: Record, unit: str) -> bool:
    if record.entity == f"{SERVICE_PREFIX}{unit}":
        return True
    attributes = _attributes(record)
    raw = _fields(record)
    if unit in {
        _text(attributes.get("unit")),
        _text(raw.get("_SYSTEMD_UNIT")),
        _text(raw.get("UNIT")),
        _text(raw.get("USER_UNIT")),
    }:
        return True
    cgroup = _text(raw.get("_SYSTEMD_CGROUP"))
    return cgroup is not None and cgroup.endswith(f"/{unit}")


def _sent_by_unit(attributes: Mapping[str, Any], raw: Mapping[str, Any], unit: str) -> bool:
    return unit in {_text(attributes.get("unit")), _text(raw.get("_SYSTEMD_UNIT"))}


def _target_matches(target: str, unit: str, cgroup: str | None) -> bool:
    if target == unit:
        return True
    return cgroup is not None and target == cgroup


def _metric_target(entity_id: str | None) -> str | None:
    if not isinstance(entity_id, str) or not entity_id.startswith("metric:"):
        return None
    parts = entity_id.split(":", 2)
    return parts[2] if len(parts) == 3 else None


def _attributes(record: Record | None) -> Mapping[str, Any]:
    if record is None:
        return {}
    value = record.payload.get("attributes")
    return value if isinstance(value, Mapping) else {}


def _fields(record: Record) -> Mapping[str, Any]:
    value = _attributes(record).get("fields")
    return value if isinstance(value, Mapping) else {}


def _evidence_refs(record: Record) -> list[str]:
    refs = record.payload.get("evidence_refs")
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, str)]


def _start(record: Record) -> int:
    return record.monotonic_ns or 0


def _end(record: Record) -> int | None:
    return _first_int(record.payload.get("end_monotonic_ns"))


def _record_order(record: Record) -> tuple[int, str, str]:
    return (_start(record), record.collection_id, record.record_id)


def _first_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
