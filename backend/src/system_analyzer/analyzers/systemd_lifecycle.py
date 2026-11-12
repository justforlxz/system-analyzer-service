"""从已入库的 Journal 事件重建 systemd Unit 的生命周期（任务 2.1）。

分析规则与判定依据见 ``docs/systemd-lifecycle-analysis.md``，本模块是它的可执行版本。模块只做
「读统一记录 → 推导区间与结论」，不写库、不 import 存储层：写库与读库都由插件
``systemd_lifecycle_analyzer`` 经同步数据入口完成。

输入是任务 1.4 写进库的 ``journal.entry`` 事件记录——原始字段留在
``payload.attributes.fields``，``record_id`` 是内容摘要，因此每条结论都能指回它。

输出落在 0.2 定稿的两种既有对象上，不新增对象类型：

- ``span``：``span_type`` 为 ``service.activating``（激活阶段）与 ``service.active``（运行阶段），
  实体是 ``service:<unit>``。结束时刻缺失时写 ``null``（``docs/time-base.md`` 第 6 节的开放区间），
  不补全、不用「下一段激活的开始时刻」冒充本段结束；
- ``annotation``：``systemd.service_failed``、``systemd.startup_failure``、
  ``systemd.abnormal_exit``、``systemd.lifecycle_incomplete``，``evidence_refs`` 指向原始事件记录。

生命周期按 ``(collection_id, unit)`` 分段，不跨采集拼接：``docs/time-base.md`` 第 6 节规定开放区间
只在其所属 collection 的窗口内有效，下一次采集若又观测到同一个 Unit，按新记录新开一段。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from system_analyzer.domain.bootrec import (
    ClockDomain,
    EntityType,
    GenericEntity,
    Record,
    RecordKind,
    compute_record_id,
)

PLUGIN_ID = "systemd.lifecycle"
"""插件 id，同时是 Annotation 的 ``analyzer`` 字段：产生结论的分析器就是本插件。"""

STREAM_SPANS = "systemd.units"
"""生命周期区间的逻辑流名，与 0.2 的样例 ``docs/samples/bootrec-v1`` 保持一致。"""

STREAM_ANNOTATIONS = "analysis.systemd"
"""分析结论的逻辑流名。"""

SPAN_ACTIVATING = "service.activating"
"""激活阶段： ``Starting`` 到 ``Started``（或激活期间的失败）。"""

SPAN_ACTIVE = "service.active"
"""运行阶段： ``Started`` 到 ``Stopped``／失败，未观测到结束时刻时为开放区间。"""

ANNOTATION_SERVICE_FAILED = "systemd.service_failed"
"""Unit 进入 failed 状态。"""

ANNOTATION_STARTUP_FAILURE = "systemd.startup_failure"
"""启动失败：激活阶段还没完成就结束了。"""

ANNOTATION_ABNORMAL_EXIT = "systemd.abnormal_exit"
"""主进程非正常退出（``Main process exited`` 的 status 非 0，或 code 不是 exited）。"""

ANNOTATION_LIFECYCLE_INCOMPLETE = "systemd.lifecycle_incomplete"
"""证据不足，生命周期不完整；消息固定含「生命周期不完整」四个字。"""

ANNOTATION_UNATTRIBUTED = "systemd.lifecycle_unattributed"
"""有生命周期消息无法归属到任何 Unit，因此没有产出结论；只报数量、原因与证据。

「认得出是生命周期消息」与「知道它在说哪个 Unit」是两件事。只有前者时不能猜：把消息记到
``init.scope`` 或 ``user@1000.service`` 会产出一份看起来完整、实际是伪造的生命周期，读者无法
从数据里看出它是错的。这条结论让「这份数据看不出是哪个服务」变得可见、可查。
"""

JOURNAL_EVENT_TYPE = "journal.entry"
"""任务 1.4 写给每条 Journal 条目的 ``event_type``。"""

LIFECYCLE_IDENTIFIER = "systemd"
"""生命周期日志的 ``SYSLOG_IDENTIFIER``。

systemd 的生命周期消息由 PID 1 自己写，标识符固定是 ``systemd``。只认这个标识符是为了避免把
业务程序自己的日志（例如某个进程打印 ``Started worker pool``）当成 Unit 的生命周期：判断错一个
「启动时刻」，整段区间就是编造的。
"""

OBJECT_UNIT_FIELDS: tuple[str, ...] = ("UNIT", "USER_UNIT")
"""生命周期消息里「说的是哪个 Unit」的结构化字段。

systemd 从 v250 起用 ``log_struct`` 打印生命周期消息，把被操作的 Unit 放在 ``UNIT=``（系统
实例）或 ``USER_UNIT=``（用户实例）里；``_SYSTEMD_UNIT`` 仍然是写日志的进程所属的 Unit
（PID 1 是 ``init.scope``，用户实例是 ``user@N.service``）。先读这两个字段，真实 Journal 才
能重建出真正的 Unit。
"""

OBJECT_INVOCATION_FIELDS: tuple[str, ...] = ("INVOCATION_ID", "USER_INVOCATION_ID")
"""与 ``OBJECT_UNIT_FIELDS`` 对应的「这次激活」编号字段。"""


MAX_UNATTRIBUTED_EVIDENCE = 20
"""``systemd.lifecycle_unattributed`` 里最多列出的原始记录条数。

数量本身写进消息正文；证据只列前若干条，避免一次退化输入（几万条消息）把一条结论撑成几百 KB。
"""
SENDER_UNIT_FIELDS: tuple[str, ...] = ("_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT")
"""写日志的进程所属的 Unit。

