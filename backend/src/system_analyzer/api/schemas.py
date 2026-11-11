"""对外 API 的响应模型与转换。

模型只描述**对外暴露的形状**，不复制存储层或时间线引擎的领域对象：

- 机器、启动、采集、记录取自 ``storage`` 与 ``domain``；
- 时间线数据取自 ``timeline``，API 只做序列化，不在这一层重算时间或合并轨道。

时间线条目按 ``type`` 区分三种形状（``event`` / ``span`` / ``metric``），用 discriminated union
表达，生成的 TypeScript 客户端因此能按 ``type`` 收窄，不需要在一堆可空字段里猜。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    MachineEntity,
    Record,
    RecordKind,
)
from system_analyzer.jobs.models import JobStatus
from system_analyzer.storage import CollectionState, ImportStatus
from system_analyzer.timeline import (
    ClockDrift,
    MetricPoint,
    ResourceStatus,
    TimeCursor,
    TimelineEvent,
    TimelineSpan,
    TimelineState,
    TimelineTracks,
    Track,
    TrackState,
    TrackStatus,
)


class Page[T](BaseModel):
    """分页结果。``total`` 是筛选后的总条数，与本次 ``limit`` / ``offset`` 无关。"""

    items: list[T]
    total: int
    limit: int
    offset: int


class MachineResponse(BaseModel):
    machine_id: str
    hostname: str
    os_release: str | None
    kernel_version: str | None
    architecture: str | None


class BootResponse(BaseModel):
    machine_id: str
    boot_id: str
    started_realtime_ns: int | None
    kernel_version: str | None
    boot_index: int | None


class CollectionResponse(BaseModel):
    collection_id: str
    machine_id: str
    boot_id: str
    mode: CollectionMode
    created_realtime_ns: int | None
    import_status: ImportStatus
    import_error: str | None
    source_sha256: str | None
    resources_available: bool | None
    resources_reason: str | None


class RecordResponse(BaseModel):
    record_id: str
    kind: RecordKind
    machine_id: str
    boot_id: str
    collection_id: str
    source_plugin: str
    stream_id: str
    monotonic_ns: int | None
    realtime_ns: int | None
    clock_domain: ClockDomain
    entity: str | None
    payload: dict[str, Any]


class RecordsPage(BaseModel):
    """一次采集里的记录页。

    ``collection_total`` 是这次采集的记录总数，**不受**时间范围与 ``kinds`` 筛选影响：
    存储层没有按筛选条件计数的接口，与其给一个算不准的「筛选后总数」，不如把口径写清楚。
    """

    items: list[RecordResponse]
    limit: int
    offset: int
    collection_total: int


class ResourceStatusResponse(BaseModel):
    available: bool | None
    reasons: list[str]


class ClockDriftResponse(BaseModel):
    anchor_ns: int | None
    drift_ns: int | None
    anchor_count: int
    unreliable: bool


class EventItemResponse(BaseModel):
    type: Literal["event"] = "event"
    record_id: str
    collection_id: str
    monotonic_ns: int | None
    realtime_ns: int | None
    entity: str | None
    source_plugin: str
    stream_id: str
    event_type: str
    severity: str
    message: str | None
    attributes: dict[str, Any]


class SpanItemResponse(BaseModel):
    type: Literal["span"] = "span"
    record_id: str
    collection_id: str
    monotonic_ns: int | None
    realtime_ns: int | None
    entity: str | None
    source_plugin: str
    stream_id: str
    span_type: str
    state: str | None
    end_monotonic_ns: int | None
    end_realtime_ns: int | None
    end_is_declared: bool
    closed_by: str | None
    attributes: dict[str, Any]


class MetricItemResponse(BaseModel):
    type: Literal["metric"] = "metric"
    record_id: str
    collection_id: str
    monotonic_ns: int | None
    realtime_ns: int | None
    entity: str | None
    source_plugin: str
    stream_id: str
    name: str
    value: float
    unit: str


TimelineItemResponse = Annotated[
    EventItemResponse | SpanItemResponse | MetricItemResponse,
    Field(discriminator="type"),
]


class TrackResponse(BaseModel):
    track_id: str
    kind: Literal["event", "span", "metric"]
    entity: str | None
    stream_id: str | None
    source_plugin: str | None
    status: TrackStatus
    unavailable_reason: str | None = Field(
        description="仅当 status 为 unavailable 时有值：这条流没有数据的原因"
    )
    name: str | None
    unit: str | None
    items: list[TimelineItemResponse]


class TracksResponse(BaseModel):
    machine_id: str
    boot_id: str
    window_start_ns: int | None
    window_end_ns: int | None
    axis_start_ns: int | None
    axis_end_ns: int | None
    tracks: list[TrackResponse]
    items: list[TimelineItemResponse]
    wall_clock_items: list[TimelineItemResponse]
    resources: ResourceStatusResponse
    clock: ClockDriftResponse


class TrackStateResponse(BaseModel):
    track_id: str
    kind: Literal["event", "span", "metric"]
    entity: str | None
    status: TrackStatus
    unavailable_reason: str | None
    metric: MetricItemResponse | None
    spans: list[SpanItemResponse]
    last_event: EventItemResponse | None


class TimelineStateResponse(BaseModel):
    machine_id: str
    boot_id: str
    monotonic_ns: int
    tracks: list[TrackStateResponse]
    resources: ResourceStatusResponse
    clock: ClockDriftResponse


class CursorResponse(BaseModel):
    machine_id: str
    boot_id: str
    start_ns: int | None
    end_ns: int | None
    position_ns: int | None
    previous_ns: int | None
    next_ns: int | None


class ImportSubmitResponse(BaseModel):
    """一次导入提交的结果：文件已经写入上传目录、任务已经入队。"""

    job_id: str
    upload_id: str
    plugin_id: str
    filename: str | None
    source_sha256: str
    size_bytes: int


class ImportJobResult(BaseModel):
    """导入任务写回的结果。字段缺失表示插件没有报告这一项。"""

    collection_id: str | None = None
    import_status: str | None = None
    records_written: int | None = None
    records_skipped: int | None = None
    skipped_reason: str | None = None


class ImportJobFailure(BaseModel):
    """任务失败的分类与对外说明。

    只给稳定的分类与一句面向调用方的说明；异常类型、堆栈与文件路径只写进 Worker 日志。
    """

    kind: Literal["invalid_input", "internal"]
    message: str


class ImportJobStatus(BaseModel):
    id: str
    kind: str
    status: JobStatus
    attempts: int
    max_attempts: int
    failure: ImportJobFailure | None = Field(
        description="任务失败时的分类与说明，成功或执行中为 null"
    )
    plugin_id: str | None
    upload_id: str | None
    filename: str | None
    result: ImportJobResult | None


class ImportStatusResponse(BaseModel):
    job: ImportJobStatus
    collection: CollectionResponse | None = Field(
        description="导入成功后对应的采集；任务还没写出结果时为 null"
    )


# ----------------------------------------------------------------------
# 领域对象 → 响应模型
# ----------------------------------------------------------------------


def machine_response(entity: MachineEntity) -> MachineResponse:
    return MachineResponse(
        machine_id=entity.machine_id,
        hostname=entity.hostname,
        os_release=entity.os_release,
        kernel_version=entity.kernel_version,
        architecture=entity.architecture,
    )


def boot_response(entity: BootEntity) -> BootResponse:
    return BootResponse(
        machine_id=entity.machine_id,
        boot_id=entity.boot_id,
        started_realtime_ns=entity.started_realtime_ns,
        kernel_version=entity.kernel_version,
        boot_index=entity.boot_index,
    )


def collection_response(state: CollectionState) -> CollectionResponse:
    collection = state.collection
    return CollectionResponse(
        collection_id=collection.collection_id,
        machine_id=collection.machine_id,
        boot_id=collection.boot_id,
        mode=collection.mode,
        created_realtime_ns=collection.created_realtime_ns,
        import_status=state.import_status,
        import_error=state.import_error,
        source_sha256=state.source_sha256,
        resources_available=state.resources_available,
        resources_reason=state.resources_reason,
    )


def record_response(record: Record) -> RecordResponse:
    return RecordResponse(
        record_id=record.record_id,
        kind=record.kind,
        machine_id=record.machine_id,
        boot_id=record.boot_id,
        collection_id=record.collection_id,
        source_plugin=record.source_plugin,
        stream_id=record.stream_id,
        monotonic_ns=record.monotonic_ns,
        realtime_ns=record.realtime_ns,
        clock_domain=record.clock_domain,
        entity=record.entity,
        payload=record.payload,
    )


def event_item(event: TimelineEvent) -> EventItemResponse:
    return EventItemResponse(
        record_id=event.record_id,
        collection_id=event.collection_id,
        monotonic_ns=event.monotonic_ns,
        realtime_ns=event.realtime_ns,
        entity=event.entity,
        source_plugin=event.source_plugin,
        stream_id=event.stream_id,
        event_type=event.event_type,
        severity=event.severity,
        message=event.message,
        attributes=event.attributes,
    )


def span_item(span: TimelineSpan) -> SpanItemResponse:
    return SpanItemResponse(
        record_id=span.record_id,
        collection_id=span.collection_id,
        monotonic_ns=span.monotonic_ns,
        realtime_ns=span.realtime_ns,
        entity=span.entity,
        source_plugin=span.source_plugin,
        stream_id=span.stream_id,
        span_type=span.span_type,
        state=span.state,
        end_monotonic_ns=span.end_monotonic_ns,
        end_realtime_ns=span.end_realtime_ns,
        end_is_declared=span.end_is_declared,
        closed_by=span.closed_by,
        attributes=span.attributes,
    )


def metric_item(point: MetricPoint) -> MetricItemResponse:
    return MetricItemResponse(
        record_id=point.record_id,
        collection_id=point.collection_id,
        monotonic_ns=point.monotonic_ns,
        realtime_ns=point.realtime_ns,
        entity=point.entity,
        source_plugin=point.source_plugin,
        stream_id=point.stream_id,
        name=point.name,
        value=point.value,
        unit=point.unit,
    )


def timeline_item(item: TimelineEvent | TimelineSpan | MetricPoint) -> TimelineItemResponse:
    """按条目类型选一个具体形状；三种条目各有一个收窄到具体类型的转换函数。"""
    if isinstance(item, TimelineEvent):
        return event_item(item)
    if isinstance(item, TimelineSpan):
        return span_item(item)
    return metric_item(item)


def track_response(track: Track) -> TrackResponse:
    return TrackResponse(
        track_id=track.track_id,
        kind=track.kind.value,
        entity=track.entity,
        stream_id=track.stream_id,
        source_plugin=track.source_plugin,
        status=track.status,
        unavailable_reason=track.unavailable_reason,
        name=track.name,
        unit=track.unit,
        items=[timeline_item(item) for item in track.items],
    )


def resources_response(resources: ResourceStatus) -> ResourceStatusResponse:
    return ResourceStatusResponse(available=resources.available, reasons=list(resources.reasons))


def clock_response(clock: ClockDrift) -> ClockDriftResponse:
    return ClockDriftResponse(
        anchor_ns=clock.anchor_ns,
        drift_ns=clock.drift_ns,
        anchor_count=clock.anchor_count,
        unreliable=clock.unreliable,
    )


def tracks_response(tracks: TimelineTracks) -> TracksResponse:
    return TracksResponse(
        machine_id=tracks.machine_id,
        boot_id=tracks.boot_id,
        window_start_ns=tracks.window_start_ns,
        window_end_ns=tracks.window_end_ns,
        axis_start_ns=tracks.axis_start_ns,
        axis_end_ns=tracks.axis_end_ns,
        tracks=[track_response(track) for track in tracks.tracks],
        items=[timeline_item(item) for item in tracks.items],
        wall_clock_items=[timeline_item(item) for item in tracks.wall_clock_items],
        resources=resources_response(tracks.resources),
        clock=clock_response(tracks.clock),
    )


def track_state_response(state: TrackState) -> TrackStateResponse:
    return TrackStateResponse(
        track_id=state.track_id,
        kind=state.kind.value,
        entity=state.entity,
        status=state.status,
        unavailable_reason=state.unavailable_reason,
        metric=None if state.metric is None else metric_item(state.metric),
        spans=[span_item(span) for span in state.spans],
        last_event=None if state.last_event is None else event_item(state.last_event),
    )


def state_response(state: TimelineState) -> TimelineStateResponse:
    return TimelineStateResponse(
        machine_id=state.machine_id,
        boot_id=state.boot_id,
        monotonic_ns=state.monotonic_ns,
        tracks=[track_state_response(track) for track in state.tracks],
        resources=resources_response(state.resources),
        clock=clock_response(state.clock),
    )


def cursor_response(cursor: TimeCursor) -> CursorResponse:
    return CursorResponse(
        machine_id=cursor.machine_id,
        boot_id=cursor.boot_id,
        start_ns=cursor.start_ns,
        end_ns=cursor.end_ns,
        position_ns=cursor.position_ns,
        previous_ns=cursor.previous_ns,
        next_ns=cursor.next_ns,
    )
