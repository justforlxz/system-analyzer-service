"""远程 Journal 采集插件（任务 3.5）。

把目标机器上一次启动的 Journal 取回本地，解析成统一记录后追加到当前采集，同时留下
原始导出文件与游标记录：

- 启动标识取自 ``/proc/sys/kernel/random/boot_id``，导出命令再以 ``_BOOT_ID`` 过滤，
  保证拿到的只是指定 boot 的日志；
- 增量采集用 journald 自己的 ``__CURSOR``：``--after-cursor`` 是排他的，上一次最后一条
  的游标交回来就能接着取，不重复也不跳条；
- 解析与字段归一化全部复用任务 1.4 的 ``parse_journal_archive`` / ``build_bundle``，
  本模块只把记录的作用域改到当前采集上，不另写一套字段映射；
- ``CollectResult.artifacts`` 返回本地导出文件、实体清单与游标记录，原始文件交给任务 3.7 回传。

采集插件按契约不能写实体（``PluginDataAccess`` 只有追加与查询记录两个入口），因此本模块
把 1.4 ``build_bundle()`` 得到的 machine、boot 与 service 实体按本次采集的标识重建后，
用 ``journal_entities()`` 导出，并写成 ``journal-<run>.entities.jsonl`` 产物。
任务 3.7 打包 ``.bootrec`` 时汇总这份清单并写入采集；``Journal`` 记录引用的每个
``service:<unit>`` 都出现在清单里。

运行方式与契约一致：``collect`` 是同步方法，经 ``PluginRegistry.invoke`` 调用。默认的
远程入口用任务 3.1 的同步适配层 ``SyncSshTargetService``，数据库连接串由 Core 通过
``CollectRequest.options["database_url"]`` 传入；测试可以构造插件时注入假的远程入口，
不需要真机和数据库。连接串只应在调用时由 Core 从运行配置里取出，不要写进持久化的任务
payload，避免把数据库凭据落到 ``jobs`` 表。
"""

from __future__ import annotations

import inspect
import json
import logging
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from system_analyzer.domain.bootrec import (
    BootEntity,
    GenericEntity,
    MachineEntity,
    compute_record_id,
)
from system_analyzer.domain.time_base import normalize_boot_id
from system_analyzer.importers.journal_archive import (
    DEFAULT_MAX_DECOMPRESSED_BYTES,
    JournalBundle,
    archive_digest,
    build_bundle,
    parse_journal_archive,
)
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    CollectRequest,
    CollectResult,
    PluginBase,
    PluginContext,
    PluginDataAccess,
    PluginInputError,
    PluginKind,
    PluginSpec,
    PluginUnavailableError,
)
from system_analyzer.remote.errors import RemoteError
from system_analyzer.remote.sync import SyncSshTargetService

logger = logging.getLogger(__name__)

PLUGIN_ID = "journal.remote"
CAPABILITY = "journal.remote"

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0
DEFAULT_REMOTE_TMP_DIR = "/tmp"
DEFAULT_WORK_ROOT = Path("var/collections")

OPTION_DATABASE_URL = "database_url"
OPTION_COLLECTION_ID = "collection_id"
OPTION_MACHINE_ID = "machine_id"
OPTION_BOOT_ID = "boot_id"
OPTION_AFTER_CURSOR = "after_cursor"
OPTION_SINCE = "since"
OPTION_UNTIL = "until"
OPTION_WORK_DIR = "work_dir"
OPTION_REMOTE_TMP_DIR = "remote_tmp_dir"
OPTION_COMMAND_TIMEOUT_SECONDS = "command_timeout_seconds"


class JournalRemote(Protocol):
    """插件需要的远程能力：执行命令、下载文件。同步方法，跑在 Worker 的工作线程里。"""

    def run(
        self,
        target_id: str,
        command: str,
        *,
        timeout: float | None = None,
    ) -> str:
        """执行一条远端命令并返回标准输出；失败时抛 ``RemoteError``。"""

    def download(self, target_id: str, remote_path: str, local_path: Path) -> None:
        """把目标机器上的文件取回本地；失败时抛 ``RemoteError``。"""


