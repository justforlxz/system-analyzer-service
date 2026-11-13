"""采集结束后的打包、回传与导入编排（任务 3.7）。

输入来自两处：采集任务记录里的系统信息（machine / boot）与统一存储里的本次采集记录。
Journal 原始导出文件与 3.5 的实体清单如果已经留在本次尝试的记录里，就一起并进容器。

顺序固定为「打包 → 回传（可选）→ 导入 → 清理远端」：

- 打包和导入都失败时，本地包与远端文件都保留；重试会重新打包并重新下载。
- 远端文件只在 1.3 导入返回成功后删除；导入失败时远端不动。
- 本模块不执行环境恢复。环境恢复由 ``collection.plan`` 的 Post 步骤负责；调用方在回传
  失败时也要确保那一步仍然执行（见 ``plan.gather_collected_data``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.import_access import import_bootrec_file
from system_analyzer.collection.packaging import (
    JOURNAL_ARTIFACT_MEDIA_TYPE,
    JOURNAL_ARTIFACT_PATH,
    BootrecPackagingError,
    JournalData,
    PackagingArtifact,
    PackagingInput,
    build_bootrec,
    collect_journal,
)
from system_analyzer.collection.transfer import (
    DEFAULT_DOWNLOAD_ATTEMPTS,
    TransferAttempt,
    TransferReport,
    pull_with_retry,
    remove_remote_file,
    stage_remote_file,
)
from system_analyzer.collectors.journal import DEFAULT_WORK_ROOT as JOURNAL_WORK_ROOT
from system_analyzer.core.config import get_settings
from system_analyzer.core.db import create_engine as create_storage_engine
from system_analyzer.domain.bootrec import (
    ENTITY_MODELS,
    BootEntity,
    ClockDomain,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    Record,
)
from system_analyzer.remote.errors import RemoteError
from system_analyzer.storage import DiagnosticStore

logger = logging.getLogger(__name__)

DELIVERY_OPTION = "delivery"
JOURNAL_OPTION = "journal"
COLLECTION_ID_OPTION = "collection_id"
MACHINE_ID_OPTION = "machine_id"
BOOT_ID_OPTION = "boot_id"
DATABASE_URL_OPTION = "database_url"
JOURNAL_WORK_DIR_OPTION = "journal_work_dir"

NO_JOURNAL_WARNING = "没有找到 3.5 的 Journal 导出文件，包内 journal.jsonl 为 0 行"


class DeliveryError(RuntimeError):
    """打包、回传或导入没有成功。远端数据在导入成功前不会被删除。"""


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """一次打包回传的完整结果，写进采集记录的 ``ctx.data["delivery"]``。"""

    status: str
    collection_id: str
    pack_path: str | None = None
    local_path: str | None = None
    transfer: Mapping[str, Any] | None = None
    imported: bool = False
    records_written: int = 0
    records_skipped: int = 0
    import_status: str | None = None
    remote_removed: bool | None = None
    error: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "collection_id": self.collection_id,
            "pack_path": self.pack_path,
            "local_path": self.local_path,
            "transfer": dict(self.transfer) if self.transfer is not None else None,
            "imported": self.imported,
            "records_written": self.records_written,
            "records_skipped": self.records_skipped,
            "import_status": self.import_status,
            "remote_removed": self.remote_removed,
            "error": self.error,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class JournalArtifacts:
    """3.5 留在 Worker 本地目录里的 Journal 产物，路径已经解析成绝对路径。"""

    work_dir: Path | None = None
    export: Path | None = None
    entities: Path | None = None

    @property
    def present(self) -> bool:
        return self.export is not None or self.entities is not None

    def as_mapping(self) -> dict[str, str | None]:
        return {
            "work_dir": str(self.work_dir) if self.work_dir is not None else None,
            "export_file": str(self.export) if self.export is not None else None,
            "entities_file": str(self.entities) if self.entities is not None else None,
        }


async def deliver_from_context(ctx: PhaseContext) -> DeliveryReport | None:
    """按上下文里的采集结果打包、回传并导入。

    返回 ``None`` 表示这次尝试没有可打包的数据（例如 In Progress 还没有填真实采集动作）；
    调用方不应把「没有数据」当成成功导入。

    任务 payload 没有 ``options.delivery`` 时直接跳过：不是每次采集任务都要在本版本
    回传打包，采集管理界面或后续接入方通过这个段显式打开本流程。
    """
    if DELIVERY_OPTION not in ctx.options:
        return None
    source = await _load_source(ctx)
    if source is None:
        return None

    destination = ctx.scratch_dir / f"{_safe_name(source.collection.collection_id)}.bootrec"
    try:
        packaged = build_bootrec(source, destination)
    except BootrecPackagingError as error:
        raise DeliveryError(f"打包失败：{error}") from error

    delivery_options = _mapping(ctx.options.get(DELIVERY_OPTION))
    database_url = str(ctx.options.get(DATABASE_URL_OPTION) or get_settings().database_url)
    remote_path = _optional_text(delivery_options.get("remote_path"))
    local_path = packaged.path
    transfer_report: TransferReport | None = None
    if remote_path:
        if bool(delivery_options.get("stage_local_pack", False)):
            async with ctx.connect() as connected:
                await stage_remote_file(connected, packaged.path, remote_path)
        transfer_report = await _pull_with_reconnect(
            ctx,
            remote_path,
            destination,
            attempts=_attempts(delivery_options),
            delay_seconds=float(delivery_options.get("retry_delay_seconds", 0.0)),
        )
        if not transfer_report.succeeded:
            raise DeliveryError(f"回传失败，远端文件保留在 {remote_path}：{transfer_report.error}")
        local_path = transfer_report.local_path
        packaged = replace(packaged, path=local_path)

    try:
        outcome = await asyncio.to_thread(import_bootrec_file, str(local_path), database_url)
    except Exception as error:
        raise DeliveryError(f"导入失败，远端文件未删除：{type(error).__name__}: {error}") from error

    remote_removed: bool | None = None
    if remote_path:
        async with ctx.connect() as cleanup_client:
            remote_removed = await remove_remote_file(
                cleanup_client,
                remote_path,
                imported=True,
            )
    warnings = packaged.warnings
    if source.journal is None:
        warnings = (*warnings, NO_JOURNAL_WARNING)
    return DeliveryReport(
        status="imported",
        collection_id=packaged.collection_id,
        pack_path=str(packaged.path),
        local_path=str(local_path),
        transfer=transfer_report.as_mapping() if transfer_report is not None else None,
        imported=True,
        records_written=outcome.records_written,
        records_skipped=outcome.records_skipped,
        import_status=outcome.status,
        remote_removed=remote_removed,
        warnings=warnings,
    )


async def _pull_with_reconnect(
    ctx: PhaseContext,
    remote_path: str,
    destination: Path,
    *,
    attempts: int,
    delay_seconds: float,
) -> TransferReport:
    """每次下载尝试都新开一条 SSH 连接。

    网络中断会把连接一起弄断，复用同一个客户端时后续尝试只会继续失败。这里按次数重连，
    并且无论哪次失败都不删除远端文件；本地包由 ``pull_with_retry`` 的 ``.part`` 写入保护。
    """
    history: list[TransferAttempt] = []
    last_error: str | None = None
    for number in range(1, attempts + 1):
        try:
            async with ctx.connect() as client:
                report = await pull_with_retry(
                    client,
                    remote_path,
                    destination,
                    attempts=1,
                )
        except (RemoteError, OSError) as error:
            last_error = f"{type(error).__name__}: {error}"
            history.append(TransferAttempt(number=number, succeeded=False, error=last_error))
        else:
            attempt = report.attempts[0]
            history.append(
                TransferAttempt(number=number, succeeded=attempt.succeeded, error=attempt.error)
            )
            if report.succeeded:
                return TransferReport(
                    remote_path=remote_path,
                    local_path=report.local_path,
                    attempts=tuple(history),
                    succeeded=True,
                    remote_retained=True,
                )
            last_error = report.error
        if number < attempts and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
    return TransferReport(
        remote_path=remote_path,
        local_path=destination,
        attempts=tuple(history),
        succeeded=False,
        remote_retained=True,
        error=last_error,
    )


async def _load_source(ctx: PhaseContext) -> PackagingInput | None:
    delivery_options = _mapping(ctx.options.get(DELIVERY_OPTION))
    collection_id = (
        _optional_text(
            ctx.options.get(COLLECTION_ID_OPTION) or delivery_options.get(COLLECTION_ID_OPTION)
        )
        or f"collection:{ctx.run_id}"
    )
    database_url = str(ctx.options.get(DATABASE_URL_OPTION) or get_settings().database_url)
    engine = create_storage_engine(database_url)
    try:
        store = DiagnosticStore(engine)
        state = await store.get_collection(collection_id)
        records: list[dict[str, Any]] = []
        entities: list[BaseModel] = []
        machine: MachineEntity | None = None
        boot: BootEntity | None = None
        if state is not None:
            entities = list(
                await store.list_entities(state.collection.machine_id, state.collection.boot_id)
            )
            for entity in entities:
                if isinstance(entity, MachineEntity):
                    machine = entity
                elif isinstance(entity, BootEntity):
                    boot = entity
            for clock_domain in (ClockDomain.BOOT_MONOTONIC, ClockDomain.WALL_REALTIME):
                for record in await store.list_records_in_collection(
                    collection_id, clock_domain=clock_domain
                ):
                    records.append(_record_mapping(record))
        collection = (
            state.collection if state is not None else _collection_from_context(ctx, collection_id)
        )
        machine = machine or _machine_from_context(ctx, collection)
        boot = boot or _boot_from_context(ctx, collection)
        journal_artifacts = resolve_journal_artifacts(ctx)
        journal_export = journal_artifacts.export
        journal_entities = journal_artifacts.entities
        if journal_entities is not None:
            entities.extend(_read_entities(journal_entities))
        if not records and journal_export is None:
            return None
        journal_payload: JournalData | None = None
        if journal_export is not None:
            # Journal entries and entities always come from the raw export; event records are
            # only taken from it when the store has none. Collector 3.5 already appended the
            # events to the store, so parsing them again would add a second copy under a
            # different source_plugin and a different record_id.
            parsed_journal = collect_journal(
                journal_export,
                collection=collection,
                machine_id=collection.machine_id,
                boot_id=collection.boot_id,
            )
            entities.extend(parsed_journal.entities)
            if not records:
                records.extend(parsed_journal.records)
            journal_payload = JournalData(
                entries=parsed_journal.entries,
                records=(),
                entities=parsed_journal.entities,
            )
        artifacts: list[PackagingArtifact] = []
        if journal_export is not None:
            artifacts.append(
                PackagingArtifact(
                    source=journal_export,
                    path=JOURNAL_ARTIFACT_PATH,
                    media_type=JOURNAL_ARTIFACT_MEDIA_TYPE,
                    description="journalctl -o export 的原始导出",
                )
            )
        artifacts.extend(_extra_artifacts(delivery_options))
        return PackagingInput(
            collection=collection,
            machine=machine,
            boot=boot,
            entities=tuple(entities),
            records=tuple(_unique_record_mappings(records)),
            journal=journal_payload,
            journal_export=None,
            artifacts=tuple(artifacts),
            resources_expected=collection.mode is CollectionMode.SSH_COLLECTION,
        )
    finally:
        await engine.dispose()


def _collection_from_context(ctx: PhaseContext, collection_id: str) -> CollectionRef:
    system = _mapping(ctx.data.get("system_info"))
    machine_id = _optional_text(ctx.options.get(MACHINE_ID_OPTION)) or _optional_text(
        system.get("hostname")
    )
    boot_id = _optional_text(ctx.options.get(BOOT_ID_OPTION)) or _optional_text(
        system.get("boot_id")
    )
    if not machine_id or not boot_id:
        raise DeliveryError(
            "没有可用的 machine_id 与 boot_id：采集记录不存在，系统信息里也没有 hostname / boot_id"
        )
    return CollectionRef(
        collection_id=collection_id,
        machine_id=machine_id if machine_id.startswith("machine:") else f"machine:{machine_id}",
        boot_id=boot_id,
        mode=CollectionMode.SSH_COLLECTION,
        created_realtime_ns=None,
    )


def _machine_from_context(ctx: PhaseContext, collection: CollectionRef) -> MachineEntity | None:
    system = _mapping(ctx.data.get("system_info"))
    hostname = _optional_text(system.get("hostname"))
    if hostname is None:
        return None
    return MachineEntity(
        entity_type="machine",
        machine_id=collection.machine_id,
        hostname=hostname,
        os_release=_optional_text(system.get("os_release")),
        kernel_version=_optional_text(system.get("kernel_version")),
        architecture=_optional_text(system.get("architecture")),
    )


def _boot_from_context(ctx: PhaseContext, collection: CollectionRef) -> BootEntity | None:
    system = _mapping(ctx.data.get("system_info"))
    boot_id = _optional_text(system.get("boot_id"))
    if boot_id is None:
        return None
    return BootEntity(
        entity_type="boot",
        boot_id=collection.boot_id,
        machine_id=collection.machine_id,
        started_realtime_ns=None,
        kernel_version=_optional_text(system.get("kernel_version")),
    )


def resolve_journal_artifacts(ctx: PhaseContext) -> JournalArtifacts:
    """找到 3.5 留下的 Journal 导出文件与实体清单，并把绝对路径写回采集记录。

    3.5 的游标记录只写文件名（``journal-<run>.export``），文件放在它自己的工作目录里。
    因此相对路径不能按 Worker 的当前工作目录解析，而要以 3.5 的工作目录为基准。显式配置了
    路径但文件不存在时直接报错：静默跳过会让一次已经采集到的 Journal 原文随打包被丢掉。
    """
    journal = _mapping(ctx.data.get(JOURNAL_OPTION))
    delivery = _mapping(ctx.options.get(DELIVERY_OPTION))
    work_dirs = _journal_work_dirs(ctx, journal, delivery)
    export_raw = _optional_text(journal.get("export_file") or delivery.get("journal_export"))
    entities_raw = _optional_text(journal.get("entities_file") or delivery.get("journal_entities"))
    if export_raw is None and entities_raw is None:
        cursor = _read_journal_cursor(work_dirs, ctx.run_id)
        if cursor is not None:
            export_raw = _optional_text(cursor.get("export_file"))
            entities_raw = _optional_text(cursor.get("entities_file"))

    export = _resolve_journal_path(export_raw, work_dirs, "Journal 导出文件")
    entities = _resolve_journal_path(entities_raw, work_dirs, "Journal 实体清单")
    work_dir: Path | None = None
    if export is not None:
        work_dir = export.parent
    elif entities is not None:
        work_dir = entities.parent
    artifacts = JournalArtifacts(work_dir=work_dir, export=export, entities=entities)
    if artifacts.present:
        record = dict(journal)
        record.update(artifacts.as_mapping())
        ctx.data[JOURNAL_OPTION] = record
    return artifacts


def _journal_work_dirs(
    ctx: PhaseContext,
    journal: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> list[Path]:
    """3.5 的产物目录：显式配置优先，否则用约定目录与采集临时目录的同级目录。"""
    configured = (
        _optional_text(journal.get("work_dir"))
        or _optional_text(delivery.get(JOURNAL_WORK_DIR_OPTION))
        or _optional_text(ctx.options.get("work_dir"))
    )
    if configured is not None:
        return [Path(configured)]
    name = f"journal-{_safe_name(ctx.run_id)}"
    return [ctx.scratch_dir.parent / name, JOURNAL_WORK_ROOT / name]


def _read_journal_cursor(work_dirs: Sequence[Path], run_id: str) -> Mapping[str, Any] | None:
    name = f"journal-{_safe_name(run_id)}.cursor.json"
    for work_dir in work_dirs:
        path = work_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(f"读不到 3.5 的游标记录 {path}：{error}") from error
        if not isinstance(payload, Mapping):
            raise DeliveryError(f"3.5 的游标记录不是对象：{path}")
        return payload
    return None


def _resolve_journal_path(
    raw: str | None,
    work_dirs: Sequence[Path],
    label: str,
) -> Path | None:
    if raw is None:
        return None
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [work_dir / path for work_dir in work_dirs]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise DeliveryError(f"{label}不存在：{candidates[0]}（配置值 {raw!r}）")


def _read_entities(path: Path) -> list[BaseModel]:
    rows: list[BaseModel] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        entity_type = raw.get("entity_type")
        model = ENTITY_MODELS.get(entity_type)
        if model is None:
            raise DeliveryError(f"实体清单里有不认识的 entity_type={entity_type!r}")
        rows.append(model.model_validate(raw))
    return rows


def _extra_artifacts(options: Mapping[str, Any]) -> list[PackagingArtifact]:
    raw = options.get("artifacts") or ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise DeliveryError("delivery.artifacts 必须是列表")
    artifacts: list[PackagingArtifact] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise DeliveryError("delivery.artifacts 里的每一项必须是对象")
        source = item.get("source")
        path = item.get("path")
        if not isinstance(source, str) or not isinstance(path, str):
            raise DeliveryError("delivery.artifacts 的每一项都需要 source 与 path")
        artifacts.append(
            PackagingArtifact(
                source=Path(source),
                path=path,
                media_type=str(item.get("media_type", "application/octet-stream")),
                description=_optional_text(item.get("description")),
            )
        )
    return artifacts


def _record_mapping(record: Record) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _unique_record_mappings(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record.get("record_id")
        if isinstance(record_id, str):
            unique[record_id] = dict(record)
    return list(unique.values())


def _attempts(options: Mapping[str, Any]) -> int:
    value = options.get("download_attempts", DEFAULT_DOWNLOAD_ATTEMPTS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DeliveryError(f"download_attempts 必须是正整数，实际是 {value!r}")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "_" for character in value
    )
    return cleaned or "collection"


__all__ = [
    "DEFAULT_DOWNLOAD_ATTEMPTS",
    "DeliveryError",
    "DeliveryReport",
    "JournalArtifacts",
    "deliver_from_context",
    "resolve_journal_artifacts",
]
