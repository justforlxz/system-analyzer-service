"""采集任务的执行记录：每次尝试、每个阶段、每个步骤的时间与结果。

记录是**可查询的采集日志与操作记录**（任务 3.2 的验收项之一），也是**中断恢复与重试的依据**：
Worker 把记录写回任务行的 ``payload.result.collection``，下一次尝试从任务行里读回它，
已完成且成功的阶段因此可以直接复用，而不是从头再跑一遍。

为什么放在任务行而不是新表：``jobs.payload`` 本来就是任务自己的数据，任务被重新领取时
``JobQueue.claim`` 会把它原样带回来；重试要读的「上次跑到哪一步」和要写的「这次跑到哪一步」
是同一份数据，分到两张表只会带来两次写入的不一致。新表留给真正需要按采集维度查询的地方
（任务 4.1 的采集管理界面）。

记录的时间字段一律是 UTC 的 ISO 8601 字符串：它只跨进程传递、只展示，不参与计算，
序列化后原样读回，不引入时区换算的歧义。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from system_analyzer.collection.states import (
    AttemptStatus,
    CollectionPhase,
    PhaseStatus,
    check_attempt_transition,
    check_phase_transition,
)


def utc_now_text() -> str:
    """当前 UTC 时间的 ISO 8601 文本，写进记录的时间字段。"""
    return datetime.now(UTC).isoformat()


class EventKind:
    """记录里的动作类型。只用于展示与检索，不参与状态转换。"""

    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"
    PHASE_REUSED = "phase_reused"
    PHASE_FAILED = "phase_failed"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    ORCHESTRATION_FAILED = "orchestration_failed"
    """编排自身出错（写记录失败、取消检查失败），与「步骤失败」区分开。"""


@dataclass(frozen=True, slots=True)
class CollectionEvent:
    """一条操作记录。

    ``kind`` 见 ``EventKind``；``phase`` 与 ``step`` 定位到具体阶段与步骤；``detail`` 写失败
    原因或复用说明。失败原因写在这里，而不是只写进 Worker 日志：Worker 日志会滚动、会丢，
    记录不会。
    """

    at: str
    kind: str
    phase: CollectionPhase
    step: str | None = None
    detail: str | None = None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "phase": self.phase.value,
            "step": self.step,
            "detail": self.detail,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CollectionEvent:
        return cls(
            at=str(mapping["at"]),
            kind=str(mapping["kind"]),
            phase=CollectionPhase(mapping["phase"]),
            step=_optional_text(mapping.get("step")),
            detail=_optional_text(mapping.get("detail")),
        )


@dataclass(slots=True)
class PhaseRecord:
    """一个阶段的执行情况。"""

    phase: CollectionPhase
    status: PhaseStatus = PhaseStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    detail: str | None = None

    def move(self, target: PhaseStatus) -> None:
        """按转换表改状态；不在表里的转换直接抛错，不会留下一个说不清来历的状态。"""
        check_phase_transition(self.status, target)
        self.status = target

    def as_mapping(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> PhaseRecord:
        return cls(
            phase=CollectionPhase(mapping["phase"]),
            status=PhaseStatus(mapping["status"]),
            started_at=_optional_text(mapping.get("started_at")),
            finished_at=_optional_text(mapping.get("finished_at")),
            detail=_optional_text(mapping.get("detail")),
        )


@dataclass(slots=True)
class AttemptRecord:
    """一次采集尝试：三个阶段的状态、全部操作记录，以及阶段之间传递的数据。

    ``data`` 是阶段之间传递的中间产物（例如 Pre 备份了哪些远端文件），跨尝试继承：
    重试时 Pre 已被复用，Post 仍然要靠它把远端恢复回去。
    """

    attempt: int
    status: AttemptStatus = AttemptStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    resumed_from: int | None = None
    phases: dict[CollectionPhase, PhaseRecord] = field(default_factory=dict)
    events: list[CollectionEvent] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for phase in CollectionPhase:
            self.phases.setdefault(phase, PhaseRecord(phase=phase))

    def move(self, target: AttemptStatus) -> None:
        """按转换表改任务状态。"""
        check_attempt_transition(self.status, target)
        self.status = target

    def phase(self, phase: CollectionPhase) -> PhaseRecord:
        """取某个阶段的记录。三个阶段在构造时都已建立，不需要判空。"""
        return self.phases[phase]

    def record_event(
        self,
        kind: str,
        phase: CollectionPhase,
        *,
        step: str | None = None,
        detail: str | None = None,
    ) -> CollectionEvent:
        """追加一条操作记录并返回它。"""
        event = CollectionEvent(at=utc_now_text(), kind=kind, phase=phase, step=step, detail=detail)
        self.events.append(event)
        return event

    def as_mapping(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "resumed_from": self.resumed_from,
            "phases": [record.as_mapping() for record in self.phases.values()],
            "events": [event.as_mapping() for event in self.events],
            "data": dict(self.data),
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> AttemptRecord:
        record = cls(
            attempt=int(mapping["attempt"]),
            status=AttemptStatus(mapping["status"]),
            started_at=_optional_text(mapping.get("started_at")),
            finished_at=_optional_text(mapping.get("finished_at")),
            resumed_from=mapping.get("resumed_from"),
            data=dict(mapping.get("data") or {}),
        )
        for item in mapping.get("phases") or ():
            phase_record = PhaseRecord.from_mapping(item)
            record.phases[phase_record.phase] = phase_record
        record.events = [CollectionEvent.from_mapping(item) for item in mapping.get("events") or ()]
        return record


@dataclass(slots=True)
class CollectionRecord:
    """一条采集任务的全部尝试。``attempts`` 按发生顺序追加，最后一条是本次尝试。"""

    run_id: str
    target_id: str
    attempts: list[AttemptRecord] = field(default_factory=list)

    @property
    def latest(self) -> AttemptRecord | None:
        """最近一次尝试；还没有尝试过时为 ``None``。"""
        return self.attempts[-1] if self.attempts else None

    def start_attempt(self) -> AttemptRecord:
        """追加一次新尝试，并处理上一次尝试留下的痕迹。

        - 上一次尝试以及它里面还挂在 ``running`` 的阶段都改成 ``interrupted``：进程在那时
          退出了，日志不能一直显示「正在执行」。中断的尝试没有确定的结束时间，``finished_at``
          留空。
        - 新尝试继承上一次尝试的 ``data``（备份清单等），供复用的阶段与清理阶段使用。
        """
        previous = self.latest
        attempt = AttemptRecord(attempt=len(self.attempts) + 1)
        if previous is not None:
            if previous.status is AttemptStatus.RUNNING:
                previous.move(AttemptStatus.INTERRUPTED)
            for phase_record in previous.phases.values():
                if phase_record.status is PhaseStatus.RUNNING:
                    phase_record.move(PhaseStatus.INTERRUPTED)
                    phase_record.detail = "执行该阶段时 Worker 退出，本次尝试重新执行"
                    phase_record.finished_at = utc_now_text()
            attempt.resumed_from = previous.attempt
            attempt.data = dict(previous.data)
        self.attempts.append(attempt)
        return attempt

    def as_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "target_id": self.target_id,
            "attempts": [attempt.as_mapping() for attempt in self.attempts],
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> CollectionRecord | None:
        """从任务行里读回记录；没有记录（首次执行）时返回 ``None``。"""
        if not mapping:
            return None
        return cls(
            run_id=str(mapping["run_id"]),
            target_id=str(mapping["target_id"]),
            attempts=[AttemptRecord.from_mapping(item) for item in mapping.get("attempts") or ()],
        )


def _optional_text(value: object) -> str | None:
    """记录里的可空文本字段：``None`` 保持 ``None``，其余统一转成字符串。"""
    return None if value is None else str(value)
