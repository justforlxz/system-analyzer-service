"""三阶段编排的行为测试：顺序、失败清理、取消、超时、重试与并发。

全部用假 SSH 连接器（``tests/collection_fakes.py``）走真实编排代码，不需要目标机器。
「远端文件被恢复成原样」直接比对假连接器里的文件内容，不靠「调用过 upload」这种弱断言。
"""

import asyncio
from pathlib import Path

import pytest

from collection_fakes import FakeConnector
from system_analyzer.collection.environment import ENVIRONMENT_COMMAND
from system_analyzer.collection.orchestrator import CollectionOutcome, run_collection
from system_analyzer.collection.plan import (
    BACKUP_DIR_NAME,
    CollectionStep,
    PhasePlan,
    backup_configuration,
    default_plan,
    inspect_target,
)
from system_analyzer.collection.record import CollectionRecord, EventKind
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus
from system_analyzer.remote.probe import SYSTEM_INFO_COMMAND

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"
CHANGED_CONFIG = b"[Seat:*]\n# temporary\n"

PRE = CollectionPhase.PRE
IN_PROGRESS = CollectionPhase.IN_PROGRESS
POST = CollectionPhase.POST


def make_connector(config: bytes = ORIGINAL_CONFIG) -> FakeConnector:
    return FakeConnector({TARGET: {CONFIG_PATH: config}})


def step(name: str, action) -> CollectionStep:
    return CollectionStep(name=name, run=action)


def with_steps(plan: dict[CollectionPhase, PhasePlan], phase: CollectionPhase, *steps):
    """替换某个阶段的步骤，模拟 3.4–3.7 往编排里填动作。"""
    result = dict(plan)
    result[phase] = PhasePlan(phase=phase, steps=tuple(steps))
    return result


def change_config(next_value: bytes):
    """一个会临时改远端配置的步骤，代表 Pre 的临时配置或 3.6 的 LightDM 改动。"""

    async def action(ctx) -> None:
        async with ctx.connect() as client:
            client.files[CONFIG_PATH] = next_value

    return action


async def run(connector: FakeConnector, scratch_dir: Path, **kwargs) -> CollectionOutcome:
    return await run_collection(
        target_id=TARGET, run_id="run-1", connector=connector, scratch_dir=scratch_dir, **kwargs
    )


def phase_statuses(outcome: CollectionOutcome) -> dict[CollectionPhase, PhaseStatus]:
    return {record.phase: record.status for record in outcome.attempt.phases.values()}


async def test_running_phase_transitions_in_order(tmp_path: Path) -> None:
    """三个阶段按 Pre → In Progress → Post 依次执行，各自 pending → running → succeeded。"""
    connector = make_connector()
    order: list[CollectionPhase] = []

    def recorder(phase: CollectionPhase):
        async def action(_ctx) -> None:
            order.append(phase)

        return action

    plans = {
        phase: PhasePlan(phase=phase, steps=(step(f"{phase} 步骤", recorder(phase)),))
        for phase in CollectionPhase
    }

    outcome = await run(connector, tmp_path, plans=plans)

    assert order == [PRE, IN_PROGRESS, POST]
    assert outcome.status is AttemptStatus.SUCCEEDED
    assert phase_statuses(outcome) == {
        PRE: PhaseStatus.SUCCEEDED,
        IN_PROGRESS: PhaseStatus.SUCCEEDED,
        POST: PhaseStatus.SUCCEEDED,
    }
    started = [
        event.phase for event in outcome.attempt.events if event.kind == EventKind.PHASE_STARTED
    ]
    assert started == [PRE, IN_PROGRESS, POST]
    for record in outcome.attempt.phases.values():
        assert record.started_at is not None
        assert record.finished_at is not None


async def test_default_plan_checks_target_backs_up_and_restores(tmp_path: Path) -> None:
    """默认计划：Pre 取到系统信息并备份配置，In Progress 改过配置，Post 把它恢复成原样。"""
    connector = make_connector()
    plans = with_steps(
        default_plan(), IN_PROGRESS, step("改临时配置", change_config(CHANGED_CONFIG))
    )

    outcome = await run(connector, tmp_path, plans=plans, options={"backup_paths": [CONFIG_PATH]})

    assert outcome.status is AttemptStatus.SUCCEEDED
    # 环境检查真的读到了目标机器
    assert outcome.attempt.data["system_info"]["hostname"] == TARGET
    # 备份清单写进了记录（下次尝试的 Post 阶段要靠它）
    assert outcome.attempt.data["backup"]["paths"] == [CONFIG_PATH]
    # 恢复动作把远端文件写回备份内容
    assert outcome.attempt.data["restored"] == [CONFIG_PATH]
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    # 采集中途确实被改过（否则「恢复成原样」是空话）
    assert connector.downloads_of(TARGET) == [CONFIG_PATH]


