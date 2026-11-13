"""In Progress 阶段的真实采样：把资源与 Journal 采集插件接到编排上。

采集器按插件契约是同步方法，编排是异步协程；这里用 ``asyncio.to_thread`` 把插件放到
Worker 的工作线程里执行，避免阻塞事件循环。插件不直接写数据库：它们通过
``PluginDataAccess`` 追加记录，本模块把记录落到本次采集临时目录的 JSONL 文件里，Post 阶段
的打包再从这里读取。这样既不违反存储层「只允许向已导入采集追加记录」的规则，也不需要把
上千条采样记录塞进任务行。

只有任务配置了 ``options.delivery`` 时才采样：没有回传就没有消费方，跑一次远程窗口只会
白白占用目标机器。数据库连接串只通过插件配置或单次请求选项传入，不写进采集记录。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.delivery import (
    BOOT_ID_OPTION,
    COLLECTION_ID_OPTION,
    DATABASE_URL_OPTION,
    DELIVERY_OPTION,
    JOURNAL_OPTION,
    MACHINE_ID_OPTION,
)
from system_analyzer.collectors.journal import PLUGIN_ID as JOURNAL_PLUGIN_ID
from system_analyzer.collectors.resources import DEFAULT_WINDOW_SECONDS
from system_analyzer.collectors.resources import PLUGIN_ID as RESOURCE_PLUGIN_ID
from system_analyzer.core.config import get_settings
from system_analyzer.domain.time_base import normalize_boot_id
from system_analyzer.plugins.contracts import (
    CollectRequest,
    CollectResult,
)
from system_analyzer.plugins.registry import PluginRegistry, get_registry

SAMPLING_OPTION = "sampling"
RECORDS_FILE_NAME = "sampling-records.jsonl"
JOURNAL_DIR_NAME = "journal"


class SamplingError(RuntimeError):
    """采样前置条件不满足，或者采集器留下的产物不可用。"""


class RecordingDataAccess:
    """把插件追加的记录留在内存里，采集结束后一次写成 JSONL。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append_records(
        self,
        records: Sequence[Any],
        *,
        plugin_id: str,
        source: str,
    ) -> int:
        for record in records:
            if not isinstance(record, Mapping):
                raise SamplingError(
                    f"插件 {plugin_id} 返回了不是对象的记录：{type(record).__name__}"
                )
            self.records.append(dict(record))
        return len(records)

    def query_records(
        self,
        *,
        record_kinds: Sequence[str] = (),
        limit: int | None = None,
    ) -> Sequence[dict[str, Any]]:
        wanted = set(record_kinds)
        selected = [record for record in self.records if not wanted or record.get("kind") in wanted]
        return selected if limit is None else selected[:limit]


async def start_sampling(ctx: PhaseContext) -> None:
    """确认本次采集要采样，并记下三个标识与记录文件的位置。

    真正占用目标机器的是下一步「持续采集」。这里只做准备，因此重试时即使这一步被复用，
    记录里也已经有一份完整的标识，打包阶段不需要重新推导。
    """
    if DELIVERY_OPTION not in ctx.options:
        ctx.data[SAMPLING_OPTION] = {
            "status": "skipped",
            "reason": "任务没有配置 options.delivery，本次不采样",
        }
        return
    ctx.data[SAMPLING_OPTION] = _prepared_state(ctx)


async def collect_continuously(ctx: PhaseContext) -> None:
    """在一个采样窗口内取回资源记录与 Journal 导出文件。

    两个采集器串行执行：资源采集默认 30 秒，Journal 只导出本次启动的日志。任一采集器失败
    都让本阶段失败，不生成缺一半数据的包；下一次尝试会重新采样。
    """
    state = _mapping(ctx.data.get(SAMPLING_OPTION))
    if state.get("status") == "skipped":
        return
    if not state:
        if DELIVERY_OPTION not in ctx.options:
            return
        state = _prepared_state(ctx)
        ctx.data[SAMPLING_OPTION] = state

    registry = get_registry()
    recorder = RecordingDataAccess()
    resources = await _run_blocking(_collect_resources, registry, ctx, recorder, state)
    journal_record, journal = await _run_blocking(_collect_journal, registry, ctx, recorder, state)
    records_file = _records_path(ctx)
    await _run_blocking(_write_records, records_file, recorder.records)

    completed = dict(state)
    completed.update(
        {
            "status": "completed",
            "stopped": True,
            "records_file": str(records_file),
            "records_written": len(recorder.records),
            "resources_written": resources.records_written,
            "journal_artifacts": len(journal.artifacts),
        }
    )
    ctx.data[SAMPLING_OPTION] = completed
    ctx.data[JOURNAL_OPTION] = journal_record


async def stop_sampling(ctx: PhaseContext) -> None:
    """确认采样窗口已经结束，没有留下后台进程。

    资源与 Journal 都是一次性窗口：采集器在 ``collect`` 返回前就结束了远端命令。这一步因此
    只把「已停止」写进记录，重试时重复执行没有副作用。
    """
    state = _mapping(ctx.data.get(SAMPLING_OPTION))
    if state.get("status") != "completed":
        return
    updated = dict(state)
    updated["stopped"] = True
    ctx.data[SAMPLING_OPTION] = updated


