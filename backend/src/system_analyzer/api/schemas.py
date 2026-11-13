"""对外 API 的响应模型与转换。

模型只描述**对外暴露的形状**，不复制存储层或时间线引擎的领域对象：

- 机器、启动、采集、记录取自 ``storage`` 与 ``domain``；
- 时间线数据取自 ``timeline``，API 只做序列化，不在这一层重算时间或合并轨道。

时间线条目按 ``type`` 区分三种形状（``event`` / ``span`` / ``metric``），用 discriminated union
表达，生成的 TypeScript 客户端因此能按 ``type`` 收窄，不需要在一堆可空字段里猜。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    MachineEntity,
    Record,
    RecordKind,
)
from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import JobRecord
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


# ----------------------------------------------------------------------
# 机器管理与采集任务（任务 4.1）
# ----------------------------------------------------------------------


class SystemInfoResponse(BaseModel):
    """目标机器在一次连接测试里报出的系统基本信息。取不到的项为 null，不填 0。"""

    hostname: str
    os_release: str | None = None
    kernel_version: str | None = None
    architecture: str | None = None
    cpu_count: int | None = None
    memory_total_bytes: int | None = None
    boot_id: str | None = None


TestConnectionFailureKind = Literal["connect", "auth", "timeout", "credential"]
"""连接测试失败的四种原因。

``credential`` 是「API 进程自己没有配置口令环境变量」，与 ``auth``（口令不对）不是一回事：
前者由运维在服务端改配置就能解决，后者要改目标机器的凭据。这四类之外的情况（例如连接成功
但读不到系统信息）按统一错误信封返回内部错误，不在这里强行归类。
"""


class TestConnectionFailure(BaseModel):
    """连接测试没成功时的原因。``message`` 已由后端脱敏，界面直接显示，不自己拼文案。"""

    kind: TestConnectionFailureKind = Field(
        description="connect=连不上，auth=认证失败，timeout=超时，credential=服务端没有配置口令"
    )
    message: str


class TestConnectionResponse(BaseModel):
    """一次连接测试的结果。

    「连不上」是连接测试的正常结果，不是请求失败，所以失败也用 200 返回：``ok`` 为 false 时
    ``failure`` 有值、``system`` 为 null。
    """

    ok: bool
    system: SystemInfoResponse | None = None
    failure: TestConnectionFailure | None = None


PUBLIC_COLLECTION_OPTIONS = frozenset(
    {
        "backup_paths",
        "desktop_user",
        "desktop_login_timeout",
        "phase_timeouts",
        "delivery",
    }
)
"""采集任务接口允许调用方设置的选项。"""

PUBLIC_DELIVERY_OPTIONS = frozenset(
    {
        "remote_path",
        "stage_local_pack",
        "retry_delay_seconds",
        "download_attempts",
    }
)
"""``options.delivery`` 里允许调用方设置的子项。