async def test_phase_failure_still_runs_post_and_restores(tmp_path: Path) -> None:
    """In Progress 失败后 Post 仍然执行，远端配置被恢复，失败原因写进记录。"""
    connector = make_connector()

    async def boom(_ctx) -> None:
        raise RuntimeError("采样起不来")

    plans = with_steps(
        default_plan(),
        IN_PROGRESS,
        step("改临时配置", change_config(CHANGED_CONFIG)),
        step("启动采样", boom),
    )

    outcome = await run(connector, tmp_path, plans=plans, options={"backup_paths": [CONFIG_PATH]})

    assert outcome.status is AttemptStatus.FAILED
    statuses = phase_statuses(outcome)
    assert statuses[PRE] is PhaseStatus.SUCCEEDED
    assert statuses[IN_PROGRESS] is PhaseStatus.FAILED
    assert statuses[POST] is PhaseStatus.SUCCEEDED
    detail = outcome.attempt.phase(IN_PROGRESS).detail
    assert detail is not None and "启动采样" in detail and "RuntimeError" in detail
    assert outcome.failure == detail
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    failed_events = [
        event for event in outcome.attempt.events if event.kind == EventKind.PHASE_FAILED
    ]
    assert [event.phase for event in failed_events] == [IN_PROGRESS]


async def test_cancel_marks_phase_cancelled_and_restores(tmp_path: Path) -> None:
    """取消请求在步骤之间生效：In Progress 记 cancelled，Post 仍把配置恢复回去。"""
    connector = make_connector()
    state = {"requested": False}

    async def request_cancel(_ctx) -> None:
        state["requested"] = True

    async def cancelled() -> bool:
        return state["requested"]

    async def should_not_run(_ctx) -> None:
        raise AssertionError("取消后不应再执行步骤")

    plan = default_plan()
    plans = {
        PRE: PhasePlan(
            phase=PRE,
            steps=(
                step("检查目标环境", inspect_target),
                step("备份系统配置", backup_configuration),
                step("改临时配置", change_config(CHANGED_CONFIG)),
                step("请求取消", request_cancel),
            ),
        ),
        IN_PROGRESS: PhasePlan(phase=IN_PROGRESS, steps=(step("启动采样", should_not_run),)),
        POST: plan[POST],
    }

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        cancelled=cancelled,
    )

    assert outcome.status is AttemptStatus.CANCELLED
    statuses = phase_statuses(outcome)
    assert statuses[PRE] is PhaseStatus.SUCCEEDED
    assert statuses[IN_PROGRESS] is PhaseStatus.CANCELLED
    assert statuses[POST] is PhaseStatus.SUCCEEDED
    detail = outcome.attempt.phase(IN_PROGRESS).detail
    assert detail is not None and "取消" in detail
    # 取消不阻止清理：远端配置回到原样
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    # 取消之后的步骤一个都没执行
    assert not [
        event
        for event in outcome.attempt.events
        if event.phase is IN_PROGRESS and event.kind == EventKind.STEP_FINISHED
    ]


async def test_timeout_marks_phase_timed_out_and_restores(tmp_path: Path) -> None:
    """阶段超时：该阶段记 timed_out，Post 仍然执行并把远端配置恢复。"""
    connector = make_connector()

    async def slow_collect(ctx) -> None:
        await change_config(CHANGED_CONFIG)(ctx)
        await asyncio.sleep(30)

    plans = with_steps(default_plan(), IN_PROGRESS, step("持续采集", slow_collect))

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        phase_timeouts={IN_PROGRESS: 0.05},
    )

    assert outcome.status is AttemptStatus.TIMED_OUT
    statuses = phase_statuses(outcome)
    assert statuses[IN_PROGRESS] is PhaseStatus.TIMED_OUT
    assert statuses[POST] is PhaseStatus.SUCCEEDED
    detail = outcome.attempt.phase(IN_PROGRESS).detail
    assert detail is not None and "时间上限" in detail
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG


async def test_retry_reuses_succeeded_phases(tmp_path: Path) -> None:
    """重试复用上一次已经成功的 Pre，In Progress 重跑，Post 每次都执行。"""
    connector = make_connector()
    attempts = {"count": 0}

    async def flaky_collect(_ctx) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("第一次采样失败")

    plans = with_steps(default_plan(), IN_PROGRESS, step("启动采样", flaky_collect))
    options = {"backup_paths": [CONFIG_PATH]}

    first = await run(connector, tmp_path, plans=plans, options=options)
    second = await run(connector, tmp_path, plans=plans, options=options, previous=first.record)

    assert first.status is AttemptStatus.FAILED
    assert second.status is AttemptStatus.SUCCEEDED
    assert second.attempt.attempt == 2
    assert second.attempt.resumed_from == 1
    statuses = phase_statuses(second)
    assert statuses[PRE] is PhaseStatus.REUSED
    assert statuses[IN_PROGRESS] is PhaseStatus.SUCCEEDED
    assert statuses[POST] is PhaseStatus.SUCCEEDED
    reused = second.attempt.phase(PRE)
    assert reused.detail is not None and "复用第 1 次尝试" in reused.detail
    assert first.status is AttemptStatus.FAILED, "上一次尝试的终态不被改写"
    assert first.attempt.phase(PRE).status is PhaseStatus.SUCCEEDED


