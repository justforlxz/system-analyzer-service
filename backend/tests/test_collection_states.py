"""阶段状态与任务状态的转换表测试。

每条转换一条用例（用例 id 就是转换本身，形如 ``pending->running``），另外三件事也必须成立：

1. 表外转换一律被拒绝（``InvalidTransition``）；
2. 终态没有出边；
3. ``docs/collection-orchestration.md`` 里的两张转换表与代码逐条一致——文档与实现不同步
   比实现出错更难发现，所以让测试盯着。
"""

import re
from pathlib import Path

import pytest

from system_analyzer.collection.states import (
    ATTEMPT_STATUSES,
    ATTEMPT_TRANSITIONS,
    PHASE_STATUSES,
    PHASE_TRANSITIONS,
    AttemptStatus,
    InvalidTransition,
    PhaseStatus,
    check_attempt_transition,
    check_phase_transition,
)

DOCUMENT = Path(__file__).resolve().parents[2] / "docs" / "collection-orchestration.md"

TERMINAL_PHASE_STATUSES = (
    PhaseStatus.SUCCEEDED,
    PhaseStatus.REUSED,
    PhaseStatus.FAILED,
    PhaseStatus.CANCELLED,
    PhaseStatus.TIMED_OUT,
    PhaseStatus.INTERRUPTED,
)

PHASE_TRANSITION_CASES = sorted(PHASE_TRANSITIONS)
ATTEMPT_TRANSITION_CASES = sorted(ATTEMPT_TRANSITIONS)


def transition_ids(cases: list[tuple[object, object]]) -> list[str]:
    return [f"{source}->{target}" for source, target in cases]


@pytest.mark.parametrize(
    ("source", "target"), PHASE_TRANSITION_CASES, ids=transition_ids(PHASE_TRANSITION_CASES)
)
def test_phase_transition(source: PhaseStatus, target: PhaseStatus) -> None:
    """转换表里的这一条阶段转换被允许。"""
    check_phase_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"), ATTEMPT_TRANSITION_CASES, ids=transition_ids(ATTEMPT_TRANSITION_CASES)
)
def test_attempt_transition(source: AttemptStatus, target: AttemptStatus) -> None:
    """转换表里的这一条任务转换被允许。"""
    check_attempt_transition(source, target)


@pytest.mark.parametrize("status", TERMINAL_PHASE_STATUSES)
def test_terminal_phase_states_have_no_outgoing_transition(status: PhaseStatus) -> None:
    """终态没有出边：任何离开终态的转换都必须是错误。"""
    assert [target for source, target in PHASE_TRANSITIONS if source is status] == []
    for target in PHASE_STATUSES:
        if target is status:
            continue
        with pytest.raises(InvalidTransition):
            check_phase_transition(status, target)


def test_invalid_phase_transition_is_rejected() -> None:
    """``pending`` 不能直接跳到 ``succeeded``：阶段必须先进入 ``running``。"""
    with pytest.raises(InvalidTransition) as raised:
        check_phase_transition(PhaseStatus.PENDING, PhaseStatus.SUCCEEDED)

    assert raised.value.source == "pending"
    assert raised.value.target == "succeeded"
    assert "pending" in str(raised.value)
    assert "succeeded" in str(raised.value)


def test_invalid_attempt_transition_is_rejected() -> None:
    """成功尝试不能回到 ``running``：重试开的是新的一次尝试，不是改旧记录。"""
    with pytest.raises(InvalidTransition):
        check_attempt_transition(AttemptStatus.SUCCEEDED, AttemptStatus.RUNNING)


def test_every_status_appears_in_a_transition() -> None:
    """每个定义出来的状态都要出现在转换表里，否则就是定义了却用不到的状态。"""
    phase_used = {status for pair in PHASE_TRANSITIONS for status in pair}
    attempt_used = {status for pair in ATTEMPT_TRANSITIONS for status in pair}
    assert phase_used == set(PHASE_STATUSES)
    assert attempt_used == set(ATTEMPT_STATUSES)


def test_phase_transition_table_matches_document() -> None:
    """文档里的阶段转换表与代码逐条一致。"""
    documented = documented_transitions("### 阶段状态转换")
    assert documented == {(source.value, target.value) for source, target in PHASE_TRANSITIONS}


def test_attempt_transition_table_matches_document() -> None:
    """文档里的任务转换表与代码逐条一致。"""
    documented = documented_transitions("### 任务状态转换")
    assert documented == {(source.value, target.value) for source, target in ATTEMPT_TRANSITIONS}


def documented_transitions(heading: str) -> set[tuple[str, str]]:
    """从文档的某个小节里读出 ``| 从 | 到 |`` 两列全部取值。"""
    assert DOCUMENT.is_file(), f"缺少状态转换文档：{DOCUMENT}"
    text = DOCUMENT.read_text(encoding="utf-8")
    assert heading in text, f"文档里没有小节：{heading}"
    section = text.split(heading, 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
    return set(re.findall(r"^\| ([a-z_]+) \| ([a-z_]+) \|", section, re.MULTILINE))