class SyncSshJournalRemote:
    """默认远程入口：包装任务 3.1 的同步 SSH 适配层。"""

    def __init__(self, database_url: str) -> None:
        self._service = SyncSshTargetService(database_url)

    def run(
        self,
        target_id: str,
        command: str,
        *,
        timeout: float | None = None,
    ) -> str:
        return self._service.run_command(target_id, command, timeout=timeout).stdout

    def download(self, target_id: str, remote_path: str, local_path: Path) -> None:
        self._service.download(target_id, remote_path, str(local_path))


RemoteFactory = Callable[[Mapping[str, Any]], JournalRemote]


class JournalCollector(PluginBase):
    """远程 Journal 采集插件。无跨调用的可变状态。"""

    spec = PluginSpec(
        id=PLUGIN_ID,
        kind=PluginKind.COLLECTOR,
        name="远程 Journal 采集",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=(CAPABILITY,),
    )

    def __init__(self, remote_factory: RemoteFactory | None = None) -> None:
        self._remote_factory = remote_factory or _default_remote_factory

    def collect(self, request: CollectRequest, context: PluginContext) -> CollectResult:
        """采集一次 Journal：读 boot id、按游标导出、解析、追加记录并留下游标。"""
        options = request.options
        collection_id = _required_text(options, OPTION_COLLECTION_ID)
        after_cursor = _optional_text(options, OPTION_AFTER_CURSOR)
        since = _optional_text(options, OPTION_SINCE)
        until = _optional_text(options, OPTION_UNTIL)
        timeout = _timeout(options)

        data = context.data
        if not isinstance(data, PluginDataAccess):
            raise PluginInputError(
                "采集需要 context.data 提供同步的 append_records 与 query_records"
                f"（docs/plugin-contracts.md 第 5 节），实际拿到的是 {type(data).__name__}"
            )

        remote = self._remote_factory(options)
        target_id = request.target_id
        boot_id = _boot_id(remote, target_id, options, timeout=timeout)
        work_dir = _work_dir(options, request.run_id)
        local_export = work_dir / f"journal-{_safe_name(request.run_id)}.export"
        local_entities = work_dir / f"journal-{_safe_name(request.run_id)}.entities.jsonl"
        local_cursor = work_dir / f"journal-{_safe_name(request.run_id)}.cursor.json"

        remote_dir = _make_remote_dir(remote, target_id, options, timeout=timeout)
        try:
            remote_export = f"{remote_dir}/journal.export"
            command = build_export_command(
                boot_id,
                after_cursor=after_cursor,
                since=since,
                until=until,
            )
            remote.run(
                target_id,
                f"{command} > {shlex.quote(remote_export)}",
                timeout=timeout,
            )
            remote.download(target_id, remote_export, local_export)
        except RemoteError as error:
            raise PluginUnavailableError(f"从 {target_id} 取 Journal 失败：{error}") from error
        finally:
            _remove_remote_dir(remote, target_id, remote_dir, timeout=timeout)

        bundle, cursor = _read_bundle(local_export, boot_id=boot_id)
        if cursor is None:
            cursor = after_cursor
        resolved_machine_id = _optional_text(options, OPTION_MACHINE_ID) or (
            bundle.collection.machine_id if bundle is not None else ""
        )
        records = (
            _rescale_records(
                bundle,
                collection_id=collection_id,
                machine_id=resolved_machine_id,
                boot_id=boot_id,
                plugin_id=self.spec.id,
            )
            if bundle is not None
            else ()
        )
        entities = (
            journal_entities(bundle, machine_id=resolved_machine_id, boot_id=boot_id)
            if bundle is not None and resolved_machine_id
            else ()
        )
        if entities:
            _write_jsonl(local_entities, entities)
        cursor_record = {
            "plugin_id": self.spec.id,
            "target_id": target_id,
            "run_id": request.run_id,
            "collection_id": collection_id,
            "machine_id": resolved_machine_id,
            "boot_id": boot_id,
            "cursor": cursor,
            "after_cursor": after_cursor,
            "since": since,
            "until": until,
            "entry_count": len(bundle.records) if bundle is not None else 0,
            "source_sha256": bundle.source_sha256 if bundle is not None else None,
            "export_file": local_export.name if bundle is not None else None,
            "entities_file": local_entities.name if entities else None,
        }
        _write_json(local_cursor, cursor_record)

        artifacts = [str(local_cursor)]
        if records:
            artifacts.insert(0, str(local_export))
            if entities:
                artifacts.insert(1, str(local_entities))
            written = data.append_records(
                records,
                plugin_id=self.spec.id,
                source=request.run_id,
            )
            _reject_coroutine(written, "append_records")
        else:
            written = 0
        return CollectResult(records_written=written, artifacts=tuple(artifacts))


