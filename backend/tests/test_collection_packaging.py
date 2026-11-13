"""采集结果打包成 ``.bootrec`` 的单元测试（任务 3.7）。

重点不是「能写一个 ZIP」，而是三件事：

- 打包 → 解包 → 1.3 导入之后，Journal 的六个原始字段仍在，内核 OOM 仍能被 2.2 识别；
- 3.5 的 service 实体与 3.4 的 metric_stream 实体合并不冲突；
- 只有资源采集、没有 Journal 时也能打包；整条指标流缺失时补 ``available = false``。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from system_analyzer.analyzers.oom import detect_kill
from system_analyzer.collection.packaging import (
    BootrecPackagingError,
    PackagingArtifact,
    PackagingInput,
    build_bootrec,
    collect_journal,
    packaging_input_from_journal,
)
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    Record,
    compute_record_id,
    validate_bootrec,
)
from system_analyzer.importers.bootrec_importer import import_bootrec
from system_analyzer.storage import ImportStatus
from system_analyzer.storage.store import ImportOutcome

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
COLLECTION_ID = "collection:test-0001"
CREATED_NS = 1_794_009_600_000_000_000

SIX_FIELDS = (
    "_TRANSPORT",
    "SYSLOG_IDENTIFIER",
    "UNIT",
    "USER_UNIT",
    "_SYSTEMD_UNIT",
    "INVOCATION_ID",
)

OOM_EXPORT = """\
__CURSOR=s=abc;i=1
__REALTIME_TIMESTAMP=1794009601200000
__MONOTONIC_TIMESTAMP=1200000
_BOOT_ID=3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11
_HOSTNAME=uos-PC
_KERNEL_VERSION=6.6.0-1-amd64
_TRANSPORT=kernel
SYSLOG_IDENTIFIER=kernel
PRIORITY=3
_SYSTEMD_UNIT=firefox.service
UNIT=firefox.service
USER_UNIT=app-firefox.service
INVOCATION_ID=11112222333344445555666677778888
_PID=4310
MESSAGE=Out of memory: Killed process 4310 (firefox) total-vm:2846920kB, anon-rss:1804808kB

__CURSOR=s=abc;i=2
__REALTIME_TIMESTAMP=1794009601300000
__MONOTONIC_TIMESTAMP=1300000
_BOOT_ID=3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11
_HOSTNAME=uos-PC
_KERNEL_VERSION=6.6.0-1-amd64
_TRANSPORT=journal
SYSLOG_IDENTIFIER=systemd
PRIORITY=3
_SYSTEMD_UNIT=firefox.service
MESSAGE=firefox.service: Failed with result 'oom-kill'.

"""


class RecordingAccess:
    """满足 1.3 的 ``CollectionImportAccess``，把写库内容留在内存里供断言。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.last_outcome: ImportOutcome | None = None

    def get_collection(self, collection_id: str) -> None:
        return None

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Any,
        records: Any,
        *,
        source_sha256: str | None = None,
        resources: Any = None,
    ) -> ImportOutcome:
        self.records = [dict(record) for record in records]
        self.last_outcome = ImportOutcome(
            collection_id=collection.collection_id,
            status=ImportStatus.IMPORTED,
            records_written=len(self.records),
        )
        return self.last_outcome


def collection() -> CollectionRef:
    return CollectionRef(
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        mode=CollectionMode.SSH_COLLECTION,
        created_realtime_ns=CREATED_NS,
    )


def machine() -> MachineEntity:
    return MachineEntity(
        entity_type="machine",
        machine_id=MACHINE_ID,
        hostname="uos-PC",
        os_release="UOS Desktop 25 Professional",
        kernel_version="6.6.0-1-amd64",
        architecture="x86_64",
    )


def boot() -> BootEntity:
    return BootEntity(
        entity_type="boot",
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        started_realtime_ns=CREATED_NS - 1_000_000_000,
        kernel_version="6.6.0-1-amd64",
    )


def metric_record(name: str, unit: str, value: float, entity: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "metric",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "resource-sampler",
        "stream_id": name.split(".")[0],
        "monotonic_ns": 2_000_000_000,
        "realtime_ns": CREATED_NS,
        "clock_domain": "boot_monotonic",
        "entity": entity,
        "payload": {"name": name, "unit": unit, "value": value},
    }
    return {"record_id": compute_record_id(body), **body}


def read_jsonl(path: Path, name: str) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        return [json.loads(line) for line in archive.read(name).splitlines() if line]