只有它等于被分析的 Unit 时，``_PID`` 与 ``_SYSTEMD_CGROUP`` 才属于该 Unit；否则它们是写日志
那个进程（systemd 自己）的标识，算在服务头上就是错误关联。
"""

MISSING_ACTIVATION_START = "missing_activation_start"
MISSING_STARTED = "missing_started"
MISSING_TERMINATION = "missing_termination"
OUT_OF_ORDER_EVIDENCE = "out_of_order_evidence"

INCOMPLETE_REASON_TEXT: Mapping[str, str] = {
    MISSING_ACTIVATION_START: "缺少启动（Starting）记录，激活开始时刻未知",
    MISSING_STARTED: "缺少启动完成（Started）记录，运行开始时刻未知",
    MISSING_TERMINATION: "缺少停止或失败记录，区间结束时刻未知，按未闭合处理",
    OUT_OF_ORDER_EVIDENCE: "失败或停止证据早于 Started，顺序异常，没有按时间补全",
}
"""证据缺口的中文说明，写进 ``systemd.lifecycle_incomplete`` 的 ``message``。"""

STATE_ORDER: Mapping[str, int] = {
    "activating": 0,
    "active": 1,
    "inactive": 2,
    "failed": 2,
    "unknown": 3,
}
"""同一时刻出现多个状态观测时的优先级：终态压过运行、运行压过激活。"""

_FAILED_RESULT = re.compile(r"Failed with result '([^']*)'")
_MAIN_EXIT = re.compile(r"Main process exited, code=(\w+), status=(\S+)")
_MANAGER_SCOPE_UNIT = re.compile(r"\A(?:init\.scope|user@[^.]*\.service)\Z")
"""systemd 自己（PID 1 与 user manager）所在的 Unit。

