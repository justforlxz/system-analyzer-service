"""采集任务的状态定义、状态转换表与转换校验（任务 3.2）。

两层状态，各自独立、互不冒充：

- **阶段状态**（``PhaseStatus``）：Pre、In Progress、Post 三个阶段各自处在哪一步。
- **任务状态**（``AttemptStatus``）：一次采集尝试整体的结果。

一次采集任务允许被重试与中断恢复，每次执行是一条独立的「尝试」（``AttemptStatus`` 从
``pending`` 重新开始），所以状态机里没有「终态回到 running」这种自环转换。重试要不要
接着上次的成果，由 ``collection.record`` 记录的阶段状态决定，不属于状态转换。

转换表是唯一真值：``PHASE_TRANSITIONS`` 与 ``ATTEMPT_TRANSITIONS`` 里没有的转换一律拒绝，
``docs/collection-orchestration.md`` 的两张转换表由测试与这里逐条对照。
"""

from enum import StrEnum

PHASE_ORDER = ("pre", "in_progress", "post")
"""三个阶段的固定执行顺序。清理（Post）阶段无条件执行，见 ``collection.orchestrator``。"""


class CollectionPhase(StrEnum):
    """采集的三个阶段。值同时是写进任务结果里的键名。"""

    PRE = "pre"
    IN_PROGRESS = "in_progress"
    POST = "post"


class PhaseStatus(StrEnum):
    """单个阶段的状态。"""

    PENDING = "pending"
    """还没开始。每次尝试的三个阶段都从这里出发。"""

    RUNNING = "running"
    """正在执行。"""

    SUCCEEDED = "succeeded"
    """本阶段全部步骤执行完成。"""

    REUSED = "reused"
    """本次尝试没有重跑，直接复用上一次成功尝试的结果（重试与中断恢复用）。"""

    FAILED = "failed"
    """步骤抛异常，或清理阶段的恢复动作失败。"""

    CANCELLED = "cancelled"
    """收到取消请求后停下。"""

    TIMED_OUT = "timed_out"
    """超过本阶段的时间上限。"""

    INTERRUPTED = "interrupted"
    """执行该阶段时进程退出（Worker 被杀、机器重启），任务后来被重新领取。

    只有「上一次尝试」里的阶段会被改成这个状态：它让日志里不再留下一个永远 running 的阶段。
    """


class AttemptStatus(StrEnum):
    """一次采集尝试的整体结果。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    """执行期间进程退出，任务后来被重新领取；没有确定的结束时间。"""


PHASE_STATUSES: tuple[PhaseStatus, ...] = tuple(PhaseStatus)
"""全部阶段状态，按定义顺序。用于「终态校验」这类需要遍历所有状态的检查。"""


class InvalidTransition(ValueError):
    """不在转换表里的状态转换。"""

    def __init__(self, subject: str, source: str, target: str) -> None:
        super().__init__(f"{subject}不能从 {source} 转换到 {target}")
        self.subject = subject
        self.source = source
        self.target = target


ATTEMPT_STATUSES: tuple[AttemptStatus, ...] = tuple(AttemptStatus)
"""全部任务状态，按定义顺序。"""


PHASE_TRANSITIONS: frozenset[tuple[PhaseStatus, PhaseStatus]] = frozenset(
    {
        ### 开始执行
        (PhaseStatus.PENDING, PhaseStatus.RUNNING),
        ### 重试时复用上一次的成功结果
        (PhaseStatus.PENDING, PhaseStatus.REUSED),
        ### 正常结束
        (PhaseStatus.RUNNING, PhaseStatus.SUCCEEDED),
        ### 步骤失败
        (PhaseStatus.RUNNING, PhaseStatus.FAILED),
        ### 收到取消请求
        (PhaseStatus.RUNNING, PhaseStatus.CANCELLED),
        ### 超过阶段时间上限
        (PhaseStatus.RUNNING, PhaseStatus.TIMED_OUT),
        ### 进程在该阶段执行期间退出
        (PhaseStatus.RUNNING, PhaseStatus.INTERRUPTED),
    }
)
"""阶段状态转换表。每条转换都在 ``tests/test_collection_states.py`` 里有一条对应用例。"""


ATTEMPT_TRANSITIONS: frozenset[tuple[AttemptStatus, AttemptStatus]] = frozenset(
    {
        ### 开始一次尝试
        (AttemptStatus.PENDING, AttemptStatus.RUNNING),
        ### 三个阶段全部成功
        (AttemptStatus.RUNNING, AttemptStatus.SUCCEEDED),
        ### 某个阶段失败（含清理阶段的恢复动作失败）
        (AttemptStatus.RUNNING, AttemptStatus.FAILED),
        ### 收到取消请求
        (AttemptStatus.RUNNING, AttemptStatus.CANCELLED),
        ### 某个阶段超过时间上限
        (AttemptStatus.RUNNING, AttemptStatus.TIMED_OUT),
        ### 执行期间进程退出，任务被重新领取
        (AttemptStatus.RUNNING, AttemptStatus.INTERRUPTED),
    }
)
"""任务（一次尝试）状态转换表。每条转换都在 ``tests/test_collection_states.py`` 有一条用例。"""


def check_phase_transition(source: PhaseStatus, target: PhaseStatus) -> None:
    """校验一次阶段状态转换；不在表里就抛 ``InvalidTransition``。"""
    _check(PHASE_TRANSITIONS, "阶段状态", source, target)


def check_attempt_transition(source: AttemptStatus, target: AttemptStatus) -> None:
    """校验一次任务状态转换；不在表里就抛 ``InvalidTransition``。"""
    _check(ATTEMPT_TRANSITIONS, "任务状态", source, target)


def _check[S: StrEnum](table: frozenset[tuple[S, S]], subject: str, source: S, target: S) -> None:
    if (source, target) not in table:
        raise InvalidTransition(subject, str(source), str(target))