def test_pack_import_keeps_journal_fields_and_kernel_oom(tmp_path: Path) -> None:
    export = tmp_path / "journal.export"
    export.write_text(OOM_EXPORT, encoding="utf-8")
    data = packaging_input_from_journal(
        collection(),
        export,
        resources_expected=False,
        artifacts=(
            PackagingArtifact(
                source=export,
                path="artifacts/journal.export",
                media_type="application/x-journal-export",
            ),
        ),
    )

    result = build_bootrec(data, tmp_path / "journal.bootrec")

    assert result.journal_entry_count == 2
    assert validate_bootrec(result.path).ok
    access = RecordingAccess()
    outcome = import_bootrec(access, result.path)
    assert outcome.records_written == 3  # 两条 Journal 事件 + 一条原始文件索引

    kernel = next(
        record
        for record in access.records
        if record.get("payload", {}).get("attributes", {}).get("fields", {}).get("_TRANSPORT")
        == "kernel"
    )
    fields = kernel["payload"]["attributes"]["fields"]
    expected = {
        "_TRANSPORT": "kernel",
        "SYSLOG_IDENTIFIER": "kernel",
        "UNIT": "firefox.service",
        "USER_UNIT": "app-firefox.service",
        "_SYSTEMD_UNIT": "firefox.service",
        "INVOCATION_ID": "11112222333344445555666677778888",
    }
    for name in SIX_FIELDS:
        assert fields[name] == expected[name]
    kill = detect_kill(Record.model_validate(kernel))
    assert kill is not None
    assert kill.source == "kernel"
    assert kill.pid == 4310
    assert kill.process == "firefox"

    entities = read_jsonl(result.path, "entities.jsonl")
    assert any(entity["entity_type"] == "service" for entity in entities)
    journal_entries = read_jsonl(result.path, "journal.jsonl")
    assert all(entry["boot_id"] == BOOT_ID for entry in journal_entries)
    assert any(entry["fields"]["_TRANSPORT"] == "kernel" for entry in journal_entries)


def test_resources_only_pack_marks_missing_streams_unavailable(tmp_path: Path) -> None:
    records = (
        metric_record("cpu.total.utilization", "percent", 12.5, "metric:cpu.total.utilization"),
        metric_record("memory.used", "bytes", 1024.0, "metric:memory.used"),
    )
    data = PackagingInput(
        collection=collection(),
        machine=machine(),
        boot=boot(),
        records=records,
        resources_expected=True,
    )

    result = build_bootrec(data, tmp_path / "resources.bootrec")

    report = validate_bootrec(result.path)
    assert report.ok
    assert report.resources_available is True
    assert report.journal_entry_count == 0
    streams = {
        entity["entity_id"]: entity
        for entity in read_jsonl(result.path, "entities.jsonl")
        if entity["entity_type"] == "metric_stream"
    }
    assert set(streams) == {
        "metric:cpu.total.utilization",
        "metric:memory.used",
        "metric:disk.read_bytes_per_second",
        "metric:disk.write_bytes_per_second",
    }
    assert streams["metric:cpu.total.utilization"]["available"] is True
    assert streams["metric:disk.write_bytes_per_second"]["available"] is False
    assert "unavailable_reason" in streams["metric:disk.write_bytes_per_second"]
    with zipfile.ZipFile(result.path) as archive:
        assert archive.read("journal.jsonl") == b""


def test_journal_export_does_not_duplicate_records_already_collected(tmp_path: Path) -> None:
    export = tmp_path / "journal.export"
    export.write_text(OOM_EXPORT, encoding="utf-8")
    journal = collect_journal(export, collection=collection())

    result = build_bootrec(
        PackagingInput(
            collection=collection(),
            machine=machine(),
            boot=boot(),
            records=journal.records,
            journal_export=export,
            resources_expected=False,
        ),
        tmp_path / "no-duplicates.bootrec",
    )

    events = [
        record for record in read_jsonl(result.path, "records.jsonl") if record["kind"] == "event"
    ]
    assert len(events) == len(journal.records)
    assert result.journal_entry_count == 2


def test_missing_whole_resource_stream_set_still_packs(tmp_path: Path) -> None:
    data = PackagingInput(
        collection=collection(),
        machine=machine(),
        boot=boot(),
        records=(),
        resources_expected=True,
    )

    result = build_bootrec(data, tmp_path / "empty-resources.bootrec")

    report = validate_bootrec(result.path)
    assert report.ok
    assert report.resources_available is False
    streams = [
        entity
        for entity in read_jsonl(result.path, "entities.jsonl")
        if entity["entity_type"] == "metric_stream"
    ]
    assert len(streams) == 4
    assert all(stream["available"] is False for stream in streams)
    assert all("unavailable_reason" in stream for stream in streams)


def test_artifact_path_rejects_parent_traversal(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")

    with pytest.raises(BootrecPackagingError, match="相对路径"):
        build_bootrec(
            PackagingInput(
                collection=collection(),
                machine=machine(),
                boot=boot(),
                artifacts=(PackagingArtifact(source=source, path="artifacts/../escape.bin"),),
            ),
            tmp_path / "traversal.bootrec",
        )


def artifact_record(path: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "artifact",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "collection.packager",
        "stream_id": "artifacts",
        "monotonic_ns": None,
        "realtime_ns": CREATED_NS,
        "clock_domain": "wall_realtime",
        "entity": None,
        "payload": {
            "path": path,
            "media_type": "application/x-journal-export",
            "size_bytes": 12,
            "sha256": "a" * 64,
        },
    }
    return {"record_id": compute_record_id(body), **body}


def test_artifact_record_without_container_file_is_marked_missing(tmp_path: Path) -> None:
    """从数据库记录重新打包时，原始文件已经不在容器里也必须能打包。"""
    data = PackagingInput(
        collection=collection(),
        machine=machine(),
        boot=boot(),
        records=(artifact_record("artifacts/journal.export"),),
        resources_expected=False,
    )

    result = build_bootrec(data, tmp_path / "missing-artifact.bootrec")

    report = validate_bootrec(result.path)
    assert report.ok, [issue.format() for issue in report.errors]
    records = read_jsonl(result.path, "records.jsonl")
    assert all(record["kind"] != "artifact" for record in records)
    assert any(
        "artifacts/journal.export" in warning and "缺失" in warning for warning in result.warnings
    )