def _prepared_state(ctx: PhaseContext) -> dict[str, Any]:
    system = _mapping(ctx.data.get("system_info"))
    hostname = _optional_text(system.get("hostname"))
    boot_raw = _optional_text(ctx.options.get(BOOT_ID_OPTION) or system.get("boot_id"))
    if not hostname:
        raise SamplingError("系统信息里没有 hostname，无法确定 machine_id")
    if not boot_raw:
        raise SamplingError("系统信息里没有 boot_id，无法把采样定位到一次启动")
    try:
        boot_id = normalize_boot_id(boot_raw)
    except ValueError as error:
        raise SamplingError(f"系统信息里的 boot_id 不合法：{error}") from error
    machine_id = _optional_text(ctx.options.get(MACHINE_ID_OPTION)) or hostname
    if not machine_id.startswith("machine:"):
        machine_id = f"machine:{machine_id}"
    collection_id = _optional_text(ctx.options.get(COLLECTION_ID_OPTION)) or (
        f"collection:{ctx.run_id}"
    )
    return {
        "status": "running",
        "collection_id": collection_id,
        "machine_id": machine_id,
        "boot_id": boot_id,
        "window_seconds": DEFAULT_WINDOW_SECONDS,
    }


async def _run_blocking[T](function: Callable[..., T], *args: Any) -> T:
    """把同步插件放到工作线程执行，并保证线程不会越过本次阶段。

    ``asyncio.to_thread`` 启动的线程无法被取消：阶段超时后如果直接进入 Post 或重试，
    旧线程仍会写同一个采集临时目录，与下一次尝试产生竞争。因此取消时先等线程结束，
    再让取消继续向上传播；线程的异常在这里不再抛出，保留原取消语义。
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        raise


def _collect_resources(
    registry: PluginRegistry,
    ctx: PhaseContext,
    recorder: RecordingDataAccess,
    state: Mapping[str, Any],
) -> CollectResult:
    options = {
        "machine_id": state["machine_id"],
        "boot_id": state["boot_id"],
        "collection_id": state["collection_id"],
        "window_seconds": state["window_seconds"],
    }
    return registry.invoke(
        RESOURCE_PLUGIN_ID,
        lambda plugin, context: plugin.collect(
            CollectRequest(target_id=ctx.target_id, run_id=ctx.run_id, options=options),
            context,
        ),
        data=recorder,
        config={"database_url": _database_url()},
        run_id=ctx.run_id,
    )


def _collect_journal(
    registry: PluginRegistry,
    ctx: PhaseContext,
    recorder: RecordingDataAccess,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], CollectResult]:
    work_dir = (ctx.scratch_dir / JOURNAL_DIR_NAME).resolve()
    options = {
        "machine_id": state["machine_id"],
        "boot_id": state["boot_id"],
        "collection_id": state["collection_id"],
        "work_dir": str(work_dir),
        DATABASE_URL_OPTION: _database_url(),
    }
    result = registry.invoke(
        JOURNAL_PLUGIN_ID,
        lambda plugin, context: plugin.collect(
            CollectRequest(target_id=ctx.target_id, run_id=ctx.run_id, options=options),
            context,
        ),
        data=recorder,
        config={},
        run_id=ctx.run_id,
    )
    return _journal_record(work_dir, result.artifacts), result


def _journal_record(work_dir: Path, artifacts: Sequence[str]) -> dict[str, Any]:
    """把采集器返回的产物路径整理成打包阶段读取的形状。"""
    record: dict[str, Any] = {"work_dir": str(work_dir)}
    for raw in artifacts:
        path = Path(raw)
        if not path.is_absolute():
            path = path.resolve()
        if path.name.endswith(".export"):
            record["export_file"] = str(_existing(path).resolve())
        elif path.name.endswith(".entities.jsonl"):
            record["entities_file"] = str(_existing(path).resolve())
        elif path.name.endswith(".cursor.json"):
            payload = _read_json(path)
            for key in ("export_file", "entities_file"):
                if key in record:
                    continue
                value = _optional_text(payload.get(key))
                if value is None:
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                if candidate.is_file():
                    record[key] = str(candidate.resolve())
    return record


def _records_path(ctx: PhaseContext) -> Path:
    return ctx.scratch_dir / RECORDS_FILE_NAME


def _write_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SamplingError(f"读不到 Journal 采集器的游标记录 {path}：{error}") from error
    if not isinstance(payload, Mapping):
        raise SamplingError(f"Journal 采集器的游标记录不是对象：{path}")
    return payload


def _existing(path: Path) -> Path:
    if not path.is_file():
        raise SamplingError(f"Journal 采集器返回的产物不存在：{path}")
    return path


def _database_url() -> str:
    value = str(get_settings().database_url).strip()
    if not value:
        raise SamplingError("运行配置里没有 database_url，无法建立远程连接")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "JOURNAL_DIR_NAME",
    "RECORDS_FILE_NAME",
    "SAMPLING_OPTION",
    "RecordingDataAccess",
    "SamplingError",
    "collect_continuously",
    "start_sampling",
    "stop_sampling",
]
