"""OOM 与进程异常退出分析插件（任务 2.2）。

从 1.4 写入的 Journal 事件里认出两类 OOM 事实，并把它们关联到受影响的进程与 Unit：

- **Kernel OOM Killer**：`payload.message` 匹配
  `Out of memory: Killed process <pid> (<comm>)`，也认 `Memory cgroup out of memory:` 与旧版
  `Kill process` 写法。PID 与进程名来自这一行的 `MESSAGE`；Unit 来自同一行的
  `payload.attributes.unit` 或 `payload.attributes.fields._SYSTEMD_UNIT`。
- **systemd-oomd**：`payload.attributes.identifier` 是 `systemd-oomd`，且 `payload.message`
  里有 `Killed /<cgroup>`。被杀的 cgroup 来自这一行的 `MESSAGE`，路径最后一段就是 Unit。

两条硬规则，对应任务交接的验收要求：

1. **只有同一行日志里写出来的关联才算直接证据。** 结论里写明是哪个字段、哪条日志
   （`evidence_refs` 指向该 `event` 的 `record_id`，`message` 里写出字段名与原文片段）。
2. **跨日志、只靠时间接近得出的关联一律标成疑似。** 内核 OOM 那一行没有 Unit 字段时，才用
   同一 boot 内、窗口时长以内、结果为 `oom-kill` 的 Unit 失败日志去补 Unit；这种结论的
   `confidence` 是 ``CONFIDENCE_PROXIMITY``，`message` 里写明「只凭时间接近」与两者的时间差，
   不写成因果。

规则全部是确定的文本与字段匹配，不引入推断模型。同一批输入重复分析得到同一批
`record_id`（`record_id` 按记录内容计算），因此重复分析是幂等的，不会叠加互相矛盾的结论。

数据入口用 ``docs/plugin-contracts.md`` 第 5 节的同步协议 ``PluginDataAccess``：读用
``query_records``，写用 ``append_records``。插件不碰存储层、不开事件循环；拿到的不是同步入口
时抛可读的 ``PluginInputError``，而不是返回一个什么都没写的「成功」。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from system_analyzer.domain.bootrec import (
    AnnotationPayload,
    ClockDomain,
    Record,
    RecordKind,
    Severity,
    compute_record_id,
)
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    AnalysisRequest,
    AnalysisResult,
    PluginBase,
    PluginContext,
    PluginDataAccess,
    PluginInputError,
    PluginKind,
    PluginSpec,
)

PLUGIN_ID = "oom.analyzer"
CAPABILITY = "system.oom"
STREAM_ID = "analysis.oom"

ANNOTATION_TYPE_KERNEL = "oom.kernel.killed_process"
ANNOTATION_TYPE_SYSTEMD_OOMD = "oom.systemd_oomd.killed_cgroup"

CONFIG_PROXIMITY_WINDOW_NS = "proximity_window_ns"
DEFAULT_PROXIMITY_WINDOW_NS = 5_000_000_000
"""疑似关联的时间窗口：默认 5 秒。