def build_export_command(
    boot_id: str,
    *,
    after_cursor: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """拼出 ``journalctl`` 导出命令；所有来自外部的值都经过 shell 引号。"""
    boot_hex = normalize_boot_id(boot_id).replace("-", "")
    parts = ["journalctl", "--no-pager", "--quiet", "-o", "export"]
    if after_cursor:
        parts.append(f"--after-cursor={shlex.quote(after_cursor)}")
    if since:
        parts.append(f"--since={shlex.quote(since)}")
    if until:
        parts.append(f"--until={shlex.quote(until)}")
    parts.append(f"_BOOT_ID={shlex.quote(boot_hex)}")
    return " ".join(parts)


def _default_remote_factory(options: Mapping[str, Any]) -> JournalRemote:
    database_url = _optional_text(options, OPTION_DATABASE_URL)
    if not database_url:
        raise PluginInputError(
            "默认远程入口需要 Core 在 CollectRequest.options 里提供 "
            f"{OPTION_DATABASE_URL}（目标机器凭据从数据库读取）；测试可以注入 remote_factory"
        )
    return SyncSshJournalRemote(database_url)


def _boot_id(
    remote: JournalRemote,
    target_id: str,
    options: Mapping[str, Any],
    *,
    timeout: float,
) -> str:
    configured = _optional_text(options, OPTION_BOOT_ID)
    if configured:
        return _normalize_boot_id(configured)
    try:
        raw = remote.run(target_id, f"cat {shlex.quote(BOOT_ID_PATH)}", timeout=timeout)
    except RemoteError as error:
        raise PluginUnavailableError(f"读不到 {target_id} 的 boot id：{error}") from error
    value = raw.strip().splitlines()[-1] if raw.strip() else ""
    if not value:
        raise PluginInputError(f"目标机器 {target_id} 的 {BOOT_ID_PATH} 是空的")
    return _normalize_boot_id(value)


def _normalize_boot_id(raw: str) -> str:
    try:
        return normalize_boot_id(raw)
    except ValueError as error:
        raise PluginInputError(f"boot id 不合法：{error}") from error


def _make_remote_dir(
    remote: JournalRemote,
    target_id: str,
    options: Mapping[str, Any],
    *,
    timeout: float,
) -> str:
    root = _optional_text(options, OPTION_REMOTE_TMP_DIR) or DEFAULT_REMOTE_TMP_DIR
    template = f"{root.rstrip('/')}/sas-journal.XXXXXX"
    try:
        output = remote.run(target_id, f"mktemp -d {shlex.quote(template)}", timeout=timeout)
    except RemoteError as error:
        raise PluginUnavailableError(f"在 {target_id} 上建临时目录失败：{error}") from error
    directory = output.strip().splitlines()[-1] if output.strip() else ""
    if not directory:
        raise PluginInputError("目标机器上的 mktemp 没有返回临时目录")
    return directory


def _remove_remote_dir(
    remote: JournalRemote,
    target_id: str,
    remote_dir: str,
    *,
    timeout: float,
) -> None:
    """删除远端临时目录；失败只记日志，不覆盖已经采到的数据。"""
    try:
        remote.run(target_id, f"rm -rf {shlex.quote(remote_dir)}", timeout=timeout)
    except Exception:
        logger.warning("删除目标机器上的临时目录 %s 失败", remote_dir, exc_info=True)


def _read_bundle(local_export: Path, *, boot_id: str) -> tuple[JournalBundle | None, str | None]:
    """解析导出文件，返回（1.4 的采集结果、最后游标）。空导出返回 ``(None, None)``。"""
    if not local_export.exists() or local_export.stat().st_size == 0:
        return None, None
    parsed = parse_journal_archive(
        local_export,
        max_decompressed_bytes=DEFAULT_MAX_DECOMPRESSED_BYTES,
    )
    if not parsed.entries:
        return None, None
    last = parsed.entries[-1]
    cursor = last.get("__CURSOR")
    if not cursor:
        raise PluginInputError(
            "导出文件最后一条 Journal 事件没有 __CURSOR，无法界定下一次增量采集的范围"
        )
    bundle = build_bundle(parsed, source_sha256=archive_digest(local_export))
    if bundle.collection.boot_id != boot_id:
        raise PluginInputError(
            f"导出的 Journal 属于 boot {bundle.collection.boot_id}，不是本次采集的 {boot_id}"
        )
    return bundle, cursor


def _rescale_records(
    bundle: JournalBundle,
    *,
    collection_id: str,
    machine_id: str,
    boot_id: str,
    plugin_id: str,
) -> tuple[dict[str, Any], ...]:
    """复用 1.4 的归一化结果，只把作用域改到当前采集并重算记录摘要。"""
    records: list[dict[str, Any]] = []
    for record in bundle.records:
        body = {key: value for key, value in record.items() if key != "record_id"}
        body["machine_id"] = machine_id
        body["boot_id"] = boot_id
        body["collection_id"] = collection_id
        body["source_plugin"] = plugin_id
        body["record_id"] = compute_record_id(body)
        records.append(body)
    return tuple(records)


def journal_entities(
    bundle: JournalBundle,
    *,
    machine_id: str,
    boot_id: str,
) -> tuple[MachineEntity | BootEntity | GenericEntity, ...]:
    """从 1.4 的采集结果导出本次采集的实体清单。

    实体按本次采集的 ``machine_id`` 与 ``boot_id`` 重建：``build_bundle()`` 用导出文件里的
    ``_HOSTNAME`` 推导 ``machine_id``，而采集可能使用 Core 分配的另一个标识；直接返回
    bundle 里的实体，会让存储层以“实体的 machine_id 与采集不一致”拒绝整批数据。
    """
    source_machine = next(
        (entity for entity in bundle.entities if isinstance(entity, MachineEntity)), None
    )
    source_boot = next(
        (entity for entity in bundle.entities if isinstance(entity, BootEntity)), None
    )
    if source_machine is None or source_boot is None:
        raise PluginInputError("1.4 的采集结果缺少 machine 或 boot 实体，无法导出实体清单")

    entities: list[MachineEntity | BootEntity | GenericEntity] = [
        MachineEntity(
            entity_type="machine",
            machine_id=machine_id,
            hostname=source_machine.hostname,
            os_release=source_machine.os_release,
            kernel_version=source_machine.kernel_version,
            architecture=source_machine.architecture,
        ),
        BootEntity(
            entity_type="boot",
            boot_id=boot_id,
            machine_id=machine_id,
            started_realtime_ns=source_boot.started_realtime_ns,
            kernel_version=source_boot.kernel_version,
            boot_index=source_boot.boot_index,
        ),
    ]
    for entity in bundle.entities:
        if not isinstance(entity, GenericEntity) or entity.entity_type != "service":
            continue
        entities.append(entity.model_copy(update={"machine_id": machine_id, "boot_id": boot_id}))
    return tuple(entities)


def _work_dir(options: Mapping[str, Any], run_id: str) -> Path:
    configured = _optional_text(options, OPTION_WORK_DIR)
    if configured:
        path = Path(configured)
    else:
        path = DEFAULT_WORK_ROOT / f"journal-{_safe_name(run_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "_" for character in value
    )
    return cleaned or "run"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Any]) -> None:
    path.write_text(
        "".join(
            json.dumps(row.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _timeout(options: Mapping[str, Any]) -> float:
    value = options.get(OPTION_COMMAND_TIMEOUT_SECONDS, DEFAULT_COMMAND_TIMEOUT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PluginInputError(
            f"选项 {OPTION_COMMAND_TIMEOUT_SECONDS} 必须是正数，实际是 {value!r}"
        )
    return float(value)


def _required_text(options: Mapping[str, Any], name: str) -> str:
    value = _optional_text(options, name)
    if not value:
        raise PluginInputError(f"远程 Journal 采集需要 CollectRequest.options.{name}")
    return value


def _optional_text(options: Mapping[str, Any], name: str) -> str | None:
    value = options.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PluginInputError(f"选项 {name} 必须是字符串，实际是 {type(value).__name__}")
    stripped = value.strip()
    return stripped or None


def _reject_coroutine(result: Any, name: str) -> None:
    if not inspect.isawaitable(result):
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()
    raise PluginInputError(f"数据入口 {name} 返回了协程，采集需要同步入口")


__all__ = [
    "CAPABILITY",
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "PLUGIN_ID",
    "JournalCollector",
    "JournalRemote",
    "SyncSshJournalRemote",
    "build_export_command",
    "journal_entities",
]
