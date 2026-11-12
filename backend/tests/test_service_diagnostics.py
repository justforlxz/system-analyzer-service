"""服务诊断读侧的行为测试：游标状态、身份信息、证据与资源采样。

用只读入口的替身，不连数据库。真实 PostgreSQL 上的导入 → 分析 → 诊断链路在
``tests/integration/test_analysis_pipeline.py``。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from system_analyzer.diagnostics import (
    DiagnosticsBootNotFoundError,
    DiagnosticsServiceNotFoundError,
    ServiceDiagnosticsEngine,
)
from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    GenericEntity,
    MetricStreamEntity,
    Record,
    RecordKind,
    compute_record_id,
)
from system_analyzer.importers import journal_archive as archive

MACHINE = "machine:uos-PC"
BOOT = "boot-a"
COLLECTION = "collection:a"
NGINX = "service:nginx.service"
FIREFOX = "service:firefox.service"


class FakeReader:
    def __init__(
        self,
        records: Sequence[Record],
        entities: Sequence[Any] = (),
        *,
        boot: bool = True,
    ) -> None:
        self.records = list(records)
        self.entities = list(entities)
        self.boot = boot

    async def get_boot(self, machine_id: str, boot_id: str) -> BootEntity | None:
        if not self.boot:
            return None
        return BootEntity(entity_type="boot", boot_id=boot_id, machine_id=machine_id)

    async def list_records_in_boot(self, machine_id: str, boot_id: str) -> list[Record]:
        return list(self.records)

    async def list_entities(self, machine_id: str, boot_id: str) -> list[Any]:
        return list(self.entities)


def make_record(
    kind: RecordKind,
    payload: dict[str, Any],
    *,
    entity: str | None = None,
    monotonic_ns: int,
    record_id: str | None = None,
) -> Record:
    body = {
        "kind": kind.value,
        "machine_id": MACHINE,
        "boot_id": BOOT,
        "collection_id": COLLECTION,
        "source_plugin": "test.analyzer",
        "stream_id": "test.stream",
        "monotonic_ns": monotonic_ns,
        "realtime_ns": 1_700_000_000_000_000_000 + monotonic_ns,
        "clock_domain": ClockDomain.BOOT_MONOTONIC.value,
        "entity": entity,
        "payload": payload,
    }
    return Record.model_validate({**body, "record_id": record_id or compute_record_id(body)})


def span(
    entity: str,
    *,
    start: int,
    end: int | None,
    span_type: str,
    state: str,
    attributes: dict[str, Any] | None = None,
) -> Record:
    return make_record(
        RecordKind.SPAN,
        {
            "span_type": span_type,
            "end_monotonic_ns": end,
            "state": state,
            "attributes": attributes or {},
        },
        entity=entity,
        monotonic_ns=start,
    )


def event(
    *,
    monotonic_ns: int,
    message: str,
    attributes: dict[str, Any],
    entity: str | None = None,
    record_id: str | None = None,
) -> Record:
    return make_record(
        RecordKind.EVENT,
        {"event_type": "journal.entry", "message": message, "attributes": attributes},
        entity=entity,
        monotonic_ns=monotonic_ns,
        record_id=record_id,
    )


def annotation(
    *,
    monotonic_ns: int,
    annotation_type: str,
    entity: str | None,
    message: str,
    confidence: float | None,
    evidence_refs: list[str],
) -> Record:
    return make_record(
        RecordKind.ANNOTATION,
        {
            "annotation_type": annotation_type,
            "analyzer": "test.analyzer",
            "severity": "error",
            "message": message,
            "confidence": confidence,
            "evidence_refs": evidence_refs,
        },
        entity=entity,
        monotonic_ns=monotonic_ns,
    )


def service_entity(entity_id: str, unit: str) -> GenericEntity:
    return GenericEntity(
        entity_type="service",
        entity_id=entity_id,
        machine_id=MACHINE,
        boot_id=BOOT,
        name=unit,
        attributes={"unit": unit},
    )


async def test_state_uses_cursor_not_the_completed_span_terminal_state() -> None:
    records = [
        span(NGINX, start=1_000, end=2_000, span_type="service.activating", state="active"),
        span(NGINX, start=2_000, end=5_000, span_type="service.active", state="inactive"),
    ]
    engine = ServiceDiagnosticsEngine(FakeReader(records, [service_entity(NGINX, "nginx.service")]))

    during = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=3_000)
    assert during.state.state == "active"
    assert during.state.since_ns == 2_000

    after = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=6_000)
    assert after.state.state == "inactive"
    assert after.state.since_ns == 5_000


async def test_open_activating_span_beyond_data_end_is_stale_and_uncertain() -> None:
    """采集在服务启动过程中结束时，游标之后不能把「启动中」当成确定事实。"""
    records = [
        span(NGINX, start=1_000, end=None, span_type="service.activating", state="active"),
        event(
            monotonic_ns=2_000,
            message="Starting nginx.service...",
            attributes={"unit": "nginx.service"},
        ),
    ]
    engine = ServiceDiagnosticsEngine(FakeReader(records, [service_entity(NGINX, "nginx.service")]))

    during = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=1_500)
    assert during.state.state == "activating"
    assert during.state.stale is False

    after = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=3_000)
    assert after.state.state == "activating"
    assert after.state.stale is True
    assert any(item.field == "state" and "无法确定" in item.note for item in after.uncertain)


async def test_state_is_unknown_before_any_lifecycle_evidence() -> None:
    engine = ServiceDiagnosticsEngine(
        FakeReader(
            [span(NGINX, start=1_000, end=None, span_type="service.active", state="active")],
            [service_entity(NGINX, "nginx.service")],
        )
    )

    result = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=500)

    assert result.state.state == "unknown"
    assert "无法确定" in (result.state.note or "")
    assert "state" in {item.field for item in result.uncertain}


async def test_identity_falls_back_to_journal_and_marks_missing_fields_unknown() -> None:
    records = [
        span(NGINX, start=2_000, end=None, span_type="service.active", state="active"),
        event(
            monotonic_ns=1_500,
            message="Started nginx.service",
            attributes={
                "unit": "nginx.service",
                "pid": 812,
                "invocation_id": "inv-1",
                "fields": {"_SYSTEMD_CGROUP": "/system.slice/nginx.service"},
            },
        ),
        span(FIREFOX, start=2_000, end=None, span_type="service.active", state="active"),
    ]
    engine = ServiceDiagnosticsEngine(
        FakeReader(
            records,
            [
                service_entity(NGINX, "nginx.service"),
                service_entity(FIREFOX, "firefox.service"),
            ],
        )
    )

    nginx = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=3_000)
    assert nginx.identity["pid"].value == "812"
    assert nginx.identity["invocation_id"].value == "inv-1"
    assert nginx.identity["cgroup"].value == "/system.slice/nginx.service"

    firefox = await engine.service_at(MACHINE, BOOT, FIREFOX, at_ns=3_000)
    assert firefox.identity["pid"].value is None
    assert "无法确定" in (firefox.identity["pid"].note or "")
    assert {"pid", "invocation_id", "cgroup"} <= {item.field for item in firefox.uncertain}


async def test_sender_identity_fields_are_not_attributed_to_the_object_unit() -> None:
    """systemd 打印生命周期消息时，_SYSTEMD_CGROUP 与 _SYSTEMD_INVOCATION_ID 属于发送方。"""
    records = [
        span(NGINX, start=1_000, end=None, span_type="service.active", state="active"),
        event(
            monotonic_ns=1_500,
            message="Starting nginx.service...",
            attributes={
                "unit": "init.scope",
                "invocation_id": "systemd-invocation",
                "fields": {
                    "_SYSTEMD_UNIT": "init.scope",
                    "_SYSTEMD_CGROUP": "/init.scope",
                    "_SYSTEMD_INVOCATION_ID": "systemd-invocation",
                    "UNIT": "nginx.service",
                    "INVOCATION_ID": "unit-invocation",
                },
            },
        ),
        make_record(
            RecordKind.METRIC,
            {"name": "cpu.cgroup.utilization", "value": 99.0, "unit": "percent"},
            entity="metric:/init.scope",
            monotonic_ns=1_600,
        ),
    ]
    engine = ServiceDiagnosticsEngine(FakeReader(records, [service_entity(NGINX, "nginx.service")]))

    result = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=3_000)

    # 被操作 Unit 自己写的 INVOCATION_ID 可以采用，发送方的 cgroup 不能。
    assert result.identity["invocation_id"].value == "unit-invocation"
    assert result.identity["cgroup"].value is None
    assert "无法确定" in (result.identity["cgroup"].note or "")
    assert "cgroup" in {item.field for item in result.uncertain}
    # /init.scope 的采样不能因为发送方 cgroup 被当成该服务的资源使用。
    assert result.resources.latest == ()
    assert result.resources.series == ()


REAL_FIXTURE = Path(__file__).parent / "fixtures" / "systemd_lifecycle" / "real-boot.journal.gz"
REAL_DISPATCHER = "NetworkManager-dispatcher.service"


def real_journal_records() -> list[Record]:
    parsed = archive.parse_journal_archive(REAL_FIXTURE)
    bundle = archive.build_bundle(parsed, source_sha256="0" * 64)
    return [Record.model_validate(record) for record in bundle.records]


async def test_real_journal_does_not_use_init_scope_as_service_identity() -> None:
    """真实 Journal 里 PID 1 打印的消息不能把 /init.scope 算到被操作的服务上。"""
    records = real_journal_records()
    starting = [
        record
        for record in records
        if record.monotonic_ns is not None
        and "Starting Network Manager Script Dispatcher Service"
        in (record.payload.get("message") or "")
    ]
    latest = max(starting, key=lambda record: record.monotonic_ns or 0)
    expected_invocation = latest.payload["attributes"]["fields"]["INVOCATION_ID"]
    entity = service_entity(f"service:{REAL_DISPATCHER}", REAL_DISPATCHER)
    engine = ServiceDiagnosticsEngine(FakeReader(records, [entity]))

    result = await engine.service_at(
        MACHINE, BOOT, f"service:{REAL_DISPATCHER}", at_ns=latest.monotonic_ns
    )

    assert result.identity["cgroup"].value is None
    assert result.identity["cgroup"].value != "/init.scope"
    assert result.identity["invocation_id"].value == expected_invocation
    assert "cgroup" in {item.field for item in result.uncertain}


async def test_oom_conclusion_keeps_raw_evidence_and_missing_evidence_is_flagged() -> None:
    kernel = event(
        monotonic_ns=2_400,
        message="Out of memory: Killed process 4310 (firefox)",
        attributes={"identifier": "kernel"},
    )
    failure = event(
        monotonic_ns=2_401,
        message="firefox.service: Failed with result 'oom-kill'.",
        attributes={"unit": "firefox.service"},
    )
    oom = annotation(
        monotonic_ns=2_400,
        annotation_type="oom.kernel.killed_process",
        entity=FIREFOX,
        message="内核 OOM 杀掉了 firefox",
        confidence=0.3,
        evidence_refs=[kernel.record_id, failure.record_id],
    )
    incomplete = annotation(
        monotonic_ns=2_500,
        annotation_type="systemd.service_failed",
        entity=FIREFOX,
        message="证据丢失的结论",
        confidence=None,
        evidence_refs=["missing-record-id"],
    )
    engine = ServiceDiagnosticsEngine(
        FakeReader(
            [kernel, failure, oom, incomplete],
            [service_entity(FIREFOX, "firefox.service")],
        )
    )

    result = await engine.service_at(MACHINE, BOOT, FIREFOX, at_ns=2_600)
    oom_view = next(item for item in result.annotations if item.record_id == oom.record_id)

    assert oom_view.confidence == 0.3
    assert [item.found for item in oom_view.evidence] == [True, True]
    assert "Killed process 4310" in (oom_view.evidence[0].message or "")
    assert {item.record_id for item in result.journal.items} >= {
        kernel.record_id,
        failure.record_id,
    }

    missing = next(item for item in result.annotations if item.record_id == incomplete.record_id)
    assert missing.evidence_missing is True
    assert any(item.field == "evidence" for item in result.uncertain)


async def test_resources_use_samples_at_or_before_the_cursor() -> None:
    records = [
        span(NGINX, start=2_000, end=None, span_type="service.active", state="active"),
        make_record(
            RecordKind.METRIC,
            {"name": "cpu.unit.utilization", "value": 12.5, "unit": "percent"},
            entity="metric:cpu.unit.utilization:nginx.service",
            monotonic_ns=2_500,
        ),
        make_record(
            RecordKind.METRIC,
            {"name": "memory.unit.used", "value": 4096.0, "unit": "bytes"},
            entity="metric:memory.unit.used:nginx.service",
            monotonic_ns=3_500,
        ),
        make_record(
            RecordKind.METRIC,
            {"name": "cpu.unit.utilization", "value": 99.0, "unit": "percent"},
            entity="metric:cpu.unit.utilization:nginx.service",
            monotonic_ns=9_000,
        ),
    ]
    entities = [
        service_entity(NGINX, "nginx.service"),
        MetricStreamEntity(
            entity_type="metric_stream",
            entity_id="metric:disk.unit.read_bytes_per_second:nginx.service",
            machine_id=MACHINE,
            boot_id=BOOT,
            name="disk.unit.read_bytes_per_second",
            unit="bytes_per_second",
            available=False,
            unavailable_reason="/proc 读取失败",
        ),
    ]
    engine = ServiceDiagnosticsEngine(FakeReader(records, entities))

    result = await engine.service_at(MACHINE, BOOT, NGINX, at_ns=4_000)
    latest = {item.name: item.value for item in result.resources.latest}

    assert latest == {"cpu.unit.utilization": 12.5, "memory.unit.used": 4096.0}
    assert [item.reason for item in result.resources.unavailable] == ["/proc 读取失败"]


async def test_unknown_boot_and_service_are_reported() -> None:
    engine = ServiceDiagnosticsEngine(FakeReader([], boot=False))
    with pytest.raises(DiagnosticsBootNotFoundError):
        await engine.services(MACHINE, BOOT)

    engine = ServiceDiagnosticsEngine(FakeReader([]))
    with pytest.raises(DiagnosticsServiceNotFoundError):
        await engine.service_at(MACHINE, BOOT, NGINX, at_ns=1)