systemd 在 OOM 之后紧接着写 `Failed with result 'oom-kill'`，间隔通常在数十毫秒以内；
窗口只决定「拿哪一条日志补 Unit」，不改变「这是疑似」这个结论。
"""

CONFIDENCE_UNIT_FROM_SAME_LINE = 0.95
"""Unit 由同一行日志的字段直接给出。"""

CONFIDENCE_KILL_ONLY = 0.9
"""OOM 事实本身来自该行日志，但该行没有任何 Unit 线索。"""

CONFIDENCE_PROXIMITY = 0.3
"""Unit 只靠时间接近补出来：疑似，不是因果。"""

OOM_KILL_RESULT = "oom-kill"
"""systemd 在单元因内核 OOM 被杀死后写下的 `Result=`。"""

_KERNEL_KILL = re.compile(
    r"(?:Out of memory|Memory cgroup out of memory):\s*"
    r"(?:Killed|Kill) process\s+(?P<pid>\d+)\s*\((?P<process>[^)]+)\)"
)
_OOMD_KILL = re.compile(r"\bKilled\s+(?P<cgroup>/\S+)")
_FAILED_RESULT = re.compile(r"Failed with result '(?P<result>[^']+)'")


@dataclass(frozen=True, slots=True)
class OomKill:
    """一条 OOM 事实：来源、受影响对象，以及它出自哪条记录、哪个字段。"""

    source: str
    record: Record
    pid: int | None
    process: str | None
    cgroup: str | None
    unit: str | None
    """同一行日志里直接给出的 Unit；为 ``None`` 表示这一行没有 Unit 线索。"""

    unit_field: str | None
    """给出 Unit 的字段名（给人看的证据名），例如 ``payload.attributes.unit``。"""

    quote: str
    """触发识别的原文片段，写进结论的 `message` 供人工核对。"""


class OomAnalyzer(PluginBase):
    """OOM 分析插件。无状态：配置与数据入口每次调用时由 ``PluginContext`` 传入。"""

    spec = PluginSpec(
        id=PLUGIN_ID,
        kind=PluginKind.ANALYZER,
        name="OOM 与进程异常退出分析",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=(CAPABILITY,),
        config_schema={
            "type": "object",
            "properties": {
                CONFIG_PROXIMITY_WINDOW_NS: {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "跨日志补 Unit 时允许的时间窗口（纳秒）；只用于把只靠时间接近的关联"
                        f"标成疑似，默认 {DEFAULT_PROXIMITY_WINDOW_NS}。"
                    ),
                }
            },
            "additionalProperties": False,
        },
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        """只读 Journal 事件，把 OOM 结论以 ``annotation`` 记录写回。"""
        data = context.data
        if not isinstance(data, PluginDataAccess):
            raise PluginInputError(
                "分析需要 context.data 提供同步的 query_records 与 append_records"
                f"（docs/plugin-contracts.md 第 5 节），实际拿到的是 {type(data).__name__}"
            )
        events = data.query_records(record_kinds=(RecordKind.EVENT.value,))
        _reject_coroutine(events, "query_records")
        annotations = plan_annotations(
            list(events),
            boot_id=request.boot_id,
            record_ids=request.record_ids,
            window_ns=_window_ns(context.config),
            analyzer=self.spec.id,
        )
        if not annotations:
            return AnalysisResult(annotations_written=0)
        written = data.append_records(
            annotations,
            plugin_id=self.spec.id,
            source=context.run_id or "analysis",
        )
        _reject_coroutine(written, "append_records")
        return AnalysisResult(annotations_written=written)


# ---------------------------------------------------------------------------
# 规则：从一条事件认出 OOM 事实
# ---------------------------------------------------------------------------


def detect_kill(record: Record) -> OomKill | None:
    """这条事件是不是一条 OOM 事实；是就给出受影响对象与证据字段。"""
    message = _message(record)
    if not message:
        return None

    kernel = _KERNEL_KILL.search(message)
    if kernel is not None:
        unit = _field(record, "unit", "_SYSTEMD_UNIT")
        return OomKill(
            source="kernel",
            record=record,
            pid=int(kernel.group("pid")),
            process=kernel.group("process").strip(),
            cgroup=_field(record, "cgroup", "_SYSTEMD_CGROUP"),
            # 内核那一行由内核写，通常不带 Unit 字段（`_TRANSPORT=kernel`，没有 cgroup 归属）。
            # 这一行确实带了 Unit 时它就是同一行的直接证据；没带就留空，由调用方按时间接近补。
            unit=unit,
            unit_field="payload.attributes.unit（原始字段 _SYSTEMD_UNIT）" if unit else None,
            quote=kernel.group(0),
        )

    identifier = _field(record, "identifier", "SYSLOG_IDENTIFIER")
    if identifier != "systemd-oomd":
        return None
    oomd = _OOMD_KILL.search(message)
    if oomd is None:
        return None
    cgroup = oomd.group("cgroup")
    return OomKill(
        source="systemd-oomd",
        record=record,
        # systemd-oomd 的 Killed 行只写 cgroup，不写 PID 与进程名：不猜，写空。
        pid=None,
        process=None,
        cgroup=cgroup,
        unit=_unit_of_cgroup(cgroup),
        unit_field="payload.message",
        quote=oomd.group(0),
    )


def plan_annotations(
    records: Sequence[Record],
    *,
    boot_id: str = "",
    record_ids: Sequence[str] = (),
    window_ns: int = DEFAULT_PROXIMITY_WINDOW_NS,
    analyzer: str = PLUGIN_ID,
) -> list[dict[str, Any]]:
    """把一批事件变成待写入的 ``annotation`` 记录字典。

    纯函数：同样的输入得到同样的输出（含每条的 ``record_id``），不读写任何外部状态。
    ``boot_id`` 为空表示分析传入的全部记录；``record_ids`` 非空时只把这些记录当作 OOM 证据，
    但找 Unit 线索时仍看整个 boot（跨 boot 关联按 ``docs/time-base.md`` 第 3.2 节禁止）。
    """
    groups: dict[tuple[str, str], list[Record]] = {}
    for record in records:
        if record.kind is not RecordKind.EVENT:
            continue
        groups.setdefault((record.machine_id, record.boot_id), []).append(record)

    wanted = set(record_ids)
    annotations: list[dict[str, Any]] = []
    for (_machine_id, group_boot_id), events in sorted(groups.items()):
        if boot_id and group_boot_id != boot_id:
            continue
        entities = {record.entity for record in events if record.entity}
        kills = []
        for record in events:
            if not _allowed(record, wanted):
                continue
            kill = detect_kill(record)
            if kill is not None:
                kills.append(kill)
        for kill in sorted(kills, key=_kill_order):
            annotations.append(
                _annotation(
                    kill,
                    events=events,
                    entities=entities,
                    window_ns=window_ns,
                    analyzer=analyzer,
                )
            )
    return annotations


def _allowed(record: Record, wanted: set[str]) -> bool:
    """``record_ids`` 为空表示不限定；否则只认列出的记录。"""
    return not wanted or record.record_id in wanted


def _kill_order(kill: OomKill) -> tuple[int, str]:
    """按时间排序，同一时刻按 ``record_id`` 排，保证结果是确定的顺序。"""
    return (kill.record.monotonic_ns or 0, kill.record.record_id)


# ---------------------------------------------------------------------------
# 关联：Unit 与实体
# ---------------------------------------------------------------------------


def _nearest_unit_failure(
    events: Sequence[Record], kill: OomKill, window_ns: int
) -> tuple[Record, str, int] | None:
    """窗口内最近的一条「因 OOM 而失败」的 Unit 日志，用于补 Unit。

    只按时间接近挑选，因此调用方必须把结果标成疑似。比较只在 ``boot_monotonic`` 时钟域内进行
    （``docs/time-base.md`` 第 3.2 节）：另一条记录没有单调时钟读数时直接跳过。
    """
    start = kill.record.monotonic_ns
    if start is None:
        return None
    best: tuple[Record, str, int] | None = None
    for record in events:
        if record.record_id == kill.record.record_id:
            continue
        if record.clock_domain is not ClockDomain.BOOT_MONOTONIC or record.monotonic_ns is None:
            continue
        delta = record.monotonic_ns - start
        if delta < 0 or delta > window_ns:
            continue
        unit = _field(record, "unit", "_SYSTEMD_UNIT")
        if unit is None:
            continue
        failed = _FAILED_RESULT.search(_message(record) or "")
        if failed is None or failed.group("result") != OOM_KILL_RESULT:
            continue
        if best is None or delta < best[2]:
            best = (record, unit, delta)
    return best


def _annotation(
    kill: OomKill,
    *,
    events: Sequence[Record],
    entities: set[str],
    window_ns: int,
    analyzer: str,
) -> dict[str, Any]:
    """一条 OOM 事实变成一条 annotation 记录。"""
    unit = kill.unit
    unit_field = kill.unit_field
    severity = Severity.ERROR
    confidence = CONFIDENCE_UNIT_FROM_SAME_LINE if unit else CONFIDENCE_KILL_ONLY
    evidence_refs = [kill.record.record_id]
    parts = [_kill_sentence(kill)]

    if unit is None:
        guessed = _nearest_unit_failure(events, kill, window_ns)
        if guessed is None:
            parts.append(
                "这条日志没有 Unit 字段，时间窗口内也没有结果为 oom-kill 的 Unit 失败日志，"
                "因此不给出 Unit 归属。"
            )
        else:
            record, unit, delta = guessed
            confidence = CONFIDENCE_PROXIMITY
            evidence_refs.append(record.record_id)
            parts.append(
                f"疑似归属 Unit {unit}：同一启动内的 event 记录 {record.record_id}"
                f"（monotonic_ns={record.monotonic_ns}，比本条晚 {delta} 纳秒，"
                f"窗口 {window_ns} 纳秒）的 payload.message 含"
                f" \"Failed with result '{OOM_KILL_RESULT}'\"，"
                f"且 payload.attributes.unit={unit}。这条关联只凭时间接近，"
                "不能当作因果结论。"
            )
            unit_field = "payload.attributes.unit（跨日志，仅时间接近）"

    parts.append(_evidence_sentence(kill, unit=unit, unit_field=unit_field))

    entity = f"service:{unit}" if unit and f"service:{unit}" in entities else None
    if unit and entity is None:
        parts.append(f"库里没有 service:{unit} 实体，因此这条结论不挂实体。")

    payload = AnnotationPayload(
        annotation_type=(
            ANNOTATION_TYPE_KERNEL if kill.source == "kernel" else ANNOTATION_TYPE_SYSTEMD_OOMD
        ),
        analyzer=analyzer,
        severity=severity,
        message="".join(parts),
        confidence=confidence,
        evidence_refs=evidence_refs,
    ).model_dump(mode="json")
    body: dict[str, Any] = {
        "kind": RecordKind.ANNOTATION.value,
        "machine_id": kill.record.machine_id,
        "boot_id": kill.record.boot_id,
        "collection_id": kill.record.collection_id,
        "source_plugin": analyzer,
        "stream_id": STREAM_ID,
        "monotonic_ns": kill.record.monotonic_ns,
        "realtime_ns": kill.record.realtime_ns,
        "clock_domain": kill.record.clock_domain.value,
        "entity": entity,
        "payload": payload,
    }
    return {"record_id": compute_record_id(body), **body}


def _kill_sentence(kill: OomKill) -> str:
    if kill.source == "kernel":
        sentence = f"Kernel OOM Killer 杀死了进程 {kill.pid}（{kill.process}）"
        if kill.cgroup:
            sentence += f"，进程所在 cgroup 是 {kill.cgroup}"
        return sentence + "。"
    return f"systemd-oomd 杀死了 cgroup {kill.cgroup}（Unit {kill.unit}）。"


def _evidence_sentence(kill: OomKill, *, unit: str | None, unit_field: str | None) -> str:
    """把「哪个字段、哪行日志」写清楚，人工核对时不用再翻原文。"""
    record = kill.record
    sentences = [
        f"依据：event 记录 {record.record_id}（monotonic_ns={record.monotonic_ns}）的 "
        f'payload.message 含 "{kill.quote}"'
    ]
    if kill.source == "systemd-oomd":
        sentences.append("、payload.attributes.identifier=systemd-oomd")
    elif kill.record.payload.get("attributes", {}).get("fields", {}).get("_TRANSPORT"):
        transport = kill.record.payload["attributes"]["fields"]["_TRANSPORT"]
        sentences.append(f"、payload.attributes.fields._TRANSPORT={transport}")
    if unit is not None and unit_field is not None:
        sentences.append(f"；Unit {unit} 由{unit_field}给出")
    if kill.pid is not None:
        sentences.append(f"；PID {kill.pid} 与进程名 {kill.process} 由 payload.message 同一行给出")
    elif kill.source == "systemd-oomd":
        sentences.append("；该行不记录受影响进程的 PID 与进程名，因此不给出进程归属")
    return "".join(sentences) + "。"


# ---------------------------------------------------------------------------
# 读 Journal 字段
# ---------------------------------------------------------------------------


def _message(record: Record) -> str | None:
    message = record.payload.get("message")
    return message if isinstance(message, str) and message else None


def _field(record: Record, name: str, journal_name: str) -> str | None:
    """先取 1.4 归一化后的 ``attributes.<name>``，再退回原始 ``fields.<_NAME>``。"""
    attributes = record.payload.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    value = attributes.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    fields = attributes.get("fields")
    if isinstance(fields, Mapping):
        value = fields.get(journal_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _unit_of_cgroup(cgroup: str) -> str | None:
    """cgroup 路径最后一段就是 Unit 名。

    例如 ``/system.slice/nginx.service`` → ``nginx.service``。
    """
    unit = cgroup.rsplit("/", 1)[-1]
    return unit or None


def _window_ns(config: Mapping[str, Any]) -> int:
    value = config.get(CONFIG_PROXIMITY_WINDOW_NS, DEFAULT_PROXIMITY_WINDOW_NS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PluginInputError(
            f"配置 {CONFIG_PROXIMITY_WINDOW_NS} 必须是不小于 0 的整数纳秒数，实际是 {value!r}"
        )
    return value


def _reject_coroutine(result: Any, method: str) -> None:
    """数据入口返回协程时明确失败，而不是让调用方拿到一个什么都没写的协程。

    与 1.3 / 1.4 的判断相同：插件的入口是同步方法，数据入口也必须同步。
    """
    if not inspect.isawaitable(result):
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()
    raise PluginInputError(
        f"数据入口 {method} 返回了协程，分析需要同步入口；"
        "Core 应把存储层包成同步的 query_records / append_records 后写入 context.data"
    )


__all__ = [
    "ANNOTATION_TYPE_KERNEL",
    "ANNOTATION_TYPE_SYSTEMD_OOMD",
    "CONFIG_PROXIMITY_WINDOW_NS",
    "DEFAULT_PROXIMITY_WINDOW_NS",
    "OomAnalyzer",
    "OomKill",
    "detect_kill",
    "plan_annotations",
]
