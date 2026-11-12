"""采集记录：序列化往返、「上一次尝试被中断」的标记，以及跨尝试的数据继承。"""

from system_analyzer.collection.record import (
    AttemptRecord,
    CollectionRecord,
    EventKind,
    utc_now_text,
)
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus


def make_record() -> CollectionRecord:
    return CollectionRecord(run_id="run-1", target_id="uos-152")


def test_record_round_trips_through_mapping() -> None:
    """记录写进任务行再读回来，字段与状态一个不差。"""
    record = make_record()
    attempt = record.start_attempt()
    attempt.move(AttemptStatus.RUNNING)
    attempt.started_at = utc_now_text()
    attempt.phase(CollectionPhase.PRE).move(PhaseStatus.RUNNING)
    attempt.phase(CollectionPhase.PRE).move(PhaseStatus.SUCCEEDED)
    attempt.record_event(EventKind.PHASE_FINISHED, CollectionPhase.PRE)
    attempt.record_event(EventKind.STEP_STARTED, CollectionPhase.PRE, step="检查目标环境")
    attempt.data["backup"] = {"paths": ["/etc/x.conf"], "files": {"/etc/x.conf": "0.bak"}}
    attempt.move(AttemptStatus.SUCCEEDED)
    attempt.finished_at = utc_now_text()

    restored = CollectionRecord.from_mapping(record.as_mapping())

    assert restored is not None
    assert restored.as_mapping() == record.as_mapping()
    assert restored.latest is not None
    assert restored.latest.phase(CollectionPhase.PRE).status is PhaseStatus.SUCCEEDED
    assert restored.latest.events[1].step == "检查目标环境"


def test_missing_record_reads_as_none() -> None:
    """任务行里还没有记录（第一次执行）时不造一个空壳出来。"""
    assert CollectionRecord.from_mapping(None) is None
    assert CollectionRecord.from_mapping({}) is None


def test_start_attempt_marks_dangling_phase_interrupted() -> None:
    """上一次尝试停在 running 时，任务与阶段都改成 interrupted，并写明原因。"""
    record = make_record()
    first = record.start_attempt()
    first.move(AttemptStatus.RUNNING)
    first.phase(CollectionPhase.IN_PROGRESS).move(PhaseStatus.RUNNING)

    second = record.start_attempt()

    assert first.status is AttemptStatus.INTERRUPTED
    assert first.finished_at is None, "中断的尝试没有确定的结束时间，不编一个"
    interrupted = first.phase(CollectionPhase.IN_PROGRESS)
    assert interrupted.status is PhaseStatus.INTERRUPTED
    assert interrupted.detail is not None
    assert "Worker 退出" in interrupted.detail
    assert interrupted.finished_at is not None
    assert first.phase(CollectionPhase.PRE).status is PhaseStatus.PENDING
    assert second.status is AttemptStatus.PENDING


def test_start_attempt_inherits_previous_data() -> None:
    """新尝试拿到上一次尝试的阶段数据（备份清单），但用的是自己的一份。"""
    record = make_record()
    first = record.start_attempt()
    first.data["backup"] = {"paths": ["/etc/x.conf"], "files": {"/etc/x.conf": "0.bak"}}

    second = record.start_attempt()

    assert second.attempt == 2
    assert second.resumed_from == 1
    assert second.data == first.data
    assert second.data is not first.data
    second.data["restored"] = ["/etc/x.conf"]
    assert "restored" not in first.data


def test_attempt_record_starts_with_three_pending_phases() -> None:
    """三个阶段在构造时就建好，不需要到处判空。"""
    attempt = AttemptRecord(attempt=1)

    assert set(attempt.phases) == set(CollectionPhase)
    assert {record.status for record in attempt.phases.values()} == {PhaseStatus.PENDING}