其余键是 Worker 内部编排使用的，必须在接口处丢掉：``database_url`` 会让 Worker 连接调用方
指定的数据库，``artifacts`` 会让打包读取 Worker 主机上的任意文件，``collection_id`` /
``machine_id`` / ``boot_id`` 会让调用方改写采集归属。
"""


def public_collection_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """只保留调用方可以设置的采集选项，丢掉 Worker 内部使用的键。"""
    sanitized = {key: value for key, value in options.items() if key in PUBLIC_COLLECTION_OPTIONS}
    delivery = sanitized.get("delivery")
    if isinstance(delivery, Mapping):
        sanitized["delivery"] = {
            key: value for key, value in delivery.items() if key in PUBLIC_DELIVERY_OPTIONS
        }
    elif "delivery" in sanitized:
        sanitized.pop("delivery")
    return sanitized


class CollectionTaskRequest(BaseModel):
    """提交一次采集任务。``target_id`` 必须是已经保存的目标机器。"""

    target_id: str = Field(min_length=1, description="要采集的目标机器 id")
    run_id: UUID | None = Field(
        None,
        description="采集任务 id，必须是 UUID；不传时由服务端生成",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "采集选项，只接受下列字段，其余键会被忽略："
            "``backup_paths``（要备份并恢复的远端文件）、"
            "``desktop_user`` / ``desktop_login_timeout``（LightDM 自动登录）、"
            "``phase_timeouts``（各阶段时间上限）、"
            "``delivery``（打包回传与导入的开关与远端路径，只接受 ``remote_path``、"
            "``stage_local_pack``、``retry_delay_seconds``、``download_attempts``；"
            "给了 ``remote_path`` 时默认先上传本地产出的包，显式传 ``stage_local_pack=false`` "
            "才表示远端已经有包、只做下载）"
        ),
    )

    @field_validator("options")
    @classmethod
    def _drop_internal_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        return public_collection_options(value)


class CollectionPhaseRecordResponse(BaseModel):
    phase: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    detail: str | None = None


class CollectionEventResponse(BaseModel):
    """一条采集操作记录。``detail`` 写失败原因或复用说明，是界面展示失败原因的来源之一。"""

    at: str
    kind: str
    phase: str
    step: str | None = None
    detail: str | None = None


class CollectionAttemptResponse(BaseModel):
    """一次采集尝试：三个阶段的状态与全部操作记录。"""

    attempt: int
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    phases: list[CollectionPhaseRecordResponse]
    events: list[CollectionEventResponse]


class CollectionTaskFailure(BaseModel):
    """采集没成功时的一句话原因。``phase`` 定位到失败的阶段，与阶段无关时为 null。"""

    phase: str | None = None
    message: str


class CollectionTaskResponse(BaseModel):
    """一次采集任务（``jobs`` 表里 ``kind = collect.target`` 的一行）的对外形状。"""

    task_id: str
    kind: str
    status: JobStatus
    attempts: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    target_id: str | None
    run_id: str | None
    cancel_requested: bool
    latest_attempt: CollectionAttemptResponse | None = Field(
        None, description="最近一次采集尝试；任务还没开始执行时为 null"
    )
    failure: CollectionTaskFailure | None = Field(
        None, description="采集没成功的可读原因；成功、执行中或取消后清理成功时为 null"
    )


_PHASE_FAILURES = frozenset({"failed", "timed_out"})
"""阶段状态里算「这次采集没成功」的两个取值。取消与中断不算失败，界面另有文案。"""


def collection_task_response(record: JobRecord) -> CollectionTaskResponse:
    """任务行 → 对外形状。

    采集结果不是任务行的列，而是 ``payload.result.collection`` 里的一份记录（由
    ``collection/record.py`` 写回）。这里只取界面要用的部分：最近一次尝试的阶段状态、
    操作记录与失败原因，不把整份 payload 透出去，也不透 ``last_error``（异常原文只进日志）。
    """
    payload: dict[str, Any] = record.payload or {}
    result = payload.get("result")
    collection = result.get("collection") if isinstance(result, dict) else None
    mapping = collection if isinstance(collection, dict) else {}
    raw_attempts = mapping.get("attempts")
    latest_raw = raw_attempts[-1] if isinstance(raw_attempts, list) and raw_attempts else None
    latest = _attempt_response(latest_raw) if isinstance(latest_raw, dict) else None
    return CollectionTaskResponse(
        task_id=str(record.id),
        kind=record.kind,
        status=record.status,
        attempts=record.attempts,
        max_attempts=record.max_attempts,
        created_at=record.created_at,
        updated_at=record.updated_at,
        target_id=_text(payload.get("target_id")),
        run_id=_text(payload.get("run_id")) or _text(mapping.get("run_id")),
        cancel_requested=bool(payload.get("cancel_requested", False)),
        latest_attempt=latest,
        failure=_task_failure(record, latest),
    )


def _task_failure(
    record: JobRecord, latest: CollectionAttemptResponse | None
) -> CollectionTaskFailure | None:
    """从最近一次尝试里找出可读的失败原因。

    阶段记录里的 ``detail`` 是编排写下的说明（例如恢复远端文件失败的原因），优先用它。
    没有阶段失败但任务整体失败时给一句固定说明：``jobs.last_error`` 带异常原文，可能含
    文件路径或 SQL 片段，只该留在 Worker 日志里。
    """
    if latest is not None:
        for phase in latest.phases:
            if phase.status in _PHASE_FAILURES:
                return CollectionTaskFailure(
                    phase=phase.phase,
                    message=phase.detail or f"{phase.phase} 阶段没有成功（{phase.status}）",
                )
    if record.status is JobStatus.FAILED:
        return CollectionTaskFailure(
            phase=None,
            message="任务执行失败，采集记录里没有写下失败原因；详细原因见 Worker 日志",
        )
    return None


def _attempt_response(raw: dict[str, Any]) -> CollectionAttemptResponse:
    return CollectionAttemptResponse(
        attempt=int(raw.get("attempt") or 0),
        status=str(raw.get("status") or ""),
        started_at=_text(raw.get("started_at")),
        finished_at=_text(raw.get("finished_at")),
        phases=[_phase_response(item) for item in _dict_items(raw.get("phases"))],
        events=[_event_response(item) for item in _dict_items(raw.get("events"))],
    )


def _phase_response(raw: dict[str, Any]) -> CollectionPhaseRecordResponse:
    return CollectionPhaseRecordResponse(
        phase=str(raw.get("phase") or ""),
        status=str(raw.get("status") or ""),
        started_at=_text(raw.get("started_at")),
        finished_at=_text(raw.get("finished_at")),
        detail=_text(raw.get("detail")),
    )


def _event_response(raw: dict[str, Any]) -> CollectionEventResponse:
    return CollectionEventResponse(
        at=str(raw.get("at") or ""),
        kind=str(raw.get("kind") or ""),
        phase=str(raw.get("phase") or ""),
        step=_text(raw.get("step")),
        detail=_text(raw.get("detail")),
    )


def _dict_items(value: object) -> list[dict[str, Any]]:
    """记录里的列表项：不是列表就当空，不是字典的项跳过，不因为半份数据整条报错。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: object) -> str | None:
    return None if value is None else str(value)
