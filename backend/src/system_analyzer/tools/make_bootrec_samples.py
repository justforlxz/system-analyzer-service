"""生成 ``docs/samples/bootrec-v1`` 下的三份最小 ``.bootrec`` 样例。

用法（在 ``backend`` 目录下）：``uv run python -m system_analyzer.tools.make_bootrec_samples``，
写文件到仓库的 ``docs/samples/bootrec-v1/``；加 ``--check`` 只比对不写入，仓库中的样例与
生成结果不一致时以非 0 退出（单元测试用的就是这个模式）。

样例内容固定：时间戳、校验和、ZIP 里的时间戳都由常量与内容算出，重复运行得到完全相同的字节，
所以它们可以进仓库、也可以在评审时逐字节比对。
"""

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from system_analyzer.domain.bootrec import compute_record_id, count_jsonl_lines, sha256_hex

REPO_ROOT = Path(__file__).resolve().parents[4]
SAMPLE_DIR = REPO_ROOT / "docs" / "samples" / "bootrec-v1"

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
KERNEL_VERSION = "6.6.0-1-amd64"
OS_RELEASE = "UOS Desktop 25 Professional"

# 样例里的「现在」固定成同一个时刻，重复生成才能得到相同的字节
REALTIME_BASE_NS = 1_794_009_600_000_000_000

PERF_ARTIFACT = b"FAKE PERF DATA for sample only\n"
PERF_ARTIFACT_PATH = "artifacts/perf.data"

PRODUCER = {"name": "system-analyzer-service", "version": "0.2.0"}


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")


def _record(
    kind: str,
    *,
    collection_id: str,
    source_plugin: str,
    stream_id: str,
    monotonic_ns: int | None,
    realtime_offset_ms: int | None,
    entity: str | None,
    payload: dict[str, Any],
    clock_domain: str = "boot_monotonic",
) -> dict[str, Any]:
    """按规范组装一条记录，``record_id`` 由内容算出，不由调用方指定。"""
    record: dict[str, Any] = {
        "kind": kind,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": collection_id,
        "source_plugin": source_plugin,
        "stream_id": stream_id,
        "monotonic_ns": monotonic_ns,
        "realtime_ns": None
        if realtime_offset_ms is None
        else REALTIME_BASE_NS + realtime_offset_ms * 1_000_000,
        "clock_domain": clock_domain,
        "entity": entity,
        "payload": payload,
    }
    record["record_id"] = compute_record_id(record)
    return record


def _machine_entity() -> dict[str, Any]:
    return {
        "entity_type": "machine",
        "machine_id": MACHINE_ID,
        "hostname": "uos-PC",
        "os_release": OS_RELEASE,
        "kernel_version": KERNEL_VERSION,
        "architecture": "x86_64",
    }


def _boot_entity() -> dict[str, Any]:
    return {
        "entity_type": "boot",
        "boot_id": BOOT_ID,
        "machine_id": MACHINE_ID,
        "started_realtime_ns": REALTIME_BASE_NS,
        "kernel_version": KERNEL_VERSION,
        "boot_index": 7,
    }


def _service_entity(unit: str) -> dict[str, Any]:
    return {
        "entity_type": "service",
        "entity_id": f"service:{unit}",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "name": unit,
        "attributes": {"unit": unit},
    }


def _stream_entity(name: str, unit: str, reason: str | None = None) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "entity_type": "metric_stream",
        "entity_id": f"metric:{name}",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "name": name,
        "unit": unit,
        "available": reason is None,
    }
    if reason is not None:
        entity["unavailable_reason"] = reason
    return entity


def _metric(
    collection_id: str,
    name: str,
    unit: str,
    value: float,
    second: int,
    source_plugin: str = "resource-sampler",
) -> dict[str, Any]:
    return _record(
        "metric",
        collection_id=collection_id,
        source_plugin=source_plugin,
        stream_id=name.split(".")[0],
        monotonic_ns=second * 1_000_000_000,
        realtime_offset_ms=second * 1000,
        entity=f"metric:{name}",
        payload={"name": name, "unit": unit, "value": value},
    )