async def test_rerunning_a_phase_does_not_duplicate_side_effects(tmp_path: Path) -> None:
    """重试时 Pre 不再产生副作用：不再连一次做检查，也不再下载一次备份。"""
    connector = make_connector()
    attempts = {"count": 0}

    async def flaky_collect(_ctx) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("第一次采样失败")

    plans = with_steps(default_plan(), IN_PROGRESS, step("启动采样", flaky_collect))
    options = {"backup_paths": [CONFIG_PATH]}

    first = await run(connector, tmp_path, plans=plans, options=options)
    commands_after_first = connector.commands_of(TARGET)
    downloads_after_first = connector.downloads_of(TARGET)

    await run(connector, tmp_path, plans=plans, options=options, previous=first.record)

    # 第二次尝试只跑 Post（恢复与清理）：它不再做环境检查，也不再下载备份
    second_attempt_commands = connector.commands_of(TARGET)[len(commands_after_first) :]
    pre_commands = {SYSTEM_INFO_COMMAND, ENVIRONMENT_COMMAND}
    assert not [command for command in second_attempt_commands if command in pre_commands]
    assert connector.downloads_of(TARGET) == downloads_after_first
    assert downloads_after_first == [CONFIG_PATH], "第一次确实备份过一次"


async def test_two_targets_run_concurrently_without_interference(tmp_path: Path) -> None:
    """两台目标机器同时跑任务，各自只碰自己的文件与连接。"""
    other_config = "/etc/a.conf"
    connector = FakeConnector(
        {
            "uos-a": {other_config: b"a-original\n"},
            "uos-b": {CONFIG_PATH: b"b-original\n"},
        }
    )

    def plans_for(path: str, changed: bytes):
        async def change(ctx) -> None:
            async with ctx.connect() as client:
                client.files[path] = changed

        return with_steps(default_plan(), IN_PROGRESS, step("改临时配置", change))

    outcomes = await asyncio.gather(
        run_collection(
            target_id="uos-a",
            run_id="run-a",
            connector=connector,
            scratch_dir=tmp_path / "a",
            plans=plans_for(other_config, b"a-changed\n"),
            options={"backup_paths": [other_config]},
        ),
        run_collection(
            target_id="uos-b",
            run_id="run-b",
            connector=connector,
            scratch_dir=tmp_path / "b",
            plans=plans_for(CONFIG_PATH, b"b-changed\n"),
            options={"backup_paths": [CONFIG_PATH]},
        ),
    )

    assert [outcome.status for outcome in outcomes] == [
        AttemptStatus.SUCCEEDED,
        AttemptStatus.SUCCEEDED,
    ]
    # 每台机器上的文件各自恢复成自己原来的内容，没有串台
    assert connector.files["uos-a"] == {other_config: b"a-original\n"}
    assert connector.files["uos-b"] == {CONFIG_PATH: b"b-original\n"}
    assert outcomes[0].attempt.data["system_info"]["hostname"] == "uos-a"
    assert outcomes[1].attempt.data["system_info"]["hostname"] == "uos-b"
    assert set(connector.connected) == {"uos-a", "uos-b"}
    assert "uos-b" not in connector.commands_of("uos-a")


async def test_reusing_a_record_of_another_run_is_rejected(tmp_path: Path) -> None:
    """把别的采集任务的记录塞进来时必须报错，不能接着写。"""
    connector = make_connector()
    other = await run_collection(
        target_id="another-target",
        run_id="run-1",
        connector=make_connector(),
        scratch_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="不属于这条采集任务"):
        await run(connector, tmp_path, previous=other.record)


class SelectiveSave:
    """按条件失败一次的保存回调，用来模拟写采集记录时数据库抖动。"""

    def __init__(self, should_fail) -> None:
        self.records = []
        self.failed = False
        self._should_fail = should_fail

    async def __call__(self, record) -> None:
        if not self.failed and self._should_fail(record.latest):
            self.failed = True
            raise RuntimeError("数据库写入失败")
        self.records.append(record)