这几个值出现在 ``_SYSTEMD_UNIT`` / ``_SYSTEMD_USER_UNIT`` 上时只说明「这条日志是 systemd 打的」，
不说明消息说的是哪个 Unit。拿不到 ``UNIT=`` / ``USER_UNIT=`` 而发送方又落在这里时，就没有任何
可信依据判断归属，只能不产出结论。
"""


class SignalKind(StrEnum):
    """从 Journal 消息里识别出的生命周期信号。"""

    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ACTIVATION_FAILED = "activation_failed"
    FAILED_RESULT = "failed_result"
    MAIN_EXIT = "main_exit"


TERMINAL_KINDS: frozenset[SignalKind] = frozenset(
    {SignalKind.STOPPED, SignalKind.ACTIVATION_FAILED, SignalKind.FAILED_RESULT}
)
"""会让一段激活结束的信号：停止、启动失败、Unit 进入 failed。"""


@dataclass(frozen=True, slots=True)
class JournalSignal:
    """一条可用的生命周期证据：一条 Journal 事件记录，加上识别出的信号。"""

    kind: SignalKind
    record_id: str
    collection_id: str
    machine_id: str
    boot_id: str
    entity: str
    unit: str
    message: str
    monotonic_ns: int
    realtime_ns: int | None
    invocation_id: str | None
    pid: int | None
    cgroup: str | None
    result: str | None = None
    exit_code: str | None = None
    exit_status: str | None = None

    @property
    def abnormal(self) -> bool:
        """主进程退出是否属于非正常退出。"""
        if self.kind is not SignalKind.MAIN_EXIT:
            return False
        return _exit_is_abnormal(self.exit_code or "", self.exit_status or "")


@dataclass(frozen=True, slots=True)
class _DroppedMessage:
    """认得出是生命周期消息、却归属不到任何 Unit 的一条原始记录。"""

    collection_id: str
    machine_id: str
    boot_id: str
    record_id: str
    monotonic_ns: int
    realtime_ns: int | None


@dataclass(frozen=True, slots=True)
class UnitSegment:
    """一次激活的生命周期区间，连同它的证据。

    五个时刻里任何一个为 ``None`` 都表示「这次分析没有拿到那个时刻的证据」，不是 0，
    也不代表另一个时刻也缺失。
    """

    unit: str
    entity: str
    collection_id: str
    machine_id: str
    boot_id: str
    signals: tuple[JournalSignal, ...]
    invocation_id: str | None
    pid: int | None
    cgroup: str | None
    activation_start: JournalSignal | None
    activation_end: JournalSignal | None
    active_start: JournalSignal | None
    active_end: JournalSignal | None
    terminal_state: str | None
    failure: JournalSignal | None
    abnormal_exit: JournalSignal | None
    incomplete_reasons: tuple[str, ...]

    @property
    def start_ns(self) -> int:
        """这一段的第一个证据时刻，用于按时间点查询与稳定排序。"""
        return self.signals[0].monotonic_ns

    @property
    def activation_start_ns(self) -> int | None:
        return None if self.activation_start is None else self.activation_start.monotonic_ns

    @property
    def activation_end_ns(self) -> int | None:
        return None if self.activation_end is None else self.activation_end.monotonic_ns

    @property
    def active_start_ns(self) -> int | None:
        return None if self.active_start is None else self.active_start.monotonic_ns

    @property
    def active_end_ns(self) -> int | None:
        return None if self.active_end is None else self.active_end.monotonic_ns

    @property
    def startup_duration_ns(self) -> int | None:
        """启动耗时：激活开始到激活结束（``Started`` 或激活期间的失败）。

        两个时刻都拿到才有值；缺一个就返回 ``None``，不用别的记录估一个数出来。
        """
        start = self.activation_start_ns
        end = self.activation_end_ns
        if start is None or end is None:
            return None
        return end - start

    @property
    def complete(self) -> bool:
        """这一段是否有足够证据说清「怎么开始、怎么结束」。"""
        return not self.incomplete_reasons


@dataclass(frozen=True, slots=True)
class LifecycleConclusion:
    """一条分析结论，对应一条 ``annotation`` 记录。"""

    annotation_type: str
    unit: str
    entity: str
    collection_id: str
    machine_id: str
    boot_id: str
    severity: str
    message: str
    evidence_refs: tuple[str, ...]
    monotonic_ns: int
    realtime_ns: int | None


@dataclass(frozen=True, slots=True)
class UnattributedLifecycle:
    """一个采集里「认得出是生命周期消息、却归属不到任何 Unit」的那些原始记录。

    它们没有产出任何区间与结论，只在 ``systemd.lifecycle_unattributed`` 里报出数量与原因：
    读者要能从结果里区分「这台机器没有这些服务」与「这份数据看不出是哪个服务」。
    """

    collection_id: str
    machine_id: str
    boot_id: str
    monotonic_ns: int
    realtime_ns: int | None
    record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StartupDuration:
    """一次激活的启动耗时，供「启动耗时」查询与测试断言使用。"""

    unit: str
    invocation_id: str | None
    collection_id: str
    start_ns: int | None
    end_ns: int | None
    duration_ns: int | None


@dataclass(frozen=True, slots=True)
class UnitStateAt:
    """某个时间点上，一个 Unit 的状态。"""

    unit: str
    state: str
    """``activating`` / ``active`` / ``inactive`` / ``failed`` / ``unknown``。"""

    invocation_id: str | None
    state_since_ns: int | None
    """当前状态开始于哪个时刻；来自证据，不是推算。"""

    evidence_ns: int | None
    """得出这个状态所依据的证据时刻。"""

    stale: bool
    """``True`` 表示证据在这个时刻之前就结束了：状态是「最后一次观测到的」，不是实时读数。"""

    complete: bool
    """覆盖该时刻的那一段生命周期是否完整。"""


@dataclass(frozen=True, slots=True)
class LifecycleReconstruction:
    """一次分析的全部结果：区间、结论，以及按时间点查询状态的能力。"""

    machine_id: str
    boot_id: str
    segments: tuple[UnitSegment, ...]
    conclusions: tuple[LifecycleConclusion, ...]
    unattributed: tuple[UnattributedLifecycle, ...]

    def units(self) -> tuple[str, ...]:
        return tuple(sorted({segment.unit for segment in self.segments}))

    def startup_durations(self) -> tuple[StartupDuration, ...]:
        """每一段激活的启动耗时。

        缺证据的那一段也列出来，``duration_ns`` 写 ``None``：把「没有启动耗时的证据」显示成
        「这一项不存在」，读的人就无法区分「这次启动没有记录到」与「根本没测」。
        """
        return tuple(
            StartupDuration(
                unit=segment.unit,
                invocation_id=segment.invocation_id,
                collection_id=segment.collection_id,
                start_ns=segment.activation_start_ns,
                end_ns=segment.activation_end_ns,
                duration_ns=segment.startup_duration_ns,
            )
            for segment in self.segments
        )

    def state_at(self, monotonic_ns: int) -> tuple[UnitStateAt, ...]:
        """按指定时间点（boot 内纳秒）查询各 Unit 的状态。

        同一个 boot 的所有采集共用一条单调时钟轴（``docs/time-base.md`` 第 7 节），所以这里把
        该 Unit 在**所有采集**里的状态变化时刻合到一起，取不晚于查询时刻的最后一个：
        激活开始 → ``activating``，运行开始 → ``active``，终态信号 → ``inactive`` / ``failed``。
        只看其中某一次采集会得出与其它采集的证据矛盾的答案。

        ``stale`` 表示「这个状态之后再也没有任何证据」：开放区间在日志用尽之后也是 ``true``，
        调用方据此区分「现在仍在运行」与「日志到这里就没有了」。

        查询时刻之前该 Unit 没有任何证据时不出现在结果里——缺证据时不说「没在运行」，那会把
        「没观测到」说成「不存在」。
        """
        answers: list[UnitStateAt] = []
        for unit in self.units():
            observations = [
                observation
                for segment in self.segments
                if segment.unit == unit
                for observation in _observations_of(segment)
            ]
            if not observations:
                continue
            known = [item for item in observations if item.monotonic_ns <= monotonic_ns]
            if not known:
                continue
            chosen = max(
                known,
                key=lambda item: (
                    item.monotonic_ns,
                    STATE_ORDER[item.state],
                    item.collection_id,
                ),
            )
            last_evidence = max(item.monotonic_ns for item in observations)
            answers.append(
                UnitStateAt(
                    unit=unit,
                    state=chosen.state,
                    invocation_id=chosen.invocation_id,
                    state_since_ns=chosen.monotonic_ns,
                    evidence_ns=chosen.monotonic_ns,
                    stale=last_evidence <= monotonic_ns,
                    complete=chosen.complete,
                )
            )
        return tuple(answers)

    def service_entities(self) -> tuple[GenericEntity, ...]:
        """本次分析引用到的 service 实体。

        记录里的 ``entity`` 必须是已存在的实体（0.2 第 6 节）；真实 Journal 的导入只为
        ``_SYSTEMD_UNIT`` 建过实体，而生命周期消息的 ``_SYSTEMD_UNIT`` 是 PID 1 的
        ``init.scope``，所以被分析的 Unit 大多没有实体。分析产出这些定义，由数据入口保证它们
        写入本次启动的实体集合。
        """
        return tuple(
            GenericEntity(
                entity_type=EntityType.SERVICE,
                entity_id=_entity_of(unit),
                machine_id=self.machine_id,
                boot_id=self.boot_id,
                name=unit,
                attributes={"unit": unit},
            )
            for unit in self.units()
        )

    def span_records(self) -> tuple[dict[str, Any], ...]:
        """生命周期区间记录（``kind = span``），``record_id`` 按 0.2 第 8 节算。"""
        return tuple(
            record for segment in self.segments for record in _segment_span_records(segment)
        )

    def unattributed_records(self) -> tuple[dict[str, Any], ...]:
        """「归属不到任何 Unit」的汇总结论：每个涉及的采集一条。

        只报数量、原因与证据，不产出任何区间：这些消息没被归到任何服务上，也没有被补成完整区间。
        """
        return tuple(_unattributed_record(item) for item in self.unattributed)

    def annotation_records(self) -> tuple[dict[str, Any], ...]:
        """分析结论记录（``kind = annotation``），``record_id`` 按 0.2 第 8 节算。"""
        return (
            tuple(_annotation_record(conclusion) for conclusion in self.conclusions)
            + self.unattributed_records()
        )

    def records(self) -> tuple[dict[str, Any], ...]:
        """本次分析要写入的全部记录，区间在前、结论在后。"""
        return self.span_records() + self.annotation_records()


# ----------------------------------------------------------------------
# 识别生命周期信号
# ----------------------------------------------------------------------


def journal_signals(
    records: Iterable[Record | Mapping[str, Any]],
) -> tuple[JournalSignal, ...]:
    """从统一记录里挑出生命周期证据，按 ``(collection_id, 时刻, record_id)`` 排序。

    只看 ``journal.entry`` 事件、只看带单调时钟的记录（``clock_domain = boot_monotonic``，
    ``docs/time-base.md`` 第 2 节：只有墙钟的读数不能直接放进 boot 时间轴）、只认标识符是
    ``systemd`` 的消息。认不出来的消息原样跳过，不猜；认得出但归属不到 Unit 的消息也不出现在
    结果里，它们由 ``journal_scan`` 单独报出来。
    """
    return journal_scan(records)[0]


def journal_scan(
    records: Iterable[Record | Mapping[str, Any]],
) -> tuple[tuple[JournalSignal, ...], tuple[UnattributedLifecycle, ...]]:
    """把一批统一记录扫成「可归属的生命周期证据」与「归属不到 Unit 的消息」两堆。

    分开是必须的：前者能推出区间与结论，后者只能报数量与原因。把后者塞进前者就是伪造。
    """
    signals: list[JournalSignal] = []
    dropped: dict[str, list[_DroppedMessage]] = {}
    for raw in records:
        record = raw if isinstance(raw, Record) else Record.model_validate(raw)
        found = _classify_record(record)
        if found is None:
            continue
        if isinstance(found, _DroppedMessage):
            dropped.setdefault(found.collection_id, []).append(found)
            continue
        signals.append(found)
    signals.sort(key=lambda item: (item.collection_id, item.monotonic_ns, item.record_id))
    return tuple(signals), tuple(
        UnattributedLifecycle(
            collection_id=collection_id,
            machine_id=items[0].machine_id,
            boot_id=items[0].boot_id,
            monotonic_ns=items[0].monotonic_ns,
            realtime_ns=items[0].realtime_ns,
            record_ids=tuple(item.record_id for item in items),
        )
        for collection_id, items in sorted(dropped.items())
    )


def _classify_record(record: Record) -> JournalSignal | _DroppedMessage | None:
    """把一条统一记录分成三种：生命周期证据、认得出但归属不到 Unit 的消息、与本次分析无关。"""
    if record.kind is not RecordKind.EVENT:
        return None
    if record.monotonic_ns is None:
        return None
    payload = record.payload or {}
    attributes = payload.get("attributes") or {}
    if (
        payload.get("event_type") != JOURNAL_EVENT_TYPE
        and attributes.get("derived_from") != "journal"
    ):
        return None
    fields = attributes.get("fields") or {}
    identifier = _text(fields.get("SYSLOG_IDENTIFIER")) or _text(attributes.get("identifier"))
    if identifier != LIFECYCLE_IDENTIFIER:
        return None
    classified = classify_message(payload.get("message") or "")
    if classified is None:
        return None
    kind, detail = classified
    if kind is SignalKind.MAIN_EXIT and not _exit_is_abnormal(
        detail.get("exit_code") or "", detail.get("exit_status") or ""
    ):
        # 主进程正常退出不是「异常退出」，也不是停止证据（停止由 Stopped／Deactivated 表达）
        return None
    unit = _object_unit(fields, attributes)
    if unit is None:
        # 认得出是生命周期消息，却没有可信依据判断它在说哪个 Unit：不猜、不产出结论，
        # 由上层把数量、原因与原始记录报出来（见 ANNOTATION_UNATTRIBUTED）。
        return _DroppedMessage(
            collection_id=record.collection_id,
            machine_id=record.machine_id,
            boot_id=record.boot_id,
            record_id=record.record_id,
            monotonic_ns=record.monotonic_ns,
            realtime_ns=record.realtime_ns,
        )
    # PID、cgroup 与 _SYSTEMD_INVOCATION_ID 描述的是「谁写了这条日志」。systemd 打印生命周期
    # 消息时写日志的是 PID 1（或 user manager），这些字段指向 init.scope / user@1000.service，
    # 不是被分析的 Unit；只有当日志的发送方就是该 Unit 时才把它们算在它头上。
    from_unit_itself = unit in {
        value for name in SENDER_UNIT_FIELDS if (value := _text(fields.get(name)))
    }
    invocation_id = next(
        (value for name in OBJECT_INVOCATION_FIELDS if (value := _text(fields.get(name)))),
        None,
    )
    if invocation_id is None and from_unit_itself:
        invocation_id = _text(attributes.get("invocation_id")) or _text(
            fields.get("_SYSTEMD_INVOCATION_ID")
        )
    return JournalSignal(
        kind=kind,
        record_id=record.record_id,
        collection_id=record.collection_id,
        machine_id=record.machine_id,
        boot_id=record.boot_id,
        entity=_entity_of(unit),
        unit=unit,
        message=payload.get("message") or "",
        monotonic_ns=record.monotonic_ns,
        realtime_ns=record.realtime_ns,
        invocation_id=invocation_id,
        pid=_int(fields.get("_PID")) if from_unit_itself else None,
        cgroup=_text(fields.get("_SYSTEMD_CGROUP")) if from_unit_itself else None,
        result=detail.get("result"),
        exit_code=detail.get("exit_code"),
        exit_status=detail.get("exit_status"),
    )


def _object_unit(fields: Mapping[str, Any], attributes: Mapping[str, Any]) -> str | None:
    """这条日志说的是哪个 Unit；拿不到可信依据时返回 ``None``。

    ``UNIT=`` / ``USER_UNIT=`` 是 systemd 明确写下的「被操作的 Unit」，永远优先。拿不到它们时，
    只能退到发送方标识（``_SYSTEMD_USER_UNIT`` / ``_SYSTEMD_UNIT``，导入时也归一化进
    ``attributes["unit"]``）——而 systemd 自己打印生命周期消息时，发送方是 PID 1（``init.scope``）
    或 user manager（``user@<uid>.service``）。这些作用域只说明「日志是谁打的」，不说明「说的是哪个
    Unit」，此时返回 ``None``：宁可没有结论，也不把这些消息记到 systemd 自己头上。
    """
    for name in OBJECT_UNIT_FIELDS:
        value = _text(fields.get(name))
        if value is not None:
            return value
    for candidate in (
        _text(fields.get("_SYSTEMD_USER_UNIT")),
        _text(attributes.get("unit")),
        _text(fields.get("_SYSTEMD_UNIT")),
    ):
        if candidate is not None and not _is_manager_scope(candidate):
            return candidate
    return None


def _is_manager_scope(unit: str) -> bool:
    """这个 Unit 是不是 systemd 自己的作用域（PID 1 或 user manager）。"""
    return _MANAGER_SCOPE_UNIT.match(unit) is not None


def classify_message(message: str) -> tuple[SignalKind, dict[str, Any]] | None:
    """把一条 systemd 日志消息归到生命周期信号；认不出来返回 ``None``。

    认的是 systemd 自己打印的固定句式（systemd 的生命周期消息不翻译），顺序即优先级。
    ``docs/systemd-lifecycle-analysis.md`` 第 3 节列出全部句式与出处。
    """
    text = message.strip()
    found = _classify_one(text)
    if found is not None:
        return found
    stripped = _without_unit_prefix(text)
    if stripped is not None:
        return _classify_one(stripped)
    return None


def _classify_one(text: str) -> tuple[SignalKind, dict[str, Any]] | None:
    """对一段已经去掉 Unit 前缀的文本做句式匹配。"""
    if text.startswith("Starting "):
        return SignalKind.STARTING, {}
    if text.startswith("Started "):
        return SignalKind.STARTED, {}
    if text.startswith("Stopping "):
        return SignalKind.STOPPING, {}
    if text.startswith("Stopped "):
        return SignalKind.STOPPED, {}
    if text.startswith("Failed to start "):
        return SignalKind.ACTIVATION_FAILED, {}
    failed_result = _FAILED_RESULT.search(text)
    if failed_result:
        return SignalKind.FAILED_RESULT, {"result": failed_result.group(1)}
    if text.endswith("entered failed state."):
        return SignalKind.FAILED_RESULT, {"result": None}
    main_exit = _MAIN_EXIT.search(text)
    if main_exit:
        return SignalKind.MAIN_EXIT, {
            "exit_code": main_exit.group(1),
            "exit_status": main_exit.group(2),
        }
    if text.endswith("Deactivated successfully.") or text.endswith("Succeeded."):
        return SignalKind.STOPPED, {}
    return None


def _without_unit_prefix(text: str) -> str | None:
    """去掉 ``<unit>: `` 这样的前缀。

    systemd 的消息有两种写法：``Started PostgreSQL RDBMS.`` 与
    ``postgresql.service: Failed with result 'exit-code'.``，前者不带单元名。前缀只当前缀本身
    不含空格且带冒号时才去掉，避免把正文里的冒号当分隔符。
    """
    head, separator, rest = text.partition(": ")
    if not separator or not head or " " in head:
        return None
    rest = rest.strip()
    return rest or None


def _exit_is_abnormal(code: str, status: str) -> bool:
    """``Main process exited`` 的 code/status 是否属于非正常退出。"""
    if code != "exited":
        # killed / dumped / timeout：都不是正常退出
        return True
    number = status.split("/", 1)[0]
    if number.isdigit():
        return int(number) != 0
    # 状态写法不认识时不下「异常」的结论
    return False


# ----------------------------------------------------------------------
# 重建区间
# ----------------------------------------------------------------------


def reconstruct(records: Iterable[Record | Mapping[str, Any]]) -> LifecycleReconstruction:
    """把一批统一记录重建成 Unit 生命周期。

    同一 Unit 的不同激活按「新的开始证据」「INVOCATION_ID 变化」「终态之后再出现开始证据」
    切开，绝不合并：``INVOCATION_ID`` 是 systemd 给每一次激活分配的编号，它变了就是另一次运行。

    归属不到 Unit 的生命周期消息不参与重建，只汇总进 ``unattributed``：没有可信依据时，
    「没有结论」是诚实的，「给一份记到 ``init.scope`` 头上的结论」不是。
    """
    signals, unattributed = journal_scan(records)
    grouped: dict[tuple[str, str], list[JournalSignal]] = {}
    for signal in signals:
        grouped.setdefault((signal.collection_id, signal.unit), []).append(signal)

    segments: list[UnitSegment] = []
    conclusions: list[LifecycleConclusion] = []
    for group in grouped.values():
        for session in _sessions(group):
            segment = _segment_of(session)
            segments.append(segment)
            conclusions.extend(_conclusions_of(segment))

    segments.sort(key=lambda item: (item.collection_id, item.start_ns, item.unit, item.entity))
    conclusions.sort(
        key=lambda item: (item.collection_id, item.monotonic_ns, item.annotation_type, item.unit)
    )
    identity = signals[0] if signals else (unattributed[0] if unattributed else None)
    return LifecycleReconstruction(
        machine_id=identity.machine_id if identity is not None else "",
        boot_id=identity.boot_id if identity is not None else "",
        segments=tuple(segments),
        conclusions=tuple(conclusions),
        unattributed=unattributed,
    )


def _sessions(signals: Sequence[JournalSignal]) -> tuple[tuple[JournalSignal, ...], ...]:
    """把同一个 (collection, unit) 的信号切成一轮轮激活。"""
    sessions: list[tuple[JournalSignal, ...]] = []
    current: list[JournalSignal] = []
    for signal in signals:
        if current and _opens_new_session(current, signal):
            sessions.append(tuple(current))
            current = []
        current.append(signal)
    if current:
        sessions.append(tuple(current))
    return tuple(sessions)


def _opens_new_session(current: Sequence[JournalSignal], signal: JournalSignal) -> bool:
    """这条信号是否开启新的一轮激活。

    ``INVOCATION_ID`` 是判定「是不是同一次激活」最硬的依据：任何一条信号的编号与当前段已知的
    编号不同，就是另一次运行。这一条对所有信号成立，不只是开始证据——只拿到失败记录的 Unit
    也会连着失败好几次，只按开始证据分段会把几次失败合并成一段。
    """
    if signal.invocation_id is not None:
        known = _first_known_invocation(current)
        if known is not None:
            # 两边编号都拿得到时它就是结论：相同即同一次激活，不同即另一次运行
            return known != signal.invocation_id
    if signal.kind not in (SignalKind.STARTING, SignalKind.STARTED):
        return False
    if any(item.kind in TERMINAL_KINDS for item in current):
        # 上一轮已经有终态，再出现开始证据就是新一轮
        return True
    return any(item.kind is SignalKind.STARTED for item in current)


def _segment_of(session: Sequence[JournalSignal]) -> UnitSegment:
    starting = _first_of(session, SignalKind.STARTING)
    started = _first_of(session, SignalKind.STARTED)
    stopped = _first_of(session, SignalKind.STOPPED)
    terminal = next((item for item in session if item.kind in TERMINAL_KINDS), None)
    failure = next(
        (
            item
            for item in session
            if item.kind in (SignalKind.ACTIVATION_FAILED, SignalKind.FAILED_RESULT)
        ),
        None,
    )
    abnormal_exit = next((item for item in session if item.kind is SignalKind.MAIN_EXIT), None)

    activation_end = started
    if activation_end is None and terminal is not None and terminal is not starting:
        # 激活阶段以失败或停止结束：这就是它的结束时刻
        activation_end = terminal
    active_end = None
    if (
        started is not None
        and terminal is not None
        and terminal.monotonic_ns >= started.monotonic_ns
    ):
        active_end = terminal
    terminal_state = None
    if terminal is not None:
        terminal_state = "inactive" if terminal.kind is SignalKind.STOPPED else "failed"

    reasons: list[str] = []
    if starting is None:
        reasons.append(MISSING_ACTIVATION_START)
    if started is None and stopped is not None:
        reasons.append(MISSING_STARTED)
    if terminal is None:
        reasons.append(MISSING_TERMINATION)
    if (
        started is not None
        and terminal is not None
        and terminal.monotonic_ns < started.monotonic_ns
    ):
        reasons.append(OUT_OF_ORDER_EVIDENCE)

    evidence_order = _evidence_order(session)
    first = session[0]
    unit = first.unit
    return UnitSegment(
        unit=unit,
        entity=next((item.entity for item in session if item.entity), _entity_of(unit)),
        collection_id=first.collection_id,
        machine_id=first.machine_id,
        boot_id=first.boot_id,
        signals=tuple(session),
        invocation_id=_first_value(evidence_order, "invocation_id"),
        pid=_first_value(evidence_order, "pid"),
        cgroup=_first_value(evidence_order, "cgroup"),
        activation_start=starting,
        activation_end=activation_end,
        active_start=started,
        active_end=active_end,
        terminal_state=terminal_state,
        failure=failure,
        abnormal_exit=abnormal_exit,
        incomplete_reasons=tuple(reasons),
    )


def _evidence_order(session: Sequence[JournalSignal]) -> tuple[JournalSignal, ...]:
    """取「这一段是谁」时优先看的证据顺序：激活开始 → 激活结束 → 终态 → 其余按时间。"""
    ordered: list[JournalSignal] = []
    for kind in (SignalKind.STARTING, SignalKind.STARTED):
        ordered.extend(item for item in session if item.kind is kind)
    ordered.extend(item for item in session if item.kind in TERMINAL_KINDS)
    ordered.extend(session)
    return tuple(ordered)


def _first_value(signals: Sequence[JournalSignal], name: str) -> Any:
    for signal in signals:
        value = getattr(signal, name)
        if value is not None:
            return value
    return None


def _first_known_invocation(signals: Sequence[JournalSignal]) -> str | None:
    return _first_value(signals, "invocation_id")


def _first_of(signals: Sequence[JournalSignal], kind: SignalKind) -> JournalSignal | None:
    return next((item for item in signals if item.kind is kind), None)


# ----------------------------------------------------------------------
# 结论
# ----------------------------------------------------------------------


def _conclusions_of(segment: UnitSegment) -> tuple[LifecycleConclusion, ...]:
    items: list[LifecycleConclusion] = []
    if segment.failure is not None:
        items.append(_service_failed(segment))
        if segment.active_start is None and _activation_attempted(segment):
            items.append(_startup_failure(segment))
    if segment.abnormal_exit is not None:
        items.append(_abnormal_exit(segment))
    if segment.incomplete_reasons:
        items.append(_lifecycle_incomplete(segment))
    return tuple(items)


def _activation_attempted(segment: UnitSegment) -> bool:
    """这一次失败之前，有没有「尝试启动」的证据。

    两种成立：观测到 ``Starting``（知道启动过），或者失败本身就是 ``Failed to start``。
    只有 ``Failed with result``、没有开始证据时，只能说「这个 Unit 失败了」，不能说「启动失败」。
    """
    if segment.activation_start is not None:
        return True
    return segment.failure is not None and segment.failure.kind is SignalKind.ACTIVATION_FAILED


def _service_failed(segment: UnitSegment) -> LifecycleConclusion:
    failure = segment.failure
    assert failure is not None  # 由 _conclusions_of 保证
    message = f"{segment.unit} 进入 failed 状态：{failure.message.strip()}"
    duration = _duration_text(segment.startup_duration_ns)
    if duration is not None:
        message = f"{message} 激活耗时 {duration}。"
    return _conclusion(
        segment,
        annotation_type=ANNOTATION_SERVICE_FAILED,
        severity="error",
        message=message,
        evidence=_unique(
            [failure.record_id, *_record_ids([segment.activation_start, segment.active_start])]
        ),
        at=failure,
    )


def _startup_failure(segment: UnitSegment) -> LifecycleConclusion:
    failure = segment.failure
    assert failure is not None
    duration = _duration_text(segment.startup_duration_ns)
    message = f"{segment.unit} 启动失败"
    if duration is not None:
        message = f"{message}，激活阶段用时 {duration}"
    message = f"{message}：{failure.message.strip()}"
    return _conclusion(
        segment,
        annotation_type=ANNOTATION_STARTUP_FAILURE,
        severity="error",
        message=message,
        evidence=_unique(_record_ids([segment.activation_start, failure])),
        at=failure,
    )


def _abnormal_exit(segment: UnitSegment) -> LifecycleConclusion:
    exit_signal = segment.abnormal_exit
    assert exit_signal is not None
    message = (
        f"{segment.unit} 的主进程非正常退出"
        f"（code={exit_signal.exit_code}, status={exit_signal.exit_status}）："
        f"{exit_signal.message.strip()}"
    )
    return _conclusion(
        segment,
        annotation_type=ANNOTATION_ABNORMAL_EXIT,
        severity="error",
        message=message,
        evidence=_unique(_record_ids([exit_signal, segment.failure])),
        at=exit_signal,
    )


def _lifecycle_incomplete(segment: UnitSegment) -> LifecycleConclusion:
    reasons = "；".join(INCOMPLETE_REASON_TEXT[reason] for reason in segment.incomplete_reasons)
    last = segment.signals[-1]
    message = (
        f"{segment.unit} 的生命周期不完整：{reasons}。"
        f"已使用的证据截至第 {len(segment.signals)} 条 Journal 记录"
        f"（{last.record_id}）。"
    )
    return _conclusion(
        segment,
        annotation_type=ANNOTATION_LIFECYCLE_INCOMPLETE,
        severity="warning",
        message=message,
        evidence=_unique([item.record_id for item in segment.signals]),
        at=last,
    )


def _conclusion(
    segment: UnitSegment,
    *,
    annotation_type: str,
    severity: str,
    message: str,
    evidence: tuple[str, ...],
    at: JournalSignal,
) -> LifecycleConclusion:
    return LifecycleConclusion(
        annotation_type=annotation_type,
        unit=segment.unit,
        entity=segment.entity,
        collection_id=segment.collection_id,
        machine_id=segment.machine_id,
        boot_id=segment.boot_id,
        severity=severity,
        message=message,
        evidence_refs=evidence,
        monotonic_ns=at.monotonic_ns,
        realtime_ns=at.realtime_ns,
    )


def _record_ids(signals: Sequence[JournalSignal | None]) -> list[str]:
    return [signal.record_id for signal in signals if signal is not None]


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _duration_text(nanoseconds: int | None) -> str | None:
    if nanoseconds is None:
        return None
    return f"{nanoseconds / 1_000_000:.3f} 毫秒"


# ----------------------------------------------------------------------
# 按时间点查询状态
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Observation:
    """一次状态变化的观测：某个时刻起，这个 Unit 处于某个状态。"""

    unit: str
    collection_id: str
    monotonic_ns: int
    state: str
    invocation_id: str | None
    complete: bool


def _observations_of(segment: UnitSegment) -> tuple[_Observation, ...]:
    """把一段区间摊成「什么时刻变成了什么状态」，供跨采集的状态查询合并。"""
    items: list[_Observation] = []

    def add(moment_ns: int | None, state: str) -> None:
        if moment_ns is None:
            return
        items.append(
            _Observation(
                unit=segment.unit,
                collection_id=segment.collection_id,
                monotonic_ns=moment_ns,
                state=state,
                invocation_id=segment.invocation_id,
                complete=segment.complete,
            )
        )

    add(segment.activation_start_ns, "activating")
    add(segment.active_start_ns, "active")
    if segment.terminal_state is not None:
        add(
            segment.active_end_ns
            if segment.active_end_ns is not None
            else segment.activation_end_ns,
            segment.terminal_state,
        )
    return tuple(items)


# ----------------------------------------------------------------------
# 生成统一记录
# ----------------------------------------------------------------------


def _segment_span_records(segment: UnitSegment) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    if segment.activation_start is not None:
        attributes = _identity_attributes(segment)
        duration = segment.startup_duration_ns
        if duration is not None:
            attributes["startup_duration_ns"] = duration
        if segment.activation_end is None:
            state = "activating"
        elif segment.activation_end is segment.active_start:
            # 激活阶段正常结束（Started），此时 Unit 的状态是 active
            state = "active"
        else:
            state = segment.terminal_state or "failed"
        records.append(
            _span_record(
                segment,
                span_type=SPAN_ACTIVATING,
                start=segment.activation_start,
                end=segment.activation_end,
                state=state,
                attributes=attributes,
            )
        )
    if segment.active_start is not None:
        state = "active" if segment.active_end is None else (segment.terminal_state or "active")
        records.append(
            _span_record(
                segment,
                span_type=SPAN_ACTIVE,
                start=segment.active_start,
                end=segment.active_end,
                state=state,
                attributes=_identity_attributes(segment),
            )
        )
    return tuple(records)


def _identity_attributes(segment: UnitSegment) -> dict[str, Any]:
    """区间上「这一段是谁」的关联信息：Unit、Invocation ID、PID、cgroup。"""
    attributes: dict[str, Any] = {"unit": segment.unit}
    if segment.invocation_id is not None:
        attributes["invocation_id"] = segment.invocation_id
    if segment.pid is not None:
        attributes["pid"] = segment.pid
    if segment.cgroup is not None:
        attributes["cgroup"] = segment.cgroup
    return attributes


def _span_record(
    segment: UnitSegment,
    *,
    span_type: str,
    start: JournalSignal,
    end: JournalSignal | None,
    state: str,
    attributes: Mapping[str, Any],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": RecordKind.SPAN.value,
        "machine_id": segment.machine_id,
        "boot_id": segment.boot_id,
        "collection_id": segment.collection_id,
        "source_plugin": PLUGIN_ID,
        "stream_id": STREAM_SPANS,
        "monotonic_ns": start.monotonic_ns,
        "realtime_ns": start.realtime_ns,
        "clock_domain": ClockDomain.BOOT_MONOTONIC.value,
        "entity": segment.entity,
        "payload": {
            "span_type": span_type,
            "end_monotonic_ns": None if end is None else end.monotonic_ns,
            "end_realtime_ns": None if end is None else end.realtime_ns,
            "state": state,
            "attributes": dict(attributes),
        },
    }
    return {"record_id": compute_record_id(body), **body}


def _annotation_record(conclusion: LifecycleConclusion) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": RecordKind.ANNOTATION.value,
        "machine_id": conclusion.machine_id,
        "boot_id": conclusion.boot_id,
        "collection_id": conclusion.collection_id,
        "source_plugin": PLUGIN_ID,
        "stream_id": STREAM_ANNOTATIONS,
        "monotonic_ns": conclusion.monotonic_ns,
        "realtime_ns": conclusion.realtime_ns,
        "clock_domain": ClockDomain.BOOT_MONOTONIC.value,
        "entity": conclusion.entity,
        "payload": {
            "annotation_type": conclusion.annotation_type,
            "analyzer": PLUGIN_ID,
            "severity": conclusion.severity,
            "message": conclusion.message,
            # 结论由固定规则从原文直接得出，没有概率成分，因此不给置信度（null），
            # 也不拿一个看起来精确的数字冒充把握程度
            "confidence": None,
            "evidence_refs": list(conclusion.evidence_refs),
        },
    }
    return {"record_id": compute_record_id(body), **body}


def _unattributed_record(item: UnattributedLifecycle) -> dict[str, Any]:
    """「N 条生命周期消息归属不到 Unit」的 annotation 记录。"""
    evidence = list(item.record_ids[:MAX_UNATTRIBUTED_EVIDENCE])
    message = (
        f"本采集有 {len(item.record_ids)} 条生命周期消息缺少结构化 Unit 字段"
        "（UNIT= / USER_UNIT=），未产出结论：发送方标识（_SYSTEMD_UNIT / _SYSTEMD_USER_UNIT）是"
        " systemd 自己的作用域（init.scope、user@<uid>.service），不能证明它就是消息所说的那个"
        f" Unit。证据列前 {len(evidence)} 条原始记录：" + "、".join(evidence)
    )
    body: dict[str, Any] = {
        "kind": RecordKind.ANNOTATION.value,
        "machine_id": item.machine_id,
        "boot_id": item.boot_id,
        "collection_id": item.collection_id,
        "source_plugin": PLUGIN_ID,
        "stream_id": STREAM_ANNOTATIONS,
        "monotonic_ns": item.monotonic_ns,
        "realtime_ns": item.realtime_ns,
        "clock_domain": ClockDomain.BOOT_MONOTONIC.value,
        # 这条结论说的不是某个 Unit，因此不挂实体：挂上任何一个都是给人一个错的对象去查
        "entity": None,
        "payload": {
            "annotation_type": ANNOTATION_UNATTRIBUTED,
            "analyzer": PLUGIN_ID,
            "severity": "warning",
            "message": message,
            "confidence": None,
            "evidence_refs": evidence,
        },
    }
    return {"record_id": compute_record_id(body), **body}


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _entity_of(unit: str) -> str:
    return f"service:{unit}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


__all__ = [
    "ANNOTATION_ABNORMAL_EXIT",
    "ANNOTATION_LIFECYCLE_INCOMPLETE",
    "ANNOTATION_SERVICE_FAILED",
    "ANNOTATION_STARTUP_FAILURE",
    "ANNOTATION_UNATTRIBUTED",
    "INCOMPLETE_REASON_TEXT",
    "PLUGIN_ID",
    "SPAN_ACTIVATING",
    "SPAN_ACTIVE",
    "JournalSignal",
    "LifecycleConclusion",
    "LifecycleReconstruction",
    "SignalKind",
    "StartupDuration",
    "UnattributedLifecycle",
    "UnitSegment",
    "UnitStateAt",
    "classify_message",
    "journal_scan",
    "journal_signals",
    "reconstruct",
]