def _manifest(
    *,
    collection_id: str,
    mode: str,
    resources_available: bool,
    resources_reason: str | None,
    journal_included: bool,
    entities: bytes,
    records: bytes,
    journal: bytes,
) -> bytes:
    manifest = {
        "format": "bootrec",
        "format_version": "1.0",
        "collection": {
            "collection_id": collection_id,
            "machine_id": MACHINE_ID,
            "boot_id": BOOT_ID,
            "mode": mode,
            "created_realtime_ns": REALTIME_BASE_NS,
        },
        "producer": PRODUCER,
        "resources": {"available": resources_available, "reason": resources_reason},
        "journal": {"included": journal_included},
        "counts": {
            "entities": count_jsonl_lines(entities),
            "records": count_jsonl_lines(records),
            "journal_entries": count_jsonl_lines(journal),
        },
        "entries": {
            "entities.jsonl": {"sha256": sha256_hex(entities), "size_bytes": len(entities)},
            "records.jsonl": {"sha256": sha256_hex(records), "size_bytes": len(records)},
            "journal.jsonl": {"sha256": sha256_hex(journal), "size_bytes": len(journal)},
        },
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _zip(entries: dict[str, bytes]) -> bytes:
    """固定时间戳与权限位，保证同样的输入得到同样的字节。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def build_full() -> bytes:
    """完整样例：资源指标、Journal、Profile 与 Artifact 都在。"""
    collection_id = "collection:full-0001"
    entities = _jsonl(
        [
            _machine_entity(),
            _boot_entity(),
            _service_entity("nginx.service"),
            _service_entity("postgresql.service"),
            _stream_entity("cpu.total.utilization", "percent"),
            _stream_entity("memory.used", "bytes"),
            _stream_entity("disk.read_bytes_per_second", "bytes_per_second"),
        ]
    )

    started_nginx = _record(
        "event",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=1_200_000_000,
        realtime_offset_ms=1200,
        entity="service:nginx.service",
        payload={
            "event_type": "systemd.unit.started",
            "severity": "info",
            "message": "Started A high performance web server and a reverse proxy server.",
            "attributes": {"unit": "nginx.service", "invocation_id": "b1d0e7f2a1c34f9c"},
        },
    )
    started_postgresql = _record(
        "event",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=900_000_000,
        realtime_offset_ms=900,
        entity="service:postgresql.service",
        payload={
            "event_type": "systemd.unit.started",
            "severity": "info",
            "message": "Started PostgreSQL RDBMS.",
            "attributes": {"unit": "postgresql.service", "invocation_id": "4c8a1f60d2b74e1a"},
        },
    )
    failed_postgresql = _record(
        "event",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=5_000_000_000,
        realtime_offset_ms=5000,
        entity="service:postgresql.service",
        payload={
            "event_type": "systemd.unit.failed",
            "severity": "error",
            "message": "postgresql.service: Main process exited, code=exited, status=1/FAILURE",
            "attributes": {"unit": "postgresql.service", "result": "exit-code"},
        },
    )
    span_nginx = _record(
        "span",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=1_200_000_000,
        realtime_offset_ms=1200,
        entity="service:nginx.service",
        payload={
            "span_type": "service.active",
            "end_monotonic_ns": 8_500_000_000,
            "end_realtime_ns": REALTIME_BASE_NS + 8500 * 1_000_000,
            "state": "active",
            "attributes": {
                "unit": "nginx.service",
                "active_state": "active",
                "sub_state": "running",
            },
        },
    )
    span_postgresql = _record(
        "span",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=900_000_000,
        realtime_offset_ms=900,
        entity="service:postgresql.service",
        payload={
            "span_type": "service.active",
            "end_monotonic_ns": 5_000_000_000,
            "end_realtime_ns": REALTIME_BASE_NS + 5000 * 1_000_000,
            "state": "failed",
            "attributes": {"unit": "postgresql.service", "active_state": "failed"},
        },
    )
    metrics = [
        _metric(collection_id, "cpu.total.utilization", "percent", 12.5, 1),
        _metric(collection_id, "cpu.total.utilization", "percent", 30.25, 2),
        _metric(collection_id, "cpu.total.utilization", "percent", 8.0, 3),
        _metric(collection_id, "memory.used", "bytes", 4_200_000_000.0, 1),
        _metric(collection_id, "memory.used", "bytes", 4_400_000_000.0, 2),
        _metric(collection_id, "memory.used", "bytes", 4_100_000_000.0, 3),
        _metric(collection_id, "disk.read_bytes_per_second", "bytes_per_second", 524_288.0, 1),
        _metric(collection_id, "disk.read_bytes_per_second", "bytes_per_second", 1_048_576.0, 2),
        _metric(collection_id, "disk.read_bytes_per_second", "bytes_per_second", 0.0, 3),
    ]
    artifact = _record(
        "artifact",
        collection_id=collection_id,
        source_plugin="perf-collector",
        stream_id="perf",
        monotonic_ns=1_000_000_000,
        realtime_offset_ms=1000,
        entity=None,
        payload={
            "path": PERF_ARTIFACT_PATH,
            "media_type": "application/octet-stream",
            "size_bytes": len(PERF_ARTIFACT),
            "sha256": sha256_hex(PERF_ARTIFACT),
            "description": "perf record 原始采样文件（样例里是占位内容）",
        },
    )
    profile = _record(
        "profile",
        collection_id=collection_id,
        source_plugin="perf-collector",
        stream_id="perf",
        monotonic_ns=1_000_000_000,
        realtime_offset_ms=1000,
        entity="metric:cpu.total.utilization",
        payload={
            "profile_type": "perf.cpu",
            "artifact_path": PERF_ARTIFACT_PATH,
            "sample_count": 15234,
            "attributes": {"tool": "perf", "event": "cycles"},
        },
    )
    annotation = _record(
        "annotation",
        collection_id=collection_id,
        source_plugin="systemd-lifecycle-analyzer",
        stream_id="analysis.systemd",
        monotonic_ns=900_000_000,
        realtime_offset_ms=900,
        entity="service:postgresql.service",
        payload={
            "annotation_type": "systemd.service_failed",
            "analyzer": "systemd-lifecycle",
            "severity": "error",
            "message": "postgresql.service 启动 4.1 秒后失败退出，退出码 1。",
            "confidence": 0.9,
            "evidence_refs": [started_postgresql["record_id"], failed_postgresql["record_id"]],
        },
    )
    records = _jsonl(
        [
            started_nginx,
            started_postgresql,
            failed_postgresql,
            span_nginx,
            span_postgresql,
            *metrics,
            artifact,
            profile,
            annotation,
        ]
    )
    journal = _jsonl(
        [
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 1200 * 1_000_000,
                "monotonic_ns": 1_200_000_000,
                "priority": 6,
                "message": "Started A high performance web server and a reverse proxy server.",
                "cursor": "s=1b2c3d4e5f6; i=1",
                "unit": "nginx.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "nginx.service"},
            },
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 5000 * 1_000_000,
                "monotonic_ns": 5_000_000_000,
                "priority": 3,
                "message": "postgresql.service: Main process exited, code=exited, status=1/FAILURE",
                "cursor": "s=1b2c3d4e5f6; i=2",
                "unit": "postgresql.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "postgresql.service", "_PID": "1284"},
            },
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 5200 * 1_000_000,
                "monotonic_ns": 5_200_000_000,
                "priority": 3,
                "message": "Failed to start PostgreSQL RDBMS.",
                "cursor": "s=1b2c3d4e5f6; i=3",
                "unit": "postgresql.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "postgresql.service"},
            },
        ]
    )
    manifest = _manifest(
        collection_id=collection_id,
        mode="ssh_collection",
        resources_available=True,
        resources_reason=None,
        journal_included=True,
        entities=entities,
        records=records,
        journal=journal,
    )
    return _zip(
        {
            "manifest.json": manifest,
            "entities.jsonl": entities,
            "records.jsonl": records,
            "journal.jsonl": journal,
            PERF_ARTIFACT_PATH: PERF_ARTIFACT,
        }
    )


def build_journal_only() -> bytes:
    """Journal-only 样例：只有从日志推导出来的事件，没有任何资源指标。"""
    collection_id = "collection:journal-only-0001"
    entities = _jsonl(
        [
            _machine_entity(),
            _boot_entity(),
            _service_entity("nginx.service"),
            _service_entity("postgresql.service"),
        ]
    )
    unit_started = _record(
        "event",
        collection_id=collection_id,
        source_plugin="journal-importer",
        stream_id="systemd.units",
        monotonic_ns=1_200_000_000,
        realtime_offset_ms=1200,
        entity="service:nginx.service",
        payload={
            "event_type": "systemd.unit.started",
            "severity": "info",
            "message": "Started A high performance web server and a reverse proxy server.",
            "attributes": {"unit": "nginx.service", "derived_from": "journal"},
        },
    )
    unit_failed = _record(
        "event",
        collection_id=collection_id,
        source_plugin="journal-importer",
        stream_id="systemd.units",
        monotonic_ns=5_000_000_000,
        realtime_offset_ms=5000,
        entity="service:postgresql.service",
        payload={
            "event_type": "systemd.unit.failed",
            "severity": "error",
            "message": "postgresql.service: Main process exited, code=exited, status=1/FAILURE",
            "attributes": {"unit": "postgresql.service", "derived_from": "journal"},
        },
    )
    records = _jsonl([unit_started, unit_failed])
    journal = _jsonl(
        [
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 1200 * 1_000_000,
                "monotonic_ns": 1_200_000_000,
                "priority": 6,
                "message": "Started A high performance web server and a reverse proxy server.",
                "cursor": "s=9f8e7d6c5b4; i=1",
                "unit": "nginx.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "nginx.service"},
            },
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 5000 * 1_000_000,
                "monotonic_ns": 5_000_000_000,
                "priority": 3,
                "message": "postgresql.service: Main process exited, code=exited, status=1/FAILURE",
                "cursor": "s=9f8e7d6c5b4; i=2",
                "unit": "postgresql.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "postgresql.service"},
            },
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 5200 * 1_000_000,
                "monotonic_ns": 5_200_000_000,
                "priority": 3,
                "message": "Failed to start PostgreSQL RDBMS.",
                "cursor": "s=9f8e7d6c5b4; i=3",
                "unit": "postgresql.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "postgresql.service"},
            },
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 6000 * 1_000_000,
                "monotonic_ns": 6_000_000_000,
                "priority": 2,
                "message": "Out of memory: Killed process 2048 (postgres) total-vm:1G",
                "cursor": "s=9f8e7d6c5b4; i=4",
                "unit": None,
                "identifier": "kernel",
                "fields": {"_TRANSPORT": "kernel", "_PID": "0"},
            },
        ]
    )
    manifest = _manifest(
        collection_id=collection_id,
        mode="journal_import",
        resources_available=False,
        resources_reason="journal_import：压缩包只含 Journal，没有采集 CPU、内存、磁盘 I/O",
        journal_included=True,
        entities=entities,
        records=records,
        journal=journal,
    )
    return _zip(
        {
            "manifest.json": manifest,
            "entities.jsonl": entities,
            "records.jsonl": records,
            "journal.jsonl": journal,
        }
    )


def build_resources_missing() -> bytes:
    """带资源缺失的样例：CPU 有数据，内存与磁盘两条流标记为不可用且不写 0。"""
    collection_id = "collection:resources-missing-0001"
    entities = _jsonl(
        [
            _machine_entity(),
            _boot_entity(),
            _service_entity("nginx.service"),
            _stream_entity("cpu.total.utilization", "percent"),
            _stream_entity(
                "memory.used",
                "bytes",
                reason="/proc/meminfo 在采集窗口内读取失败，本流没有数据",
            ),
            _stream_entity(
                "disk.read_bytes_per_second",
                "bytes_per_second",
                reason="目标机器没有 iostat，磁盘 I/O 未采集",
            ),
        ]
    )
    unit_started = _record(
        "event",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=1_200_000_000,
        realtime_offset_ms=1200,
        entity="service:nginx.service",
        payload={
            "event_type": "systemd.unit.started",
            "severity": "info",
            "message": "Started A high performance web server and a reverse proxy server.",
            "attributes": {"unit": "nginx.service"},
        },
    )
    span_nginx = _record(
        "span",
        collection_id=collection_id,
        source_plugin="systemd",
        stream_id="systemd.units",
        monotonic_ns=1_200_000_000,
        realtime_offset_ms=1200,
        entity="service:nginx.service",
        payload={
            "span_type": "service.active",
            "end_monotonic_ns": 4_000_000_000,
            "end_realtime_ns": REALTIME_BASE_NS + 4000 * 1_000_000,
            "state": "active",
        },
    )
    records = _jsonl(
        [
            unit_started,
            span_nginx,
            _metric(collection_id, "cpu.total.utilization", "percent", 9.5, 1),
            _metric(collection_id, "cpu.total.utilization", "percent", 21.75, 2),
            _metric(collection_id, "cpu.total.utilization", "percent", 15.0, 3),
        ]
    )
    journal = _jsonl(
        [
            {
                "boot_id": BOOT_ID,
                "realtime_ns": REALTIME_BASE_NS + 1200 * 1_000_000,
                "monotonic_ns": 1_200_000_000,
                "priority": 6,
                "message": "Started A high performance web server and a reverse proxy server.",
                "cursor": "s=0011; i=1",
                "unit": "nginx.service",
                "identifier": "systemd",
                "fields": {"_SYSTEMD_UNIT": "nginx.service"},
            }
        ]
    )
    manifest = _manifest(
        collection_id=collection_id,
        mode="ssh_collection",
        resources_available=True,
        resources_reason=None,
        journal_included=True,
        entities=entities,
        records=records,
        journal=journal,
    )
    return _zip(
        {
            "manifest.json": manifest,
            "entities.jsonl": entities,
            "records.jsonl": records,
            "journal.jsonl": journal,
        }
    )


BUILDERS = {
    "full.bootrec": build_full,
    "journal-only.bootrec": build_journal_only,
    "resources-missing.bootrec": build_resources_missing,
}


def build_samples() -> dict[str, bytes]:
    """三份样例的文件名与字节内容。"""
    return {name: builder() for name, builder in BUILDERS.items()}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check_only = "--check" in args
    unknown = [arg for arg in args if arg != "--check"]
    if unknown:
        print(f"未知参数: {' '.join(unknown)}", file=sys.stderr)
        return 2

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name, data in build_samples().items():
        path = SAMPLE_DIR / name
        if check_only:
            if not path.is_file() or path.read_bytes() != data:
                stale.append(name)
            continue
        path.write_bytes(data)
        print(f"已写入 {path.relative_to(REPO_ROOT)}（{len(data)} 字节）")

    if stale:
        print(
            f"以下样例与生成结果不一致，重新执行不带 --check 的命令：{', '.join(stale)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
