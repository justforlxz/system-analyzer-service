"""API 单元测试用的替身：内存存储与内存任务队列。

替身只实现接口路由真正调用的方法，行为与 ``DiagnosticStore`` 的公开语义一致（筛选、排序、
``limit``），不复制存储层内部的校验与事务。真实链路的断言在
``tests/integration/test_api_import_timeline.py`` 里用 PostgreSQL 跑。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    MetricStreamEntity,
    Record,
    RecordKind,
)
from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import JobRecord
from system_analyzer.storage import CollectionState, ImportStatus

MACHINE = "machine:uos-PC"
_WHEN = datetime(2026, 1, 1, tzinfo=UTC)


# ----------------------------------------------------------------------
# 固定数据构造
# ----------------------------------------------------------------------


def machine(machine_id: str = MACHINE, hostname: str = "uos-PC") -> MachineEntity:
    return MachineEntity(
        entity_type="machine",
        machine_id=machine_id,
        hostname=hostname,
        os_release="UOS 25",
        kernel_version="6.6.0",
    )


def boot(
    boot_id: str = "boot:a",
    *,
    machine_id: str = MACHINE,
    boot_index: int = 1,
    started_realtime_ns: int | None = 1_000_000_000_000,
) -> BootEntity:
    return BootEntity(
        entity_type="boot",
        boot_id=boot_id,
        machine_id=machine_id,
        started_realtime_ns=started_realtime_ns,
        boot_index=boot_index,
    )


def collection(
    collection_id: str = "collection:a1",
    *,
    machine_id: str = MACHINE,
    boot_id: str = "boot:a",
    status: ImportStatus = ImportStatus.IMPORTED,
) -> CollectionState:
    return CollectionState(
        collection=CollectionRef(
            collection_id=collection_id,
            machine_id=machine_id,
            boot_id=boot_id,
            mode=CollectionMode.JOURNAL_IMPORT,
            created_realtime_ns=None,
        ),
        import_status=status,
        import_error=None,
        source_sha256=None,
        resources_available=False,
        resources_reason="只含 Journal",
    )


def event(
    number: int,
    monotonic_ns: int,
    *,
    collection_id: str = "collection:a1",
    boot_id: str = "boot:a",
    entity: str | None = "service:nginx.service",
) -> Record:
    return Record(
        record_id=f"{number:032x}",
        kind=RecordKind.EVENT,
        machine_id=MACHINE,
        boot_id=boot_id,
        collection_id=collection_id,
        source_plugin="journal.archive",
        stream_id="journal",
        monotonic_ns=monotonic_ns,
        realtime_ns=monotonic_ns + 1_000_000,
        clock_domain=ClockDomain.BOOT_MONOTONIC,
        entity=entity,
        payload={"event_type": "journal.entry", "severity": "info", "message": f"e{number}"},
    )


def metric(
    number: int,
    monotonic_ns: int,
    *,
    entity: str = "metric:cpu.total.utilization",
    collection_id: str = "collection:a1",
    boot_id: str = "boot:a",
) -> Record:
    return Record(
        record_id=f"{number:032x}",
        kind=RecordKind.METRIC,
        machine_id=MACHINE,
        boot_id=boot_id,
        collection_id=collection_id,
        source_plugin="journal.archive",
        stream_id="cpu",
        monotonic_ns=monotonic_ns,
        realtime_ns=monotonic_ns + 1_000_000,
        clock_domain=ClockDomain.BOOT_MONOTONIC,
        entity=entity,
        payload={"name": "cpu.total.utilization", "value": 12.5, "unit": "percent"},
    )


def stream(
    entity_id: str = "metric:cpu.total.utilization",
    *,
    available: bool = True,
    reason: str | None = None,
    boot_id: str = "boot:a",
) -> MetricStreamEntity:
    return MetricStreamEntity(
        entity_type="metric_stream",
        entity_id=entity_id,
        machine_id=MACHINE,
        boot_id=boot_id,
        name="cpu.total.utilization",
        unit="percent",
        available=available,
        unavailable_reason=reason,
    )


# ----------------------------------------------------------------------
# 存储替身
# ----------------------------------------------------------------------


@dataclass
class FakeStore:
    """同时满足查询路由与 ``TimelineReader`` 的内存实现。"""

    machines: dict[str, MachineEntity] = field(default_factory=dict)
    boots: dict[tuple[str, str], BootEntity] = field(default_factory=dict)
    collections: dict[str, CollectionState] = field(default_factory=dict)
    records: list[Record] = field(default_factory=list)
    streams: dict[tuple[str, str], list[MetricStreamEntity]] = field(default_factory=dict)

    async def get_machine(self, machine_id: str) -> MachineEntity | None:
        return self.machines.get(machine_id)

    async def list_boots(self, machine_id: str) -> list[BootEntity]:
        found = [item for (machine, _), item in self.boots.items() if machine == machine_id]
        return sorted(found, key=lambda item: item.boot_index or 0)

    async def get_boot(self, machine_id: str, boot_id: str) -> BootEntity | None:
        return self.boots.get((machine_id, boot_id))

    async def get_collection(self, collection_id: str) -> CollectionState | None:
        return self.collections.get(collection_id)

    async def list_collections(
        self,
        *,
        machine_id: str | None = None,
        boot_id: str | None = None,
        status: ImportStatus | None = None,
    ) -> list[CollectionState]:
        states = list(self.collections.values())
        if machine_id is not None:
            states = [state for state in states if state.collection.machine_id == machine_id]
        if boot_id is not None:
            states = [state for state in states if state.collection.boot_id == boot_id]
        if status is not None:
            states = [state for state in states if state.import_status is status]
        return states

    async def list_metric_streams(self, machine_id: str, boot_id: str) -> list[MetricStreamEntity]:
        return list(self.streams.get((machine_id, boot_id), []))

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
        records = [
            record
            for record in self.records
            if record.machine_id == machine_id
            and record.boot_id == boot_id
            and record.clock_domain is clock_domain
            and (not kinds or record.kind in kinds)
        ]
        return records if limit is None else records[:limit]

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
        column = "monotonic_ns" if clock_domain is ClockDomain.BOOT_MONOTONIC else "realtime_ns"
        records = sorted(
            (
                record
                for record in self.records
                if record.collection_id == collection_id and record.clock_domain is clock_domain
            ),
            key=lambda record: (getattr(record, column) or 0, record.record_id),
        )
        if start_ns is not None:
            records = [record for record in records if getattr(record, column) >= start_ns]
        if end_ns is not None:
            records = [record for record in records if getattr(record, column) <= end_ns]
        if kinds:
            records = [record for record in records if record.kind in kinds]
        return records if limit is None else records[:limit]

    async def count_records(self, collection_id: str) -> int:
        return sum(1 for record in self.records if record.collection_id == collection_id)


# ----------------------------------------------------------------------
# 任务队列替身
# ----------------------------------------------------------------------


@dataclass
class FakeJobQueue:
    """只实现接口会用到的几个方法（导入与采集任务状态查询）。"""

    jobs: dict[UUID, JobRecord] = field(default_factory=dict)
    enqueued: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def enqueue(self, kind: str, payload: dict[str, Any] | None = None) -> UUID:
        job_id = uuid4()
        self.enqueued.append((kind, dict(payload or {})))
        self.jobs[job_id] = JobRecord(
            id=job_id,
            kind=kind,
            payload=dict(payload or {}),
            status=JobStatus.QUEUED,
            attempts=0,
            max_attempts=3,
            last_error=None,
            created_at=_WHEN,
            updated_at=_WHEN,
        )
        return job_id

    async def get(self, job_id: UUID) -> JobRecord | None:
        return self.jobs.get(job_id)

    async def record_result(self, job_id: UUID, result: dict[str, Any]) -> None:
        current = self.jobs.get(job_id)
        if current is None:
            return
        self.jobs[job_id] = JobRecord(
            id=current.id,
            kind=current.kind,
            payload={**current.payload, "result": dict(result)},
            status=current.status,
            attempts=current.attempts,
            max_attempts=current.max_attempts,
            last_error=current.last_error,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )

    async def list_by_kind(
        self,
        kind: str,
        *,
        target_id: str | None = None,
        status: JobStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[JobRecord], int]:
        """与 ``JobQueue.list_by_kind`` 同语义：按创建时间倒序、先筛选再分页。"""
        records = [record for record in self.jobs.values() if record.kind == kind]
        if target_id is not None:
            records = [
                record for record in records if (record.payload or {}).get("target_id") == target_id
            ]
        if status is not None:
            records = [record for record in records if record.status == status]
        records.sort(key=lambda record: (record.created_at, str(record.id)), reverse=True)
        return records[offset : offset + limit], len(records)

    async def request_cancel(self, job_id: UUID, *, kind: str) -> JobRecord | None:
        """与 ``JobQueue.request_cancel`` 同语义：只对排队中、执行中的任务置取消标记。"""
        current = self.jobs.get(job_id)
        if current is None or current.kind != kind:
            return None
        if current.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return None
        updated = replace(
            current,
            payload={**(current.payload or {}), "cancel_requested": True},
            updated_at=_WHEN,
        )
        self.jobs[job_id] = updated
        return updated


def make_app(
    *,
    store: FakeStore | None = None,
    queue: FakeJobQueue | None = None,
    plugins: Any = None,
    settings: Any = None,
) -> Any:
    """构造一个把所有外部依赖换成替身的应用。

    ``httpx.ASGITransport`` 默认在应用抛异常时把异常再抛给调用方，那样看不到 500 响应；
    测试客户端统一用 ``raise_app_exceptions=False``，让「内部错误的响应长什么样」可断言。
    """
    from system_analyzer.api.app import create_app
    from system_analyzer.api.deps import (
        app_settings,
        get_job_queue,
        get_plugins,
        get_store,
        get_timeline,
    )
    from system_analyzer.timeline import TimelineEngine

    resolved_store = FakeStore() if store is None else store
    resolved_queue = FakeJobQueue() if queue is None else queue
    app = create_app()
    app.dependency_overrides[get_store] = lambda: resolved_store
    app.dependency_overrides[get_timeline] = lambda: TimelineEngine(resolved_store)
    app.dependency_overrides[get_job_queue] = lambda: resolved_queue
    registered = app.state.plugins if plugins is None else plugins
    app.dependency_overrides[get_plugins] = lambda: registered
    if settings is not None:
        app.dependency_overrides[app_settings] = lambda: settings
    return app


def make_client(app: Any) -> Any:
    """测试客户端：读响应，不把应用抛出的异常再抛出来。"""
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )
