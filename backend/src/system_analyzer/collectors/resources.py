"""远程系统资源采集插件（任务 3.4）。

一次 ``collect`` 只建立一条 SSH 命令：目标机器上的标准 Python 在采样窗口内循环读取
``/proc`` 与 cgroup v2 文件，把每个采样点按 JSON Lines 打印回来。插件在 Worker 的线程里
同步解析这些行，换算成统一 Metric 记录，再经 ``context.data.append_records`` 写入。

时间字段严格按 ``docs/time-base.md``：

- 每个采样点同时带 ``CLOCK_MONOTONIC`` 与 ``CLOCK_REALTIME`` 读数；
- ``monotonic_ns`` 非空，因此 ``clock_domain`` 固定为 ``boot_monotonic``；
- 采样窗口、采样间隔只影响采多少点，不写进记录；抖动与缺口由读取方按第 8 节推导。

缺失语义：某个指标这一拍读不到（文件不存在、内容无法解析、计数器回退）时，不生成这条
Metric 记录，也不写 0 或 null 占位。整条指标流都没有数据时，这条流不会出现在结果里；
把流登记成 ``available = false`` 的工作由 Core 在组装实体时完成，插件不直接写实体。
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from system_analyzer.domain.bootrec import MetricStreamEntity, RecordKind, compute_record_id
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    CollectRequest,
    CollectResult,
    PluginBase,
    PluginContext,
    PluginInputError,
    PluginKind,
    PluginSpec,
)
from system_analyzer.remote.sync import SyncSshTargetService

PLUGIN_ID = "resource-sampler"
CAPABILITY = "system.resources"

DEFAULT_WINDOW_SECONDS = 30.0
DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_CGROUPS = 64

SYSTEM_CPU = "cpu.total.utilization"
SYSTEM_MEMORY = "memory.used"
SYSTEM_DISK_READ = "disk.read_bytes_per_second"
SYSTEM_DISK_WRITE = "disk.write_bytes_per_second"

UNIT_CPU = "cpu.unit.utilization"
UNIT_MEMORY = "memory.unit.used"
UNIT_DISK_READ = "disk.unit.read_bytes_per_second"
UNIT_DISK_WRITE = "disk.unit.write_bytes_per_second"

CGROUP_CPU = "cpu.cgroup.utilization"
CGROUP_MEMORY = "memory.cgroup.used"
CGROUP_DISK_READ = "disk.cgroup.read_bytes_per_second"
CGROUP_DISK_WRITE = "disk.cgroup.write_bytes_per_second"

_SYSTEM_STREAMS = {
    SYSTEM_CPU: ("percent", "cpu"),
    SYSTEM_MEMORY: ("bytes", "memory"),
    SYSTEM_DISK_READ: ("bytes_per_second", "disk"),
    SYSTEM_DISK_WRITE: ("bytes_per_second", "disk"),
}

_UNIT_STREAMS = {
    UNIT_CPU: ("percent", "cpu"),
    UNIT_MEMORY: ("bytes", "memory"),
    UNIT_DISK_READ: ("bytes_per_second", "disk"),
    UNIT_DISK_WRITE: ("bytes_per_second", "disk"),
}

_CGROUP_STREAMS = {
    CGROUP_CPU: ("percent", "cpu"),
    CGROUP_MEMORY: ("bytes", "memory"),
    CGROUP_DISK_READ: ("bytes_per_second", "disk"),
    CGROUP_DISK_WRITE: ("bytes_per_second", "disk"),
}

_REMOTE_SCRIPT = r"""
import json
import os
import socket
import sys
import time

params = json.loads(sys.argv[1])
duration = float(params["window_seconds"])
interval = float(params["interval_seconds"])
max_cgroups = int(params["max_cgroups"])
include_units = bool(params["include_units"])
include_cgroups = bool(params["include_cgroups"])
cpu_count = os.cpu_count() or 1

UNIT_SUFFIXES = (
    ".service",
    ".scope",
    ".slice",
    ".socket",
    ".mount",
    ".timer",
    ".path",
    ".target",
    ".device",
    ".swap",
)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cpu_times():
    text = read_text("/proc/stat")
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        values = [int_or_none(item) for item in line.split()[1:]]
        if len(values) < 4 or any(item is None for item in values[:8]):
            return None
        total = sum(values[:8])
        idle = values[3] + values[4]
        return {"total": total, "idle": idle}
    return None


def memory_used():
    text = read_text("/proc/meminfo")
    if not text:
        return None
    values = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if not separator:
            continue
        values[key] = int_or_none(rest.split()[0]) if rest.split() else None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or available > total:
        return None
    return {"used_bytes": (total - available) * 1024}


def disk_counters():
    base = "/sys/block"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    read_total = 0
    write_total = 0
    found = False
    for name in names:
        if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "fd")):
            continue
        text = read_text(os.path.join(base, name, "stat"))
        if text is None:
            continue
        parts = text.split()
        if len(parts) < 7:
            continue
        sectors_read = int_or_none(parts[2])
        sectors_written = int_or_none(parts[6])
        if sectors_read is None or sectors_written is None:
            continue
        read_total += sectors_read * 512
        write_total += sectors_written * 512
        found = True
    if not found:
        return None
    return {"read_bytes": read_total, "write_bytes": write_total}


def cgroup_values(path):
    usage_usec = None
    cpu = read_text(os.path.join(path, "cpu.stat"))
    if cpu:
        for line in cpu.splitlines():
            if line.startswith("usage_usec "):
                usage_usec = int_or_none(line.split()[1])
                break

    memory = None
    memory_text = read_text(os.path.join(path, "memory.current"))
    if memory_text:
        memory = int_or_none(memory_text.strip())

    rbytes = None
    wbytes = None
    io_text = read_text(os.path.join(path, "io.stat"))
    if io_text:
        read_total = 0
        write_total = 0
        found = False
        for line in io_text.splitlines():
            fields = {}
            for item in line.split()[1:]:
                key, separator, value = item.partition("=")
                if separator:
                    fields[key] = int_or_none(value)
            if fields.get("rbytes") is not None and fields.get("wbytes") is not None:
                read_total += fields["rbytes"]
                write_total += fields["wbytes"]
                found = True
        if found:
            rbytes = read_total
            wbytes = write_total

    if usage_usec is None and memory is None and rbytes is None:
        return None
    return {
        "cpu_usage_usec": usage_usec,
        "memory_current": memory,
        "io_rbytes": rbytes,
        "io_wbytes": wbytes,
    }


def cgroup_paths():
    root = "/sys/fs/cgroup"
    if max_cgroups <= 0:
        return []
    paths = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames.sort()
        if dirpath == root:
            continue
        relative = "/" + os.path.relpath(dirpath, root).replace(os.sep, "/")
        paths.append(relative)
        if len(paths) >= max_cgroups:
            break
    return paths


def snapshot():
    return {
        "system": {"cpu": cpu_times(), "memory": memory_used(), "disk": disk_counters()},
        "units": {},
        "cgroups": {},
    }


def collect_cgroups(current):
    if not include_units and not include_cgroups:
        return
    root = "/sys/fs/cgroup"
    for relative in cgroup_paths():
        values = cgroup_values(root + relative)
        if values is None:
            continue
        if include_cgroups:
            current["cgroups"][relative] = values
        if include_units and os.path.basename(relative).endswith(UNIT_SUFFIXES):
            current["units"][relative] = values


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


boot_id = read_text("/proc/sys/kernel/random/boot_id")
emit(
    {
        "type": "meta",
        "hostname": socket.gethostname(),
        "boot_id": boot_id.strip() if boot_id else None,
        "cpu_count": cpu_count,
        "kernel_version": os.uname().release,
    }
)

previous = snapshot()
collect_cgroups(previous)
previous_monotonic = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
deadline = previous_monotonic + int(duration * 1_000_000_000)

while True:
    now = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    remaining = deadline - now
    if remaining <= 0:
        break
    time.sleep(min(interval, remaining / 1_000_000_000))
    current = snapshot()
    collect_cgroups(current)
    monotonic_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    realtime_ns = time.time_ns()
    emit(
        {
            "type": "sample",
            "monotonic_ns": monotonic_ns,
            "realtime_ns": realtime_ns,
            "elapsed_ns": max(0, monotonic_ns - previous_monotonic),
            "system": current["system"],
            "units": current["units"],
            "cgroups": current["cgroups"],
            "previous": {
                "system": previous["system"],
                "units": previous["units"],
                "cgroups": previous["cgroups"],
            },
        }
    )
    previous = current
    previous_monotonic = monotonic_ns
"""


class RemoteSampler(Protocol):
    """执行一条远程命令并返回标准输出。测试用假实现替换真实 SSH。"""

    def capture(self, target_id: str, command: str, *, timeout: float) -> str: ...


class _SyncSshSampler:
    """真实实现：在 Worker 线程里用一条短命事件循环执行 SSH 命令。"""

    def __init__(self, database_url: str) -> None:
        self._remote = SyncSshTargetService(database_url)

    def capture(self, target_id: str, command: str, *, timeout: float) -> str:
        return self._remote.run_command(target_id, command, timeout=timeout).stdout


@dataclass(frozen=True, slots=True)
class _Sample:
    monotonic_ns: int
    realtime_ns: int
    elapsed_ns: int
    system: Mapping[str, Any] = field(default_factory=dict)
    units: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    cgroups: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    previous: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Capture:
    hostname: str
    boot_id: str | None
    cpu_count: int
    samples: tuple[_Sample, ...]


class ResourceCollector(PluginBase):
    """CPU、内存、磁盘 I/O 与 Unit / cgroup 维度的远程资源采样插件。"""

    spec = PluginSpec(
        id=PLUGIN_ID,
        kind=PluginKind.COLLECTOR,
        name="远程系统资源采集",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=(CAPABILITY,),
        config_schema={
            "type": "object",
            "properties": {
                "database_url": {"type": "string", "minLength": 1},
                "window_seconds": {"type": "number", "exclusiveMinimum": 0},
                "interval_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_cgroups": {"type": "integer", "minimum": 0},
                "include_units": {"type": "boolean"},
                "include_cgroups": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    )

    def __init__(
        self,
        sampler_factory: Callable[[str], RemoteSampler] | None = None,
    ) -> None:
        self._sampler_factory = sampler_factory

    def collect(self, request: CollectRequest, context: PluginContext) -> CollectResult:
        """采集一次并写入统一存储；失败时抛 ``PluginError`` 家族，不返回伪造的成功。"""
        target_id = request.target_id.strip()
        if not target_id:
            raise PluginInputError("collect 需要 request.target_id，不能为空")

        window_seconds = _positive_number(
            _option(request, context, "window_seconds", DEFAULT_WINDOW_SECONDS),
            "window_seconds",
        )
        interval_seconds = _positive_number(
            _option(request, context, "interval_seconds", DEFAULT_INTERVAL_SECONDS),
            "interval_seconds",
        )
        max_cgroups = _non_negative_int(
            _option(request, context, "max_cgroups", DEFAULT_MAX_CGROUPS),
            "max_cgroups",
        )
        include_units = _boolean(_option(request, context, "include_units", True), "include_units")
        include_cgroups = _boolean(
            _option(request, context, "include_cgroups", True), "include_cgroups"
        )

        command = _remote_command(
            window_seconds=window_seconds,
            interval_seconds=interval_seconds,
            max_cgroups=max_cgroups,
            include_units=include_units,
            include_cgroups=include_cgroups,
        )
        if self._sampler_factory is None:
            sampler = _SyncSshSampler(_database_url(context))
        else:
            sampler = self._sampler_factory(str(context.config.get("database_url", "")))
        stdout = sampler.capture(target_id, command, timeout=max(60.0, window_seconds * 2 + 30))
        capture = parse_capture(stdout)

        machine_id = _identity(
            _option(request, context, "machine_id", None),
            _default_machine_id(capture.hostname),
        )
        boot_id = _identity(
            _option(request, context, "boot_id", None),
            capture.boot_id or "",
        )
        if not boot_id:
            raise PluginInputError("目标机器没有返回 boot_id，无法把资源采样定位到一次启动")
        collection_id = _identity(
            _option(request, context, "collection_id", None),
            _default_collection_id(request.run_id),
        )

        records = build_records(
            capture,
            plugin_id=self.spec.id,
            collection_id=collection_id,
            machine_id=machine_id,
            boot_id=boot_id,
        )
        if not records:
            return CollectResult(records_written=0)
        written = context.data.append_records(
            records,
            plugin_id=self.spec.id,
            source=request.run_id,
        )
        return CollectResult(records_written=written)


def _option(
    request: CollectRequest,
    context: PluginContext,
    key: str,
    default: Any,
) -> Any:
    """请求选项优先于插件配置，两者都没有时取默认值。"""
    if key in request.options:
        return request.options[key]
    if key in context.config:
        return context.config[key]
    return default


def _database_url(context: PluginContext) -> str:
    value = context.config.get("database_url")
    if not isinstance(value, str) or not value.strip():
        raise PluginInputError(
            "默认 SSH 入口需要 context.config.database_url；"
            "测试或其它传输方式应通过 sampler_factory 注入采样器"
        )
    return value


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PluginInputError(f"{name} 必须是大于 0 的数字，实际是 {value!r}")
    return float(value)


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PluginInputError(f"{name} 必须是不小于 0 的整数，实际是 {value!r}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise PluginInputError(f"{name} 必须是布尔值，实际是 {value!r}")
    return value


def _identity(value: Any, fallback: str) -> str:
    if value is not None and not isinstance(value, str):
        raise PluginInputError(f"实体标识必须是字符串，实际是 {value!r}")
    resolved = (value or fallback).strip()
    if not resolved:
        raise PluginInputError("无法确定采集的 machine_id、boot_id 或 collection_id")
    return resolved


def _default_machine_id(hostname: str) -> str:
    return f"machine:{hostname}"


def _default_collection_id(run_id: str) -> str:
    if not run_id.strip():
        raise PluginInputError("缺少 collection_id 时必须提供非空 run_id，用于生成采集标识")
    return f"collection:{run_id}"


def _remote_command(
    *,
    window_seconds: float,
    interval_seconds: float,
    max_cgroups: int,
    include_units: bool,
    include_cgroups: bool,
) -> str:
    params = json.dumps(
        {
            "window_seconds": window_seconds,
            "interval_seconds": interval_seconds,
            "max_cgroups": max_cgroups,
            "include_units": include_units,
            "include_cgroups": include_cgroups,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"python3 -c {shlex.quote(_REMOTE_SCRIPT)} {shlex.quote(params)}"


def parse_capture(stdout: str) -> _Capture:
    """解析远程脚本的 JSON Lines 输出；结构不对时抛 ``PluginInputError``。"""
    meta: Mapping[str, Any] | None = None
    samples: list[_Sample] = []
    for number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise PluginInputError(f"远程采样第 {number} 行不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise PluginInputError(f"远程采样第 {number} 行不是 JSON 对象")
        kind = payload.get("type")
        if kind == "meta":
            meta = payload
        elif kind == "sample":
            samples.append(_sample_from(payload, number))
    if meta is None:
        raise PluginInputError("远程采样没有返回 meta 行，无法确认目标机器与启动标识")
    hostname = meta.get("hostname")
    if not isinstance(hostname, str) or not hostname.strip():
        raise PluginInputError("远程采样没有返回 hostname")
    boot_id = meta.get("boot_id")
    if boot_id is not None and not isinstance(boot_id, str):
        raise PluginInputError("远程采样返回的 boot_id 不是字符串")
    cpu_count = meta.get("cpu_count")
    if not isinstance(cpu_count, int) or isinstance(cpu_count, bool) or cpu_count <= 0:
        cpu_count = 1
    return _Capture(
        hostname=hostname.strip(),
        boot_id=boot_id.strip() if boot_id else None,
        cpu_count=cpu_count,
        samples=tuple(samples),
    )


def _sample_from(payload: Mapping[str, Any], number: int) -> _Sample:
    monotonic_ns = payload.get("monotonic_ns")
    realtime_ns = payload.get("realtime_ns")
    if not _is_non_negative_int(monotonic_ns) or not _is_non_negative_int(realtime_ns):
        raise PluginInputError(f"远程采样第 {number} 行缺少有效的时间戳")
    elapsed_ns = payload.get("elapsed_ns")
    if not _is_non_negative_int(elapsed_ns):
        elapsed_ns = 0
    return _Sample(
        monotonic_ns=monotonic_ns,
        realtime_ns=realtime_ns,
        elapsed_ns=elapsed_ns,
        system=_mapping(payload.get("system")),
        units=_mapping_map(payload.get("units")),
        cgroups=_mapping_map(payload.get("cgroups")),
        previous=_mapping(payload.get("previous")),
    )


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_map(value: Any) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, Mapping)
    }


def build_records(
    capture: _Capture,
    *,
    plugin_id: str,
    collection_id: str,
    machine_id: str,
    boot_id: str,
) -> list[dict[str, Any]]:
    """把一次采样的全部采样点换算成 Metric 记录；读不到的指标直接缺席。"""
    records: list[dict[str, Any]] = []
    for sample in capture.samples:
        records.extend(
            _system_records(
                sample,
                plugin_id=plugin_id,
                collection_id=collection_id,
                machine_id=machine_id,
                boot_id=boot_id,
            )
        )
        records.extend(
            _dimension_records(
                sample,
                dimension="unit",
                streams=_UNIT_STREAMS,
                plugin_id=plugin_id,
                collection_id=collection_id,
                machine_id=machine_id,
                boot_id=boot_id,
                cpu_count=capture.cpu_count,
            )
        )
        records.extend(
            _dimension_records(
                sample,
                dimension="cgroup",
                streams=_CGROUP_STREAMS,
                plugin_id=plugin_id,
                collection_id=collection_id,
                machine_id=machine_id,
                boot_id=boot_id,
                cpu_count=capture.cpu_count,
            )
        )
    return records


def _system_records(
    sample: _Sample,
    *,
    plugin_id: str,
    collection_id: str,
    machine_id: str,
    boot_id: str,
) -> list[dict[str, Any]]:
    current = sample.system
    previous = _mapping(sample.previous.get("system"))
    records: list[dict[str, Any]] = []
    elapsed_seconds = sample.elapsed_ns / 1_000_000_000

    cpu = _mapping(current.get("cpu"))
    previous_cpu = _mapping(previous.get("cpu"))
    total_delta = _delta(cpu.get("total"), previous_cpu.get("total"))
    idle_delta = _delta(cpu.get("idle"), previous_cpu.get("idle"))
    if total_delta is not None and idle_delta is not None and total_delta > 0:
        value = _clamp_percent((total_delta - idle_delta) / total_delta * 100)
        records.append(
            _metric_record(
                SYSTEM_CPU,
                value,
                _SYSTEM_STREAMS,
                sample,
                plugin_id,
                collection_id,
                machine_id,
                boot_id,
                target=SYSTEM_CPU,
            )
        )

    memory = _mapping(current.get("memory"))
    used = memory.get("used_bytes")
    if _is_number(used) and used >= 0:
        records.append(
            _metric_record(
                SYSTEM_MEMORY,
                float(used),
                _SYSTEM_STREAMS,
                sample,
                plugin_id,
                collection_id,
                machine_id,
                boot_id,
                target=SYSTEM_MEMORY,
            )
        )

    disk = _mapping(current.get("disk"))
    previous_disk = _mapping(previous.get("disk"))
    if elapsed_seconds > 0:
        read_delta = _delta(disk.get("read_bytes"), previous_disk.get("read_bytes"))
        write_delta = _delta(disk.get("write_bytes"), previous_disk.get("write_bytes"))
        if read_delta is not None:
            records.append(
                _metric_record(
                    SYSTEM_DISK_READ,
                    read_delta / elapsed_seconds,
                    _SYSTEM_STREAMS,
                    sample,
                    plugin_id,
                    collection_id,
                    machine_id,
                    boot_id,
                    target=SYSTEM_DISK_READ,
                )
            )
        if write_delta is not None:
            records.append(
                _metric_record(
                    SYSTEM_DISK_WRITE,
                    write_delta / elapsed_seconds,
                    _SYSTEM_STREAMS,
                    sample,
                    plugin_id,
                    collection_id,
                    machine_id,
                    boot_id,
                    target=SYSTEM_DISK_WRITE,
                )
            )
    return records


def _dimension_records(
    sample: _Sample,
    *,
    dimension: str,
    streams: Mapping[str, tuple[str, str]],
    plugin_id: str,
    collection_id: str,
    machine_id: str,
    boot_id: str,
    cpu_count: int,
) -> list[dict[str, Any]]:
    current = sample.units if dimension == "unit" else sample.cgroups
    previous_all = _mapping(sample.previous.get("units" if dimension == "unit" else "cgroups"))
    elapsed_seconds = sample.elapsed_ns / 1_000_000_000
    records: list[dict[str, Any]] = []
    for name, values in current.items():
        previous = _mapping(previous_all.get(name))
        target = name
        elapsed = elapsed_seconds
        records.extend(
            _dimension_metric_records(
                values,
                previous,
                target=target,
                dimension=dimension,
                streams=streams,
                sample=sample,
                plugin_id=plugin_id,
                collection_id=collection_id,
                machine_id=machine_id,
                boot_id=boot_id,
                elapsed_seconds=elapsed,
                cpu_count=cpu_count,
            )
        )
    return records


def _dimension_metric_records(
    values: Mapping[str, Any],
    previous: Mapping[str, Any],
    *,
    target: str,
    dimension: str,
    streams: Mapping[str, tuple[str, str]],
    sample: _Sample,
    plugin_id: str,
    collection_id: str,
    machine_id: str,
    boot_id: str,
    elapsed_seconds: float,
    cpu_count: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    usage_delta = _delta(values.get("cpu_usage_usec"), previous.get("cpu_usage_usec"))
    if usage_delta is not None and elapsed_seconds > 0:
        value = _clamp_percent(usage_delta / 1_000_000 / elapsed_seconds / cpu_count * 100)
        name = UNIT_CPU if dimension == "unit" else CGROUP_CPU
        records.append(
            _metric_record(
                name,
                value,
                streams,
                sample,
                plugin_id,
                collection_id,
                machine_id,
                boot_id,
                target=target,
                dimension=dimension,
            )
        )

    memory = values.get("memory_current")
    if _is_number(memory) and memory >= 0:
        name = UNIT_MEMORY if dimension == "unit" else CGROUP_MEMORY
        records.append(
            _metric_record(
                name,
                float(memory),
                streams,
                sample,
                plugin_id,
                collection_id,
                machine_id,
                boot_id,
                target=target,
                dimension=dimension,
            )
        )

    if elapsed_seconds > 0:
        read_delta = _delta(values.get("io_rbytes"), previous.get("io_rbytes"))
        write_delta = _delta(values.get("io_wbytes"), previous.get("io_wbytes"))
        read_name = UNIT_DISK_READ if dimension == "unit" else CGROUP_DISK_READ
        write_name = UNIT_DISK_WRITE if dimension == "unit" else CGROUP_DISK_WRITE
        if read_delta is not None:
            records.append(
                _metric_record(
                    read_name,
                    read_delta / elapsed_seconds,
                    streams,
                    sample,
                    plugin_id,
                    collection_id,
                    machine_id,
                    boot_id,
                    target=target,
                    dimension=dimension,
                )
            )
        if write_delta is not None:
            records.append(
                _metric_record(
                    write_name,
                    write_delta / elapsed_seconds,
                    streams,
                    sample,
                    plugin_id,
                    collection_id,
                    machine_id,
                    boot_id,
                    target=target,
                    dimension=dimension,
                )
            )
    return records


def _metric_record(
    name: str,
    value: float,
    streams: Mapping[str, tuple[str, str]],
    sample: _Sample,
    plugin_id: str,
    collection_id: str,
    machine_id: str,
    boot_id: str,
    *,
    target: str,
    dimension: str | None = None,
) -> dict[str, Any]:
    unit, base_stream = streams[name]
    if dimension is None:
        stream_id = base_stream
        entity = f"metric:{name}"
    else:
        stream_id = f"{base_stream}.{dimension}:{target}"
        entity = f"metric:{name}:{target}"
    record: dict[str, Any] = {
        "kind": RecordKind.METRIC.value,
        "machine_id": machine_id,
        "boot_id": boot_id,
        "collection_id": collection_id,
        "source_plugin": plugin_id,
        "stream_id": stream_id,
        "monotonic_ns": sample.monotonic_ns,
        "realtime_ns": sample.realtime_ns,
        "clock_domain": "boot_monotonic",
        "entity": entity,
        "payload": {"name": name, "value": round(value, 6), "unit": unit},
    }
    record["record_id"] = compute_record_id(record)
    return record


def _delta(current: Any, previous: Any) -> float | None:
    if not _is_number(current) or not _is_number(previous):
        return None
    difference = float(current) - float(previous)
    if difference < 0:
        return None
    return difference


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, value))


def metric_stream_entities(records: Sequence[Mapping[str, Any]]) -> list[MetricStreamEntity]:
    """从采集记录推出对应的 ``metric_stream`` 实体，供 Core 组装采集数据。"""
    entities: dict[str, MetricStreamEntity] = {}
    for record in records:
        if record.get("kind") != RecordKind.METRIC.value:
            continue
        entity_id = record.get("entity")
        payload = record.get("payload")
        if not isinstance(entity_id, str) or not isinstance(payload, Mapping):
            continue
        if entity_id in entities:
            continue
        entities[entity_id] = MetricStreamEntity(
            entity_type="metric_stream",
            entity_id=entity_id,
            machine_id=str(record["machine_id"]),
            boot_id=str(record["boot_id"]),
            name=str(payload["name"]),
            unit=str(payload["unit"]),
            available=True,
        )
    return [entities[key] for key in sorted(entities)]


__all__ = [
    "CAPABILITY",
    "CGROUP_CPU",
    "CGROUP_DISK_READ",
    "CGROUP_DISK_WRITE",
    "CGROUP_MEMORY",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_CGROUPS",
    "DEFAULT_WINDOW_SECONDS",
    "PLUGIN_ID",
    "SYSTEM_CPU",
    "SYSTEM_DISK_READ",
    "SYSTEM_DISK_WRITE",
    "SYSTEM_MEMORY",
    "UNIT_CPU",
    "UNIT_DISK_READ",
    "UNIT_DISK_WRITE",
    "UNIT_MEMORY",
    "ResourceCollector",
    "build_records",
    "metric_stream_entities",
    "parse_capture",
]
