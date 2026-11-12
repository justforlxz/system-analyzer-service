"""服务诊断视图的响应模型（任务 4.3）。

字段与 ``diagnostics.models`` 一一对应；这里只做序列化形状，不补默认值、不重算时间。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from system_analyzer.diagnostics.models import (
    AnnotationView,
    Evidence,
    Fact,
    JournalContext,
    JournalEntry,
    LifecycleSpan,
    ResourceUsage,
    ServiceDiagnostics,
    ServiceList,
    ServiceState,
    ServiceSummary,
    Uncertainty,
)


class FactResponse(BaseModel):
    value: str | None
    record_id: str | None
    monotonic_ns: int | None
    note: str | None


class ServiceSummaryResponse(BaseModel):
    entity: str
    unit: str
    lifecycle_start_ns: int | None
    lifecycle_end_ns: int | None
    annotation_count: int


class ServiceListResponse(BaseModel):
    machine_id: str
    boot_id: str
    services: list[ServiceSummaryResponse]


class ServiceStateResponse(BaseModel):
    state: str
    since_ns: int | None
    record_id: str | None
    stale: bool
    note: str | None


class LifecycleSpanResponse(BaseModel):
    record_id: str
    collection_id: str
    monotonic_ns: int | None
    end_monotonic_ns: int | None
    end_is_declared: bool
    span_type: str
    state: str | None
    invocation_id: str | None
    pid: str | None
    cgroup: str | None
    attributes: dict[str, Any]


class JournalEntryResponse(BaseModel):
    record_id: str
    collection_id: str
    monotonic_ns: int
    realtime_ns: int | None
    message: str | None
    identifier: str | None
    unit: str | None
    priority: str | None
    from_evidence: bool
    attributes: dict[str, Any]


class JournalContextResponse(BaseModel):
    window_start_ns: int
    window_end_ns: int
    total: int
    items: list[JournalEntryResponse]


class EvidenceResponse(BaseModel):
    record_id: str
    found: bool
    kind: str | None
    monotonic_ns: int | None
    message: str | None


class AnnotationResponse(BaseModel):
    record_id: str
    annotation_type: str
    analyzer: str
    severity: str
    message: str | None
    confidence: float | None
    monotonic_ns: int | None
    evidence: list[EvidenceResponse]
    evidence_missing: bool


class ResourcePointResponse(BaseModel):
    monotonic_ns: int
    value: float


class ResourceSeriesResponse(BaseModel):
    entity: str
    name: str
    unit: str
    points: list[ResourcePointResponse]


class ResourceLatestResponse(BaseModel):
    entity: str
    name: str
    unit: str
    value: float
    record_id: str
    monotonic_ns: int


class UnavailableResourceResponse(BaseModel):
    entity: str
    name: str
    unit: str
    reason: str


class ResourceUsageResponse(BaseModel):
    at_ns: int
    latest: list[ResourceLatestResponse]
    series: list[ResourceSeriesResponse]
    unavailable: list[UnavailableResourceResponse]
    note: str | None


class UncertaintyResponse(BaseModel):
    field: str
    note: str


class ServiceDiagnosticsResponse(BaseModel):
    machine_id: str
    boot_id: str
    entity: str
    unit: str
    at_ns: int
    state: ServiceStateResponse
    identity: dict[str, FactResponse]
    lifecycle: list[LifecycleSpanResponse]
    journal: JournalContextResponse
    resources: ResourceUsageResponse
    annotations: list[AnnotationResponse]
    uncertain: list[UncertaintyResponse]


def service_list_response(value: ServiceList) -> ServiceListResponse:
    return ServiceListResponse(
        machine_id=value.machine_id,
        boot_id=value.boot_id,
        services=[_summary(item) for item in value.services],
    )


def service_diagnostics_response(value: ServiceDiagnostics) -> ServiceDiagnosticsResponse:
    return ServiceDiagnosticsResponse(
        machine_id=value.machine_id,
        boot_id=value.boot_id,
        entity=value.entity,
        unit=value.unit,
        at_ns=value.at_ns,
        state=_state(value.state),
        identity={name: _fact(fact) for name, fact in value.identity.items()},
        lifecycle=[_span(item) for item in value.lifecycle],
        journal=_journal(value.journal),
        resources=_resources(value.resources),
        annotations=[_annotation(item) for item in value.annotations],
        uncertain=[_uncertainty(item) for item in value.uncertain],
    )


def _summary(value: ServiceSummary) -> ServiceSummaryResponse:
    return ServiceSummaryResponse(
        entity=value.entity,
        unit=value.unit,
        lifecycle_start_ns=value.lifecycle_start_ns,
        lifecycle_end_ns=value.lifecycle_end_ns,
        annotation_count=value.annotation_count,
    )


def _fact(value: Fact) -> FactResponse:
    return FactResponse(
        value=value.value,
        record_id=value.record_id,
        monotonic_ns=value.monotonic_ns,
        note=value.note,
    )


def _state(value: ServiceState) -> ServiceStateResponse:
    return ServiceStateResponse(
        state=value.state,
        since_ns=value.since_ns,
        record_id=value.record_id,
        stale=value.stale,
        note=value.note,
    )


def _span(value: LifecycleSpan) -> LifecycleSpanResponse:
    return LifecycleSpanResponse(
        record_id=value.record_id,
        collection_id=value.collection_id,
        monotonic_ns=value.monotonic_ns,
        end_monotonic_ns=value.end_monotonic_ns,
        end_is_declared=value.end_is_declared,
        span_type=value.span_type,
        state=value.state,
        invocation_id=value.invocation_id,
        pid=value.pid,
        cgroup=value.cgroup,
        attributes=value.attributes,
    )


def _journal_entry(value: JournalEntry) -> JournalEntryResponse:
    return JournalEntryResponse(
        record_id=value.record_id,
        collection_id=value.collection_id,
        monotonic_ns=value.monotonic_ns,
        realtime_ns=value.realtime_ns,
        message=value.message,
        identifier=value.identifier,
        unit=value.unit,
        priority=value.priority,
        from_evidence=value.from_evidence,
        attributes=value.attributes,
    )


def _journal(value: JournalContext) -> JournalContextResponse:
    return JournalContextResponse(
        window_start_ns=value.window_start_ns,
        window_end_ns=value.window_end_ns,
        total=value.total,
        items=[_journal_entry(item) for item in value.items],
    )


def _evidence(value: Evidence) -> EvidenceResponse:
    return EvidenceResponse(
        record_id=value.record_id,
        found=value.found,
        kind=value.kind,
        monotonic_ns=value.monotonic_ns,
        message=value.message,
    )


def _annotation(value: AnnotationView) -> AnnotationResponse:
    return AnnotationResponse(
        record_id=value.record_id,
        annotation_type=value.annotation_type,
        analyzer=value.analyzer,
        severity=value.severity,
        message=value.message,
        confidence=value.confidence,
        monotonic_ns=value.monotonic_ns,
        evidence=[_evidence(item) for item in value.evidence],
        evidence_missing=value.evidence_missing,
    )


def _resources(value: ResourceUsage) -> ResourceUsageResponse:
    return ResourceUsageResponse(
        at_ns=value.at_ns,
        latest=[
            ResourceLatestResponse(
                entity=item.entity,
                name=item.name,
                unit=item.unit,
                value=item.value,
                record_id=item.record_id,
                monotonic_ns=item.monotonic_ns,
            )
            for item in value.latest
        ],
        series=[
            ResourceSeriesResponse(
                entity=item.entity,
                name=item.name,
                unit=item.unit,
                points=[
                    ResourcePointResponse(monotonic_ns=point.monotonic_ns, value=point.value)
                    for point in item.points
                ],
            )
            for item in value.series
        ],
        unavailable=[
            UnavailableResourceResponse(
                entity=item.entity, name=item.name, unit=item.unit, reason=item.reason
            )
            for item in value.unavailable
        ],
        note=value.note,
    )


def _uncertainty(value: Uncertainty) -> UncertaintyResponse:
    return UncertaintyResponse(field=value.field, note=value.note)


__all__ = [
    "ServiceDiagnosticsResponse",
    "ServiceListResponse",
    "service_diagnostics_response",
    "service_list_response",
]
