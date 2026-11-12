"""三阶段编排：按 Pre → In Progress → Post 执行，处理超时、取消、失败与中断恢复。

编排只做流程，不采集任何东西：每个阶段做什么在 ``collection.plan`` 里，连哪台机器在
``collection.connector`` 里。三条规则决定了本模块的形状：

1. **清理阶段（Post）无条件执行。** 前面任一阶段失败、取消、超时，或者编排自身出错（写采集记录
   失败、取消检查失败），Post 都要跑完它的恢复动作，否则目标机器会停在半改状态。Post 自己不响应
   取消请求，也不因为写记录失败而停下：取消的时候更需要它把环境恢复回去。
2. **每次尝试是独立的状态机。** 重试开启一条新记录，不把上一次的终态改回 running；要不要
   复用上一次的成果看上一次的阶段状态，由 ``_reusable`` 判断。
3. **记录随状态变化落盘。** 每次阶段开始、阶段结束、步骤结束时都调用一次保存回调，
   进程被杀后重新领取任务时，能从记录里读出「走到哪个阶段、哪个步骤」。

清理阶段失败与「按用户要求停下」不能混为一谈：Post 没把远端恢复回去时，即使前面是取消，
本次尝试也记成失败（``_attempt_status``），任务因此会重试、临时目录与备份也不会被删。

已知边界：进程被暴力终止（``kill -9``、断电）时，被中断那次尝试的内存清理动作不会执行，
远端可能停在一份改过的配置上；这时靠下一次尝试的 Post 阶段恢复（它按记录里的备份清单执行），
而不是靠进程退出时的回调。这也是备份清单必须持久化的原因。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system_analyzer.collection.connector import TargetConnector
from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.plan import PhasePlan, default_plan
from system_analyzer.collection.record import (
    AttemptRecord,
    CollectionRecord,
    EventKind,
    utc_now_text,
)
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus

logger = logging.getLogger(__name__)

DEFAULT_PHASE_TIMEOUTS: Mapping[CollectionPhase, float] = {
    CollectionPhase.PRE: 300.0,
    CollectionPhase.IN_PROGRESS: 1800.0,
    CollectionPhase.POST: 300.0,
}
"""各阶段的默认时间上限（秒）。Pre 与 Post 只做检查、备份、恢复，In Progress 要等桌面登录与
采样窗口，允许更长。任务 payload 的 ``options.phase_timeouts`` 可以按阶段覆盖。"""

CancelCheck = Callable[[], Awaitable[bool]]
"""取消信号：返回 ``True`` 表示请求取消。编排在两个步骤之间检查一次。"""

SaveRecord = Callable[[CollectionRecord], Awaitable[None]]
"""把记录写回任务行。每次状态变化后调用一次。"""


class PhaseAbort(Exception):
    """某个阶段提前结束：失败、取消或超时。``status`` 决定阶段与任务的终态。"""

    def __init__(self, status: PhaseStatus, detail: str) -> None:
        super().__init__(detail)
        if status not in (PhaseStatus.FAILED, PhaseStatus.CANCELLED, PhaseStatus.TIMED_OUT):
            raise ValueError(
                f"阶段提前结束的状态只能是 failed / cancelled / timed_out，实际是 {status}"
            )
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    """一次采集尝试的结果。"""

    record: CollectionRecord
    attempt: AttemptRecord
    failure: str | None
    """主失败原因；本次尝试成功或取消且清理成功时为 ``None``。"""

    @property
    def status(self) -> AttemptStatus:
        return self.attempt.status


async def run_collection(
    *,
    target_id: str,
    run_id: str,
    connector: TargetConnector,
    scratch_dir: Path,
    previous: CollectionRecord | None = None,
    plans: Mapping[CollectionPhase, PhasePlan] | None = None,
    options: Mapping[str, Any] | None = None,
    phase_timeouts: Mapping[CollectionPhase, float] | None = None,
    cancelled: CancelCheck | None = None,
    save: SaveRecord | None = None,
) -> CollectionOutcome:
    """执行一次采集尝试。

    ``previous`` 是这条采集任务上一次尝试留下的记录：给了就接着它跑（复用它已经成功的阶段，
    并把记录里悬挂在 running 的阶段改记为 interrupted），没给就是第一次执行。

    ``scratch_dir`` 是这条采集任务的临时目录，跨尝试保持同一个路径：上一次尝试备份的远端文件
    还在里面，本次尝试的 Post 阶段要用它恢复。
    """
    record = _load_record(previous, target_id=target_id, run_id=run_id)
    attempt = record.start_attempt()
    attempt.move(AttemptStatus.RUNNING)
    attempt.started_at = utc_now_text()

    resolved_plans = dict(plans or default_plan())
    timeouts = {**DEFAULT_PHASE_TIMEOUTS, **(phase_timeouts or {})}
    ctx = PhaseContext(
        target_id=target_id,
        run_id=run_id,
        attempt=attempt.attempt,
        options=dict(options or {}),
        scratch_dir=scratch_dir,
        data=attempt.data,
        connector=connector,
    )

    primary: PhaseAbort | None = None
    phase = CollectionPhase.PRE
    try:
        await _save(save, record)
        for phase in (CollectionPhase.PRE, CollectionPhase.IN_PROGRESS):
            if _reusable(record, phase):
                _mark_reused(attempt, phase, source=record.attempts[-2].attempt)
                await _save(save, record)
                continue
            try:
                await _run_phase(
                    record,
                    attempt,
                    resolved_plans[phase],
                    ctx,
                    limit_seconds=timeouts[phase],
                    cancelled=cancelled or _never_cancelled,
                    save=save,
                )
            except PhaseAbort as abort:
                primary = abort
                break
    except Exception as exc:
        # 写记录失败、取消检查失败这类基础设施异常的兜底：阶段没能成功，但清理阶段绝不能被跳过。
        primary = _orchestration_abort(attempt, phase, exc)

    # 清理阶段无条件执行：不响应取消请求，写记录失败也继续执行它的动作。
    post_abort: PhaseAbort | None = None
    try:
        await _run_phase(
            record,
            attempt,
            resolved_plans[CollectionPhase.POST],
            ctx,
            limit_seconds=timeouts[CollectionPhase.POST],
            cancelled=_never_cancelled,
            save=save,
            fatal_save=False,
        )
    except PhaseAbort as abort:
        post_abort = abort
    except Exception as exc:
        post_abort = _orchestration_abort(attempt, CollectionPhase.POST, exc, cleanup=True)

    attempt.move(_attempt_status(primary, post_abort))
    attempt.finished_at = utc_now_text()
    await _save(save, record)
    return CollectionOutcome(
        record=record, attempt=attempt, failure=_failure_detail(primary, post_abort)
    )


def _load_record(
    previous: CollectionRecord | None, *, target_id: str, run_id: str
) -> CollectionRecord:
    """拿到这次要接着写的记录。给了上一次的记录就核对它确实属于同一条采集任务。"""
    if previous is None:
        return CollectionRecord(run_id=run_id, target_id=target_id)
    if previous.run_id != run_id or previous.target_id != target_id:
        raise ValueError(
            f"上一次尝试的记录不属于这条采集任务：记录是 {previous.run_id}@{previous.target_id}，"
            f"本次是 {run_id}@{target_id}"
        )
    return previous


def _reusable(record: CollectionRecord, phase: CollectionPhase) -> bool:
    """该阶段能否直接复用上一次尝试的成果。

    Post 永不复用：它是清理阶段，每次尝试都要确认远端环境已经恢复。上一次尝试里被标成
    ``interrupted`` 的阶段也不复用，因为它根本没跑完。
    """
    if phase is CollectionPhase.POST or len(record.attempts) < 2:
        return False
    previous_status = record.attempts[-2].phase(phase).status
    return previous_status in (PhaseStatus.SUCCEEDED, PhaseStatus.REUSED)


def _mark_reused(attempt: AttemptRecord, phase: CollectionPhase, *, source: int) -> None:
    """把本次尝试的该阶段记为「复用上一次结果」。"""
    phase_record = attempt.phase(phase)
    phase_record.move(PhaseStatus.REUSED)
    detail = f"复用第 {source} 次尝试已经成功的该阶段结果，本次没有重新执行"
    phase_record.detail = detail
    attempt.record_event(EventKind.PHASE_REUSED, phase, detail=detail)


async def _run_phase(
    record: CollectionRecord,
    attempt: AttemptRecord,
    plan: PhasePlan,
    ctx: PhaseContext,
    *,
    limit_seconds: float,
    cancelled: CancelCheck,
    save: SaveRecord | None,
    fatal_save: bool = True,
) -> None:
    """执行一个阶段。成功返回；失败、取消、超时抛 ``PhaseAbort``（状态已写进记录）。

    ``fatal_save`` 为假时（清理阶段）写记录失败只写日志、不中断：清理动作本身比记录更重要。
    """
    phase_record = attempt.phase(plan.phase)
    phase_record.move(PhaseStatus.RUNNING)
    phase_record.started_at = utc_now_text()
    attempt.record_event(EventKind.PHASE_STARTED, plan.phase)
    await _save(save, record, fatal=fatal_save)
    try:
        await asyncio.wait_for(
            _run_steps(
                record,
                attempt,
                plan,
                ctx,
                cancelled=cancelled,
                save=save,
                fatal_save=fatal_save,
            ),
            timeout=limit_seconds,
        )
    except TimeoutError:
        await _abort(
            record,
            attempt,
            plan.phase,
            PhaseStatus.TIMED_OUT,
            f"阶段超过 {limit_seconds:g} 秒的时间上限",
            save=save,
            fatal_save=fatal_save,
        )
    except PhaseAbort as abort:
        await _abort(
            record,
            attempt,
            plan.phase,
            abort.status,
            abort.detail,
            save=save,
            fatal_save=fatal_save,
        )
    phase_record.move(PhaseStatus.SUCCEEDED)
    phase_record.finished_at = utc_now_text()
    attempt.record_event(EventKind.PHASE_FINISHED, plan.phase)
    await _save(save, record, fatal=fatal_save)


async def _run_steps(
    record: CollectionRecord,
    attempt: AttemptRecord,
    plan: PhasePlan,
    ctx: PhaseContext,
    *,
    cancelled: CancelCheck,
    save: SaveRecord | None,
    fatal_save: bool = True,
) -> None:
    """按顺序执行阶段里的步骤。取消检查发生在两个步骤之间。"""
    for step in plan.steps:
        if await cancelled():
            raise PhaseAbort(PhaseStatus.CANCELLED, f"收到取消请求，未执行步骤「{step.name}」")
        attempt.record_event(EventKind.STEP_STARTED, plan.phase, step=step.name)
        try:
            await step.run(ctx)
        except Exception as exc:
            raise PhaseAbort(
                PhaseStatus.FAILED,
                f"步骤「{step.name}」失败：{type(exc).__name__}: {exc}",
            ) from exc
        attempt.record_event(EventKind.STEP_FINISHED, plan.phase, step=step.name)
        await _save(save, record, fatal=fatal_save)


async def _abort(
    record: CollectionRecord,
    attempt: AttemptRecord,
    phase: CollectionPhase,
    status: PhaseStatus,
    detail: str,
    *,
    save: SaveRecord | None,
    fatal_save: bool = True,
) -> None:
    """把当前阶段记成提前结束，落盘，然后抛 ``PhaseAbort`` 交给上层决定任务终态。"""
    phase_record = attempt.phase(phase)
    phase_record.move(status)
    phase_record.detail = detail
    phase_record.finished_at = utc_now_text()
    attempt.record_event(EventKind.PHASE_FAILED, phase, detail=detail)
    await _save(save, record, fatal=fatal_save)
    raise PhaseAbort(status, detail)


def _orchestration_abort(
    attempt: AttemptRecord,
    phase: CollectionPhase,
    exc: BaseException,
    *,
    cleanup: bool = False,
) -> PhaseAbort:
    """把编排自身的异常（写记录失败、取消检查失败）折算成阶段失败。

    与「步骤失败」分开记：步骤失败是远端动作没做成，这里说的是编排没能继续，读日志的人要能区分。
    该阶段的动作可能已经做完（例如阶段结束时写记录失败），所以只有它还挂在 running 时才改状态；
    无论如何都补一条 ``orchestration_failed`` 事件，失败原因不会只留在 Worker 日志里。
    """
    detail = f"{'清理阶段' if cleanup else '编排'}执行出错：{type(exc).__name__}: {exc}"
    phase_record = attempt.phase(phase)
    if phase_record.status is PhaseStatus.RUNNING:
        phase_record.move(PhaseStatus.FAILED)
        phase_record.detail = detail
        phase_record.finished_at = utc_now_text()
    attempt.record_event(EventKind.ORCHESTRATION_FAILED, phase, detail=detail)
    return PhaseAbort(PhaseStatus.FAILED, detail)


def _attempt_status(primary: PhaseAbort | None, cleanup: PhaseAbort | None) -> AttemptStatus:
    """本次尝试的终态。

    清理阶段失败时不能沿用「取消即结束」的判定：远端可能没恢复，任务必须失败，才会重试，
    临时目录与备份也才会保留下来。
    """
    if cleanup is not None and (primary is None or primary.status is PhaseStatus.CANCELLED):
        return AttemptStatus.FAILED
    if primary is None:
        return AttemptStatus.SUCCEEDED
    return {
        PhaseStatus.FAILED: AttemptStatus.FAILED,
        PhaseStatus.CANCELLED: AttemptStatus.CANCELLED,
        PhaseStatus.TIMED_OUT: AttemptStatus.TIMED_OUT,
    }[primary.status]


def _failure_detail(primary: PhaseAbort | None, cleanup: PhaseAbort | None) -> str | None:
    """失败原因。两个阶段都失败时都要写清楚，别让清理失败被主失败吞掉。"""
    if primary is not None and cleanup is not None:
        return f"{primary.detail}；清理阶段也失败：{cleanup.detail}"
    if primary is not None:
        return primary.detail
    if cleanup is not None:
        return f"清理阶段失败：{cleanup.detail}"
    return None


async def _save(save: SaveRecord | None, record: CollectionRecord, *, fatal: bool = True) -> None:
    """把记录写回任务行。``fatal`` 为假时记日志后继续，供清理阶段使用。"""
    if save is None:
        return
    try:
        await save(record)
    except Exception:
        if fatal:
            raise
        logger.warning("写采集记录失败，继续执行清理动作", exc_info=True)


async def _never_cancelled() -> bool:
    return False
