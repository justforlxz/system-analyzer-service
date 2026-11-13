"""把一次远程采集的结果打包成 ``.bootrec`` v1 容器（任务 3.7）。

本模块只做数据整理与容器写入，不做网络传输、不调用导入插件：

- 实体来自三处并合并去重：采集编排拿到的 machine / boot、3.4 从 Metric 记录推出的
  ``metric_stream``、3.5 导出的 ``journal-<run>.entities.jsonl`` 或 Journal 导出文件本身。
- 记录保留原样。``record_id`` 是导入幂等键，打包时优先原值；只有当库里重建的字典比原始
  内容多了取值为 null 的键时，才按 ``docs/bootrec-v1.md`` 第 8 节的规则重新拼出与
  ``record_id`` 一致的那一份，不凭空改内容。
- Journal 条目从 ``journalctl -o export`` 的原始字段换算，六个原始字段
  （``_TRANSPORT``、``SYSLOG_IDENTIFIER``、``UNIT``、``USER_UNIT``、``_SYSTEMD_UNIT``、
  ``INVOCATION_ID``）随 ``fields`` 原样写进 ``journal.jsonl``。
- 资源缺失不补 0。整条指标流没有数据时补一条 ``available = false`` 的
  ``metric_stream`` 实体，并写明原因；部分流缺失时只补缺的那几条。
- 数据库记录里的 ``artifact`` 记录如果指向本次容器里不存在的文件，标记为缺失：
  不写进包内，并逐条记进打包结果的 ``warnings``；不伪造占位文件，也不让整次打包失败。

生成的容器直接交给 1.3 的 ``validate_bootrec`` 复核；复核不通过就不返回路径，避免把坏包
送进回传与导入流程。
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from system_analyzer.collectors.journal import journal_entities
from system_analyzer.collectors.resources import (
    CGROUP_CPU,
    CGROUP_DISK_READ,
    CGROUP_DISK_WRITE,
    CGROUP_MEMORY,
    SYSTEM_CPU,
    SYSTEM_DISK_READ,
    SYSTEM_DISK_WRITE,
    SYSTEM_MEMORY,
    UNIT_CPU,
    UNIT_DISK_READ,
    UNIT_DISK_WRITE,
    UNIT_MEMORY,
    metric_stream_entities,
)
from system_analyzer.domain.bootrec import (
    ARTIFACTS_PREFIX,
    ENTITIES_NAME,
    JOURNAL_NAME,
    MANIFEST_NAME,
    RECORDS_NAME,
    BootEntity,
    CollectionRef,
    EntityType,
    GenericEntity,
    MachineEntity,
    MetricStreamEntity,
    RecordKind,
    compute_record_id,
    count_jsonl_lines,
    sha256_hex,
    validate_bootrec,
)
from system_analyzer.domain.time_base import journal_event_position
from system_analyzer.importers.journal_archive import (
    JournalBundle,
    archive_digest,
    build_bundle,
    parse_journal_archive,
)

FORMAT_VERSION = "1.0"
PRODUCER_NAME = "system-analyzer-service"
PRODUCER_VERSION = "1.0.0"
PACKAGER_PLUGIN_ID = "collection.packager"

ARTIFACT_STREAM_ID = "artifacts"
ARTIFACT_MEDIA_TYPE = "application/octet-stream"
JOURNAL_ARTIFACT_PATH = f"{ARTIFACTS_PREFIX}journal.export"
JOURNAL_ARTIFACT_MEDIA_TYPE = "application/x-journal-export"

DEFAULT_PRIORITY = 6
OPTIONAL_RECORD_KEYS = ("entity", "monotonic_ns", "realtime_ns")

SYSTEM_STREAMS: tuple[tuple[str, str], ...] = (
    (SYSTEM_CPU, "percent"),
    (SYSTEM_MEMORY, "bytes"),
    (SYSTEM_DISK_READ, "bytes_per_second"),
    (SYSTEM_DISK_WRITE, "bytes_per_second"),
)
UNIT_STREAMS: tuple[tuple[str, str], ...] = (
    (UNIT_CPU, "percent"),
    (UNIT_MEMORY, "bytes"),
    (UNIT_DISK_READ, "bytes_per_second"),
    (UNIT_DISK_WRITE, "bytes_per_second"),
)
CGROUP_STREAMS: tuple[tuple[str, str], ...] = (
    (CGROUP_CPU, "percent"),
    (CGROUP_MEMORY, "bytes"),
    (CGROUP_DISK_READ, "bytes_per_second"),
    (CGROUP_DISK_WRITE, "bytes_per_second"),
)

MISSING_SYSTEM_REASON = "本次采集没有从目标机器拿到这条系统指标流的数据"
MISSING_DIMENSION_REASON = "这条 Unit 或 cgroup 指标流没有采集到数据"
NO_RESOURCES_REASON = "本次采集没有任何资源指标"


class BootrecPackagingError(RuntimeError):
    """打包失败：输入不足或生成的容器没有通过校验。"""


@dataclass(frozen=True, slots=True)
class PackagingArtifact:
    """要放进 ``artifacts/`` 的原始文件。``path`` 必须以 ``artifacts/`` 开头。"""

    source: Path
    path: str
    media_type: str = ARTIFACT_MEDIA_TYPE
    description: str | None = None


@dataclass(frozen=True, slots=True)
class JournalData:
    """从一份 Journal 导出文件整理出的三样东西。"""

    entries: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    entities: tuple[BaseModel, ...]


@dataclass(frozen=True, slots=True)
class PackagingInput:
    """一次采集要写进 ``.bootrec`` 的全部内容。

    ``machine`` 与 ``boot`` 可以为空：Journal 导出文件本身带 ``_HOSTNAME`` 与 ``_BOOT_ID``，
    Journal-only 采集因此能从导出文件补齐。两者都拿不到时打包直接失败，不猜作用域。
    """

    collection: CollectionRef
    machine: MachineEntity | None = None
    boot: BootEntity | None = None
    entities: tuple[BaseModel, ...] = ()
    records: tuple[Mapping[str, Any], ...] = ()
    journal: JournalData | None = None
    journal_export: Path | None = None
    artifacts: tuple[PackagingArtifact, ...] = ()
    resources_expected: bool = True
    resources_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PackagingResult:
    """打包结果。路径已经通过 ``validate_bootrec`` 复核。"""

    path: Path
    collection_id: str
    machine_id: str
    boot_id: str
    entity_count: int
    record_count: int
    journal_entry_count: int
    resources_available: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


def collect_journal(
    export_path: str | Path,
    *,
    collection: CollectionRef,
    machine_id: str | None = None,
    boot_id: str | None = None,
) -> JournalData:
    """解析一份 Journal 导出文件，返回归一化条目、事件记录与实体。

    解析与字段归一化复用 1.4（``parse_journal_archive`` / ``build_bundle``），记录作用域与
    3.5 的做法一致：只改 machine、boot、collection 与 source plugin，再重算 ``record_id``。
    """
    path = Path(export_path)
    parsed = parse_journal_archive(path)
    bundle = build_bundle(parsed, source_sha256=archive_digest(path))
    resolved_machine = machine_id or bundle.collection.machine_id
    resolved_boot = boot_id or bundle.collection.boot_id
    if not resolved_machine or not resolved_boot:
        raise BootrecPackagingError("Journal 导出文件没有给出 machine 或 boot 标识")
    entries = journal_entries(parsed.entries)
    for entry in entries:
        if entry["boot_id"] != collection.boot_id:
            raise BootrecPackagingError(
                f"Journal 条目属于 {entry['boot_id']}，与本次采集的 {collection.boot_id} 不一致"
            )
    records = _rescale_journal_records(
        bundle,
        collection=collection,
        machine_id=resolved_machine,
        boot_id=resolved_boot,
    )
    entities = journal_entities(bundle, machine_id=resolved_machine, boot_id=resolved_boot)
    return JournalData(entries=entries, records=records, entities=entities)


def journal_entries(raw_entries: Sequence[Mapping[str, str]]) -> tuple[dict[str, Any], ...]:
    """把 ``journalctl -o export`` 的原始字段换算成 ``journal.jsonl`` 的条目。"""
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_entries, start=1):
        try:
            position = journal_event_position(raw)
        except (ValueError, OverflowError) as error:
            raise BootrecPackagingError(
                f"第 {index} 条 Journal 条目无法换算时间：{error}"
            ) from error
        if position.realtime_ns is None:
            raise BootrecPackagingError(
                f"第 {index} 条 Journal 条目没有 __REALTIME_TIMESTAMP；"
                "docs/bootrec-v1.md 第 5 节要求墙钟时间必填"
            )
        rows.append(
            {
                "boot_id": position.boot_id,
                "realtime_ns": position.realtime_ns,
                "monotonic_ns": position.monotonic_ns,
                "priority": _priority(raw, index),
                "message": raw.get("MESSAGE", ""),
                "cursor": raw.get("__CURSOR") or None,
                "unit": (raw.get("_SYSTEMD_UNIT") or "").strip() or None,
                "identifier": (raw.get("SYSLOG_IDENTIFIER") or "").strip() or None,
                "fields": dict(raw),
            }
        )
    rows.sort(key=lambda row: (row["realtime_ns"], row.get("cursor") or ""))
    return tuple(rows)


def build_bootrec(data: PackagingInput, destination: str | Path) -> PackagingResult:
    """把 ``data`` 写成一份合法的 ``.bootrec``，写完立即校验。"""
    collection = _with_created_time(data.collection)
    journal = data.journal or _journal_from_export(data, collection)
    journal_records: Sequence[Mapping[str, Any]] = ()
    if journal is not None and not _records_include_journal_events(data.records):
        # 3.5 已经把 Journal 事件写进存储时，records 里就有这些事件；再并入导出文件解析出的
        # 同一批事件会得到 source_plugin / record_id 不同但内容重复的两份记录。
        journal_records = journal.records
    records = _normalise_records((*data.records, *journal_records), collection)
    entities = _merge_entities(
        data,
        collection=collection,
        journal=journal,
        records=records,
    )
    artifact_records, artifact_files = _artifact_entries(
        data.artifacts,
        collection=collection,
    )
    records, missing_warnings = _drop_missing_artifact_records(records, artifact_files)
    records.extend(artifact_records)
    records = _unique_records(records)
    metric_records = [row for row in records if row.get("kind") == RecordKind.METRIC.value]
    resources_available = bool(metric_records)
    resources_reason = None
    if not resources_available:
        resources_reason = data.resources_reason or NO_RESOURCES_REASON

    entries_bytes = _entities_jsonl(entities)
    records_bytes = _jsonl(records)
    journal_rows = list(journal.entries) if journal else []
    journal_bytes = _jsonl(journal_rows)
    files: dict[str, bytes] = {
        ENTITIES_NAME: entries_bytes,
        RECORDS_NAME: records_bytes,
        JOURNAL_NAME: journal_bytes,
        **artifact_files,
    }
    manifest = {
        "format": "bootrec",
        "format_version": FORMAT_VERSION,
        "collection": collection.model_dump(mode="json"),
        "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
        "resources": {"available": resources_available, "reason": resources_reason},
        "journal": {"included": bool(journal_rows)},
        "counts": {
            "entities": count_jsonl_lines(entries_bytes),
            "records": count_jsonl_lines(records_bytes),
            "journal_entries": count_jsonl_lines(journal_bytes),
        },
        "entries": {
            name: {"sha256": sha256_hex(files[name]), "size_bytes": len(files[name])}
            for name in (ENTITIES_NAME, RECORDS_NAME, JOURNAL_NAME)
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    _write_zip(temporary, manifest_bytes, files)
    temporary.replace(target)

    report = validate_bootrec(target)
    if not report.ok:
        target.unlink(missing_ok=True)
        details = "；".join(issue.format() for issue in report.errors)
        raise BootrecPackagingError(f"生成的 .bootrec 没有通过校验：{details}")
    return PackagingResult(
        path=target,
        collection_id=collection.collection_id,
        machine_id=collection.machine_id,
        boot_id=collection.boot_id,
        entity_count=len(entities),
        record_count=len(records),
        journal_entry_count=len(journal_rows),
        resources_available=resources_available,
        warnings=(*missing_warnings, *(issue.format() for issue in report.warnings)),
    )


def packaging_input_from_journal(
    collection: CollectionRef,
    export_path: str | Path,
    *,
    resources_expected: bool = False,
    artifacts: Sequence[PackagingArtifact] = (),
) -> PackagingInput:
    """Journal-only 场景的便捷构造：实体、记录与条目全部从导出文件得到。"""
    journal = collect_journal(export_path, collection=collection)
    return PackagingInput(
        collection=collection,
        journal=journal,
        journal_export=Path(export_path),
        artifacts=tuple(artifacts),
        resources_expected=resources_expected,
        resources_reason=NO_RESOURCES_REASON,
    )


# ----------------------------------------------------------------------
# 实体合并与不可用指标流
# ----------------------------------------------------------------------


def _merge_entities(
    data: PackagingInput,
    *,
    collection: CollectionRef,
    journal: JournalData | None,
    records: Sequence[Mapping[str, Any]],
) -> list[BaseModel]:
    merged: dict[tuple[str, str], BaseModel] = {}

    def add(entity: BaseModel) -> None:
        key = _entity_key(entity)
        merged.setdefault(key, entity)

    machine = data.machine
    boot = data.boot
    if journal is not None:
        for entity in journal.entities:
            if isinstance(entity, MachineEntity) and machine is None:
                machine = entity
            if isinstance(entity, BootEntity) and boot is None:
                boot = entity
    for entity in data.entities:
        if isinstance(entity, MachineEntity) and machine is None:
            machine = entity
        if isinstance(entity, BootEntity) and boot is None:
            boot = entity
        if not isinstance(entity, (MachineEntity, BootEntity)):
            add(entity)

    if machine is None or boot is None:
        raise BootrecPackagingError(
            "打包需要 machine 与 boot 实体；请从系统信息或 Journal 导出文件提供"
        )
    _check_scope(collection, machine, boot)
    canonical_machine = machine.model_copy(
        update={
            "machine_id": collection.machine_id,
        }
    )
    canonical_boot = boot.model_copy(
        update={
            "machine_id": collection.machine_id,
            "boot_id": collection.boot_id,
        }
    )
    add(canonical_machine)
    add(canonical_boot)

    if journal is not None:
        for entity in journal.entities:
            if isinstance(entity, GenericEntity) and entity.entity_type == EntityType.SERVICE:
                add(
                    entity.model_copy(
                        update={
                            "machine_id": collection.machine_id,
                            "boot_id": collection.boot_id,
                        }
                    )
                )
    for entity in metric_stream_entities(records):
        add(entity)
    for entity in _unavailable_streams(records, collection, enabled=data.resources_expected):
        add(entity)
    return sorted(merged.values(), key=_entity_sort_key)


def _unavailable_streams(
    records: Sequence[Mapping[str, Any]],
    collection: CollectionRef,
    *,
    enabled: bool,
) -> list[MetricStreamEntity]:
    """按已出现的记录补 ``available = false`` 的指标流实体。

    Unit 与 cgroup 维度只能从一条已有记录反推出「这次采集观察过这个对象」；如果某个 Unit
    或 cgroup 的四条流一条都没有出现，记录里就没有它的标识，无法知道它本该存在，因此不补。
    系统级四条流是固定清单，任何一次 SSH 采集都按清单补齐。
    """
    if not enabled:
        return []
    observed: set[str] = set()
    dimension_targets: dict[str, set[str]] = {"unit": set(), "cgroup": set()}
    for record in records:
        if record.get("kind") != RecordKind.METRIC.value:
            continue
        entity_id = record.get("entity")
        payload = record.get("payload")
        if not isinstance(entity_id, str) or not isinstance(payload, Mapping):
            continue
        name = str(payload.get("name", ""))
        observed.add(entity_id)
        for dimension, streams in (("unit", UNIT_STREAMS), ("cgroup", CGROUP_STREAMS)):
            if any(name == stream for stream, _ in streams):
                prefix = f"metric:{name}:"
                if entity_id.startswith(prefix):
                    dimension_targets[dimension].add(entity_id[len(prefix) :])

    missing: list[MetricStreamEntity] = []
    for name, unit in SYSTEM_STREAMS:
        entity_id = f"metric:{name}"
        if entity_id not in observed:
            missing.append(
                _missing_stream(
                    collection,
                    entity_id=entity_id,
                    name=name,
                    unit=unit,
                    reason=MISSING_SYSTEM_REASON,
                )
            )
    for dimension, streams in (("unit", UNIT_STREAMS), ("cgroup", CGROUP_STREAMS)):
        for target in sorted(dimension_targets[dimension]):
            for name, unit in streams:
                entity_id = f"metric:{name}:{target}"
                if entity_id not in observed:
                    missing.append(
                        _missing_stream(
                            collection,
                            entity_id=entity_id,
                            name=name,
                            unit=unit,
                            reason=MISSING_DIMENSION_REASON,
                        )
                    )
    return missing


def _missing_stream(
    collection: CollectionRef,
    *,
    entity_id: str,
    name: str,
    unit: str,
    reason: str,
) -> MetricStreamEntity:
    return MetricStreamEntity(
        entity_type=EntityType.METRIC_STREAM,
        entity_id=entity_id,
        machine_id=collection.machine_id,
        boot_id=collection.boot_id,
        name=name,
        unit=unit,
        available=False,
        unavailable_reason=reason,
    )


# ----------------------------------------------------------------------
# Journal、记录与文件
# ----------------------------------------------------------------------


def _journal_from_export(data: PackagingInput, collection: CollectionRef) -> JournalData | None:
    if data.journal_export is None:
        if data.journal is not None:
            return data.journal
        return None
    machine_id = data.machine.machine_id if data.machine else collection.machine_id
    boot_id = data.boot.boot_id if data.boot else collection.boot_id
    return collect_journal(
        data.journal_export,
        collection=collection,
        machine_id=machine_id,
        boot_id=boot_id,
    )


def _records_include_journal_events(records: Sequence[Mapping[str, Any]]) -> bool:
    """判断记录里是否已经有 Journal 事件（带原始字段的 event）。"""
    for record in records:
        if record.get("kind") != RecordKind.EVENT.value:
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        attributes = payload.get("attributes")
        if isinstance(attributes, Mapping) and isinstance(attributes.get("fields"), Mapping):
            return True
    return False


def _rescale_journal_records(
    bundle: JournalBundle,
    *,
    collection: CollectionRef,
    machine_id: str,
    boot_id: str,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    for record in bundle.records:
        body = {key: value for key, value in record.items() if key != "record_id"}
        body["machine_id"] = machine_id
        body["boot_id"] = boot_id
        body["collection_id"] = collection.collection_id
        body["source_plugin"] = PACKAGER_PLUGIN_ID
        body["record_id"] = compute_record_id(body)
        records.append(body)
    return tuple(records)


def _normalise_records(
    records: Sequence[Mapping[str, Any]],
    collection: CollectionRef,
) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for raw in records:
        record = dict(raw)
        record_id = record.get("record_id")
        if not isinstance(record_id, str):
            raise BootrecPackagingError("记录缺少 record_id，无法作为导入幂等键")
        body = {key: value for key, value in record.items() if key != "record_id"}
        body["machine_id"] = collection.machine_id
        body["boot_id"] = collection.boot_id
        body["collection_id"] = collection.collection_id
        matched = _matching_content(body, record_id)
        if matched is None:
            raise BootrecPackagingError(
                f"record_id={record_id} 与记录内容对不上；拒绝打包，避免导入时才发现"
            )
        normalised.append({"record_id": record_id, **matched})
    return normalised


def _matching_content(body: dict[str, Any], record_id: str) -> dict[str, Any] | None:
    if compute_record_id(body) == record_id:
        return body
    optional = [key for key in OPTIONAL_RECORD_KEYS if body.get(key, "") is None]
    for count in range(len(optional) + 1):
        for omitted in _combinations(optional, count):
            candidate = {key: value for key, value in body.items() if key not in omitted}
            if compute_record_id(candidate) == record_id:
                return candidate
    return None


def _combinations(values: list[str], count: int):
    if count == 0:
        yield ()
        return
    for index, value in enumerate(values):
        for rest in _combinations(values[index + 1 :], count - 1):
            yield (value, *rest)


def _unique_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        unique[record["record_id"]] = record
    return sorted(unique.values(), key=lambda row: (str(row.get("kind", "")), row["record_id"]))


def _artifact_entries(
    artifacts: Sequence[PackagingArtifact],
    *,
    collection: CollectionRef,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    files: dict[str, bytes] = {}
    records: list[dict[str, Any]] = []
    realtime_ns = collection.created_realtime_ns or time.time_ns()
    for artifact in artifacts:
        container_path = PurePosixPath(artifact.path)
        if (
            container_path.is_absolute()
            or ".." in container_path.parts
            or not artifact.path.startswith(ARTIFACTS_PREFIX)
        ):
            raise BootrecPackagingError(
                f"原始文件路径必须是 {ARTIFACTS_PREFIX} 下的相对路径，实际是 {artifact.path}"
            )
        if artifact.path in files:
            raise BootrecPackagingError(f"原始文件路径重复：{artifact.path}")
        try:
            payload = Path(artifact.source).read_bytes()
        except OSError as error:
            raise BootrecPackagingError(f"读不到原始文件 {artifact.source}：{error}") from error
        files[artifact.path] = payload
        body: dict[str, Any] = {
            "kind": RecordKind.ARTIFACT.value,
            "machine_id": collection.machine_id,
            "boot_id": collection.boot_id,
            "collection_id": collection.collection_id,
            "source_plugin": PACKAGER_PLUGIN_ID,
            "stream_id": ARTIFACT_STREAM_ID,
            "monotonic_ns": None,
            "realtime_ns": realtime_ns,
            "clock_domain": "wall_realtime",
            "entity": None,
            "payload": {
                "path": artifact.path,
                "media_type": artifact.media_type,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        }
        if artifact.description:
            body["payload"]["description"] = artifact.description
        body["record_id"] = compute_record_id(body)
        records.append(body)
    return records, files


MISSING_ARTIFACT_WARNING = (
    "记录 {record_id} 引用的原始文件 {path} 不在本次容器里：该 artifact 记录标记为缺失，不写入包内"
)


def _drop_missing_artifact_records(
    records: Sequence[Mapping[str, Any]],
    artifact_files: Mapping[str, bytes],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """原始文件不在容器里时，去掉对应的 artifact 记录并留下明确警告。

    从数据库记录重新打包时，上一次导入写入的 artifact 记录还会在库里，但原始文件只存在于
    上一次打包的临时目录。`.bootrec` 不允许 artifact 记录指向不存在的文件
    （`E_ARTIFACT_MISSING`），又不能用占位文件伪造原始数据，所以这里把这类记录标记为缺失：
    不写进包内，并在打包结果的警告里逐条写明记录与路径，不让整次打包失败。
    """
    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    for record in records:
        path = _artifact_path(record)
        if path is None or path in artifact_files:
            kept.append(record)
            continue
        warnings.append(
            MISSING_ARTIFACT_WARNING.format(
                record_id=record.get("record_id", "<未知>"),
                path=path,
            )
        )
    return kept, tuple(warnings)


def _artifact_path(record: Mapping[str, Any]) -> str | None:
    """artifact 记录引用的容器内路径；不是 artifact 记录时返回 ``None``。"""
    if record.get("kind") != RecordKind.ARTIFACT.value:
        return None
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    path = payload.get("path")
    return path if isinstance(path, str) else None


def _entities_jsonl(entities: Sequence[BaseModel]) -> bytes:
    rows = [entity.model_dump(mode="json", exclude_none=True) for entity in entities]
    return _jsonl(rows)


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")


def _write_zip(path: Path, manifest: bytes, files: Mapping[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in ((MANIFEST_NAME, manifest), *files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)


def _with_created_time(collection: CollectionRef) -> CollectionRef:
    if collection.created_realtime_ns is not None:
        return collection
    return collection.model_copy(update={"created_realtime_ns": time.time_ns()})


def _priority(raw: Mapping[str, str], index: int) -> int:
    value = raw.get("PRIORITY")
    if value is None or value == "":
        return DEFAULT_PRIORITY
    try:
        priority = int(value)
    except ValueError as error:
        raise BootrecPackagingError(
            f"第 {index} 条 Journal 条目的 PRIORITY={value!r} 不是整数"
        ) from error
    if not 0 <= priority <= 7:
        raise BootrecPackagingError(f"第 {index} 条 Journal 条目的 PRIORITY={priority} 超出 0-7")
    return priority


def _check_scope(
    collection: CollectionRef,
    machine: MachineEntity,
    boot: BootEntity,
) -> None:
    if machine.machine_id != collection.machine_id:
        raise BootrecPackagingError(
            f"machine {machine.machine_id} 与采集的 {collection.machine_id} 不一致"
        )
    if boot.boot_id != collection.boot_id:
        raise BootrecPackagingError(f"boot {boot.boot_id} 与采集的 {collection.boot_id} 不一致")
    if boot.machine_id != collection.machine_id:
        raise BootrecPackagingError(
            f"boot {boot.boot_id} 的机器 {boot.machine_id} 与采集的 {collection.machine_id} 不一致"
        )


def _entity_key(entity: BaseModel) -> tuple[str, str]:
    if isinstance(entity, MachineEntity):
        return (EntityType.MACHINE.value, entity.machine_id)
    if isinstance(entity, BootEntity):
        return (EntityType.BOOT.value, entity.boot_id)
    return (str(entity.entity_type), str(entity.entity_id))


def _entity_sort_key(entity: BaseModel) -> tuple[str, str]:
    if isinstance(entity, MachineEntity):
        return ("0", entity.machine_id)
    if isinstance(entity, BootEntity):
        return ("1", entity.boot_id)
    return (f"2:{entity.entity_type}", str(entity.entity_id))


__all__ = [
    "BootrecPackagingError",
    "JournalData",
    "PackagingArtifact",
    "PackagingInput",
    "PackagingResult",
    "build_bootrec",
    "collect_journal",
    "journal_entries",
    "packaging_input_from_journal",
]
