"""诊断视图输出形状。

字段都来自存储层里已有的记录：生命周期区间、分析结论、Journal 事件与资源采样。这里只做
归并与时间定位，不新增数据模型，也不推算原始记录里没有的事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DiagnosticsError(Exception):
    """诊断查询的基类异常。"""


class DiagnosticsBootNotFoundError(DiagnosticsError):
    """这次启动不在库里。"""


class DiagnosticsServiceNotFoundError(DiagnosticsError):
    """这次启动里没有这个服务。"""


@dataclass(frozen=True, slots=True)
class Fact:
    """一项可能缺失的关联信息。``value`` 为 None 时必须读 ``note`` 的「无法确定」说明。"""

    value: str | None
    record_id: str | None = None
    monotonic_ns: int | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceSummary:
    entity: str
    unit: str
    lifecycle_start_ns: int | None
    lifecycle_end_ns: int | None
    annotation_count: int


@dataclass(frozen=True, slots=True)
class ServiceState:
    state: str
    since_ns: int | None
    record_id: str | None
    stale: bool
    note: str | None


@dataclass(frozen=True, slots=True)
class LifecycleSpan:
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
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    record_id: str
    collection_id: str
    monotonic_ns: int
    realtime_ns: int | None
    message: str | None
    identifier: str | None
    unit: str | None
    priority: str | None
    from_evidence: bool
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JournalContext:
    window_start_ns: int
    window_end_ns: int
    total: int
    items: tuple[JournalEntry, ...]


@dataclass(frozen=True, slots=True)
class Evidence:
    record_id: str
    found: bool
    kind: str | None
    monotonic_ns: int | None
    message: str | None


@dataclass(frozen=True, slots=True)
class AnnotationView:
    record_id: str
    annotation_type: str
    analyzer: str
    severity: str
    message: str | None
    confidence: float | None
    monotonic_ns: int | None
    evidence: tuple[Evidence, ...]
    evidence_missing: bool


@dataclass(frozen=True, slots=True)
class ResourcePoint:
    monotonic_ns: int
    value: float


@dataclass(frozen=True, slots=True)
class ResourceSeries:
    entity: str
    name: str
    unit: str
    points: tuple[ResourcePoint, ...]


@dataclass(frozen=True, slots=True)
class ResourceLatest:
    entity: str
    name: str
    unit: str
    value: float
    record_id: str
    monotonic_ns: int


@dataclass(frozen=True, slots=True)
class UnavailableResource:
    entity: str
    name: str
    unit: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    at_ns: int
    latest: tuple[ResourceLatest, ...]
    series: tuple[ResourceSeries, ...]
    unavailable: tuple[UnavailableResource, ...]
    note: str | None


@dataclass(frozen=True, slots=True)
class Uncertainty:
    field: str
    note: str


@dataclass(frozen=True, slots=True)
class ServiceDiagnostics:
    machine_id: str
    boot_id: str
    entity: str
    unit: str
    at_ns: int
    state: ServiceState
    identity: dict[str, Fact]
    lifecycle: tuple[LifecycleSpan, ...]
    journal: JournalContext
    resources: ResourceUsage
    annotations: tuple[AnnotationView, ...]
    uncertain: tuple[Uncertainty, ...]


@dataclass(frozen=True, slots=True)
class ServiceList:
    machine_id: str
    boot_id: str
    services: tuple[ServiceSummary, ...]