def in_progress_step_finished(attempt) -> bool:
    """In Progress 已经跑完一个步骤：此时写记录失败，阶段还挂在 running。"""
    return attempt.phase(IN_PROGRESS).status is PhaseStatus.RUNNING and any(
        event.phase is IN_PROGRESS and event.kind == EventKind.STEP_FINISHED
        for event in attempt.events
    )


async def test_record_write_failure_still_runs_post(tmp_path: Path) -> None:
    """写采集记录失败（数据库抖动）不让清理阶段被跳过：远端照样恢复，本次尝试记失败。"""
    connector = make_connector()
    save = SelectiveSave(in_progress_step_finished)
    plans = with_steps(
        default_plan(),
        IN_PROGRESS,
        step("改临时配置", change_config(CHANGED_CONFIG)),
        step("启动采样", _noop),
        step("持续采集", _noop),
    )

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        save=save,
    )

    assert save.failed, "用例前提：写记录确实失败过一次"
    assert outcome.status is AttemptStatus.FAILED
    statuses = phase_statuses(outcome)
    assert statuses[PRE] is PhaseStatus.SUCCEEDED
    assert statuses[IN_PROGRESS] is PhaseStatus.FAILED
    assert statuses[POST] is PhaseStatus.SUCCEEDED
    detail = outcome.attempt.phase(IN_PROGRESS).detail
    assert detail is not None and "数据库写入失败" in detail
    # 清理真的执行了：改过的配置被写回原始内容
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    assert outcome.failure is not None and "数据库写入失败" in outcome.failure
    orchestration_events = [
        event for event in outcome.attempt.events if event.kind == EventKind.ORCHESTRATION_FAILED
    ]
    assert [event.phase for event in orchestration_events] == [IN_PROGRESS]


async def test_cleanup_failure_marks_the_run_failed_even_after_cancel(tmp_path: Path) -> None:
    """取消后清理失败不能算「按用户要求停下就结束」：本次尝试记失败，任务才会重试。"""
    connector = make_connector()
    state = {"requested": False}

    async def lose_local_backup(ctx) -> None:
        (ctx.scratch_dir / BACKUP_DIR_NAME / "0.bak").unlink()

    async def request_cancel(_ctx) -> None:
        state["requested"] = True

    async def cancelled() -> bool:
        return state["requested"]

    plan = default_plan()
    plans = {
        PRE: PhasePlan(
            phase=PRE,
            steps=(
                step("检查目标环境", inspect_target),
                step("备份系统配置", backup_configuration),
                step("改临时配置", change_config(CHANGED_CONFIG)),
                step("弄丢本地备份", lose_local_backup),
                step("请求取消", request_cancel),
            ),
        ),
        IN_PROGRESS: plan[IN_PROGRESS],
        POST: plan[POST],
    }

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        cancelled=cancelled,
    )

    assert phase_statuses(outcome)[IN_PROGRESS] is PhaseStatus.CANCELLED
    assert phase_statuses(outcome)[POST] is PhaseStatus.FAILED
    assert outcome.status is AttemptStatus.FAILED, "清理失败时不能沿用「取消即结束」"
    assert outcome.failure is not None
    assert "清理阶段" in outcome.failure
    # 远端确实没被恢复：这正是必须让任务失败并保留备份的原因
    assert connector.files[TARGET][CONFIG_PATH] == CHANGED_CONFIG


def interrupted_record_with_backup() -> CollectionRecord:
    """一条停在 Pre 阶段的上一次尝试记录（模拟进程被杀），带着已经做好的备份清单。"""
    record = CollectionRecord(run_id="run-1", target_id=TARGET)
    attempt = record.start_attempt()
    attempt.move(AttemptStatus.RUNNING)
    attempt.phase(PRE).move(PhaseStatus.RUNNING)
    attempt.data["backup"] = {"paths": [CONFIG_PATH], "files": {CONFIG_PATH: "0.bak"}}
    return record


async def test_backup_is_taken_once_per_collection(tmp_path: Path) -> None:
    """重试重跑 Pre 时不再下载备份，否则会把「已被改过」的远端内容当成原始配置。"""
    backup_dir = tmp_path / BACKUP_DIR_NAME
    backup_dir.mkdir()
    (backup_dir / "0.bak").write_bytes(ORIGINAL_CONFIG)
    connector = make_connector(config=CHANGED_CONFIG)
    previous = interrupted_record_with_backup()

    outcome = await run(
        connector, tmp_path, options={"backup_paths": [CONFIG_PATH]}, previous=previous
    )

    assert outcome.status is AttemptStatus.SUCCEEDED
    assert phase_statuses(outcome)[PRE] is PhaseStatus.SUCCEEDED
    assert connector.downloads_of(TARGET) == [], "同一次采集只备份一次"
    assert (backup_dir / "0.bak").read_bytes() == ORIGINAL_CONFIG
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG


async def _noop(_ctx) -> None:
    return None
