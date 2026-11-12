"""Systemd 生命周期重建的单元测试。

覆盖任务 2.1 的验收点：从固定 Journal fixture 重建生命周期并断言到毫秒级容差、同一 Unit 的不同
INVOCATION_ID 不合并、每条结论指回原始记录、数据不足时输出「生命周期不完整」且不补全区间、
按指定时间点查询状态、启动耗时。

fixture 是 ``tests/fixtures/systemd_lifecycle/boot.export``（``journalctl -o export`` 文本），
经任务 1.4 的解析与归一化路径变换成 ``journal.entry`` 事件记录，因此这里验证的正是「从已入库的
Journal 事件导出语义」，而不是重新解析压缩包。
"""

from __future__ import annotations

import copy as _copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from system_analyzer.analyzers.systemd_lifecycle import (
    ANNOTATION_ABNORMAL_EXIT,
    ANNOTATION_LIFECYCLE_INCOMPLETE,
    ANNOTATION_SERVICE_FAILED,
    ANNOTATION_STARTUP_FAILURE,
    ANNOTATION_UNATTRIBUTED,
    PLUGIN_ID,
    SPAN_ACTIVATING,
    SPAN_ACTIVE,
    SignalKind,
    classify_message,
    journal_signals,
    reconstruct,
)
from system_analyzer.domain.bootrec import PAYLOAD_MODELS, Record, compute_record_id
from system_analyzer.importers import journal_archive as archive

FIXTURE = Path(__file__).parent / "fixtures" / "systemd_lifecycle" / "boot.export"

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "7f2c5d4e-9a1b-4c3d-8e2f-1a2b3c4d5e6f"
COLLECTION_ID = f"journal-import:{MACHINE_ID}:{BOOT_ID}"

NGINX_INVOCATION = "2f9c6a0d1b3e4f5a8c7d9e0f1a2b3c4d"
POSTGRESQL_INVOCATION = "aa11bb22cc33dd44ee55ff6677889900"
REDIS_FIRST_INVOCATION = "11111111111111111111111111111111"
REDIS_SECOND_INVOCATION = "22222222222222222222222222222222"
SSHD_INVOCATION = "7c8d9e0f1a2b3c4d5e6f70819a2b3c4d"

MILLISECOND = 1_000_000
"""断言容差：毫秒级。日志时间戳是微秒精度，换算成纳秒后低三位恒为 0。"""


def fixture_records() -> tuple[dict[str, Any], ...]:
    """固定 fixture 经 1.4 的解析路径得到的 ``journal.entry`` 事件记录。"""
    parsed = archive.parse_journal_archive(FIXTURE)
    bundle = archive.build_bundle(parsed, source_sha256="0" * 64)
    return bundle.records


def journal_event(
    *,
    monotonic_ms: int,
    unit: str,
    message: str,
    invocation: str | None = None,
    identifier: str = "systemd",
    sender_unit: str | None = None,
    pid: int = 1,
    cgroup: str = "/system.slice/test.service",
    collection: str = COLLECTION_ID,
    extra_fields: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构造一条 ``journal.entry`` 事件记录。

    ``sender_unit`` 是 ``_SYSTEMD_UNIT``（写日志的进程所属的 Unit），默认与被分析的 Unit 相同；
    真实 Journal 里 systemd 自己写的消息这两者不同（见 ``extra_fields`` 里的 ``UNIT``）。
    """
    sender = sender_unit or unit
    fields: dict[str, str] = {
        "SYSLOG_IDENTIFIER": identifier,
        "_SYSTEMD_UNIT": sender,
        "_PID": str(pid),
        "_SYSTEMD_CGROUP": cgroup,
    }
    if invocation is not None:
        fields["_SYSTEMD_INVOCATION_ID"] = invocation
    fields.update(extra_fields or {})
    attributes: dict[str, Any] = {
        "derived_from": "journal",
        "identifier": identifier,
        "unit": sender,
        "fields": fields,
    }
    if invocation is not None:
        attributes["invocation_id"] = invocation
    body: dict[str, Any] = {
        "kind": "event",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": collection,
        "source_plugin": "journal.archive",
        "stream_id": "journal",
        "monotonic_ns": monotonic_ms * MILLISECOND,
        "realtime_ns": 1_794_000_000 * 1_000_000_000 + monotonic_ms * MILLISECOND,
        "clock_domain": "boot_monotonic",
        "entity": f"service:{sender}",
        "payload": {
            "event_type": "journal.entry",
            "severity": "info",
            "message": message,
            "attributes": attributes,
        },
    }
    return {"record_id": compute_record_id(body), **body}


def with_changes(record: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    """改几个字段并按内容重算 ``record_id``（记录 id 是内容摘要）。"""
    body = {key: value for key, value in record.items() if key != "record_id"}
    body.update(changes)
    return {"record_id": compute_record_id(body), **body}


def assert_close(actual: int | None, expected_ms: int) -> None:
    """断言时刻落在期望的毫秒附近（容差 1 毫秒）。"""
    assert actual is not None, "期望有值，实际是 None"
    assert abs(actual - expected_ms * MILLISECOND) <= MILLISECOND


def segments_of(unit: str) -> list[Any]:
    return [segment for segment in reconstruct(fixture_records()).segments if segment.unit == unit]


# --- 从固定 fixture 重建生命周期 ---------------------------------------------


def test_fixture_rebuilds_the_expected_lifecycle_intervals() -> None:
    """nginx 有完整的 Starting→Started→Stopped 证据，四个时刻都必须对上。"""
    (nginx,) = segments_of("nginx.service")

    assert_close(nginx.activation_start_ns, 1000)
    assert_close(nginx.activation_end_ns, 1480)
    assert_close(nginx.active_start_ns, 1480)
    assert_close(nginx.active_end_ns, 9120)
    assert nginx.terminal_state == "inactive"
    assert nginx.incomplete_reasons == ()
    assert nginx.complete is True


def test_span_records_carry_the_interval_and_the_systemd_state() -> None:
    reconstruction = reconstruct(fixture_records())
    spans = [
        record
        for record in reconstruction.span_records()
        if record["payload"]["attributes"]["unit"] == "nginx.service"
    ]

    assert [record["payload"]["span_type"] for record in spans] == [
        SPAN_ACTIVATING,
        SPAN_ACTIVE,
    ]
    activating, active = spans
    assert_close(activating["monotonic_ns"], 1000)
    assert_close(activating["payload"]["end_monotonic_ns"], 1480)
    assert activating["payload"]["state"] == "active"
    assert activating["payload"]["attributes"]["startup_duration_ns"] == 480 * MILLISECOND
    assert_close(active["monotonic_ns"], 1480)
    assert_close(active["payload"]["end_monotonic_ns"], 9120)
    assert active["payload"]["state"] == "inactive"
    assert active["entity"] == "service:nginx.service"
    assert active["clock_domain"] == "boot_monotonic"


def test_segments_keep_unit_pid_invocation_and_cgroup_together() -> None:
    """关联 Unit、PID、Invocation ID、cgroup：四个字段落在同一段区间上。"""
    (nginx,) = segments_of("nginx.service")

    assert nginx.entity == "service:nginx.service"
    assert nginx.invocation_id == NGINX_INVOCATION
    assert nginx.pid == 812
    assert nginx.cgroup == "/system.slice/nginx.service"

    span = next(
        record
        for record in reconstruct(fixture_records()).span_records()
        if record["payload"]["attributes"].get("invocation_id") == NGINX_INVOCATION
        and record["payload"]["span_type"] == SPAN_ACTIVE
    )
    assert span["payload"]["attributes"] == {
        "unit": "nginx.service",
        "invocation_id": NGINX_INVOCATION,
        "pid": 812,
        "cgroup": "/system.slice/nginx.service",
    }


def test_startup_duration_comes_from_the_activation_phase() -> None:
    reconstruction = reconstruct(fixture_records())
    durations = {
        (item.unit, item.invocation_id): item.duration_ns
        for item in reconstruction.startup_durations()
    }

    assert durations[("nginx.service", NGINX_INVOCATION)] == 480 * MILLISECOND
    assert durations[("postgresql.service", POSTGRESQL_INVOCATION)] == 640 * MILLISECOND
    # redis 没有 Starting 记录，激活开始时刻未知：这一项明确写 None，不给估算值
    assert ("redis.service", REDIS_SECOND_INVOCATION) in durations
    assert durations[("redis.service", REDIS_SECOND_INVOCATION)] is None


# --- 同一 Unit 的不同 Invocation ID 不合并 -----------------------------------


def test_different_invocation_ids_are_not_merged() -> None:
    """redis 两次激活的 INVOCATION_ID 不同，必须变成两段、两条运行区间。"""
    redis = segments_of("redis.service")

    assert len(redis) == 2
    assert [segment.invocation_id for segment in redis] == [
        REDIS_FIRST_INVOCATION,
        REDIS_SECOND_INVOCATION,
    ]
    assert [segment.pid for segment in redis] == [2001, 2002]
    first, second = redis
    assert_close(first.active_start_ns, 3000)
    assert_close(second.active_start_ns, 7000)
    assert_close(second.active_end_ns, 8000)

    spans = [
        record
        for record in reconstruct(fixture_records()).span_records()
        if record["payload"]["span_type"] == SPAN_ACTIVE
        and record["payload"]["attributes"]["unit"] == "redis.service"
    ]
    assert len(spans) == 2
    assert spans[0]["payload"]["attributes"]["invocation_id"] == REDIS_FIRST_INVOCATION
    assert spans[1]["payload"]["attributes"]["invocation_id"] == REDIS_SECOND_INVOCATION


def test_a_repeated_started_line_with_the_same_invocation_stays_one_segment() -> None:
    """同一次激活里重复打印的 Started 不能被拆成两次运行。"""
    records = [
        journal_event(
            monotonic_ms=1000,
            unit="nginx.service",
            message="Started A high performance web server.",
            invocation=NGINX_INVOCATION,
        ),
        journal_event(
            monotonic_ms=1000,
            unit="nginx.service",
            message="Started A high performance web server.",
            invocation=NGINX_INVOCATION,
        ),
    ]

    (segment,) = reconstruct(records).segments
    assert segment.invocation_id == NGINX_INVOCATION
    assert_close(segment.active_start_ns, 1000)


# --- 启动失败与异常退出 ------------------------------------------------------


def test_startup_failure_is_reported_with_the_start_evidence() -> None:
    (postgresql,) = segments_of("postgresql.service")
    assert postgresql.active_start_ns is None
    assert postgresql.terminal_state == "failed"

    conclusions = {
        conclusion.annotation_type: conclusion
        for conclusion in reconstruct(fixture_records()).conclusions
        if conclusion.unit == "postgresql.service"
    }

    assert set(conclusions) == {ANNOTATION_SERVICE_FAILED, ANNOTATION_STARTUP_FAILURE}
    startup_failure = conclusions[ANNOTATION_STARTUP_FAILURE]
    assert startup_failure.severity == "error"
    assert "启动失败" in startup_failure.message
    assert "640.000 毫秒" in startup_failure.message
    assert len(startup_failure.evidence_refs) == 2


def test_abnormal_exit_is_reported_for_a_non_zero_exit_status() -> None:
    conclusions = [
        conclusion
        for conclusion in reconstruct(fixture_records()).conclusions
        if conclusion.unit == "sshd.service"
    ]
    by_type = {conclusion.annotation_type: conclusion for conclusion in conclusions}

    assert ANNOTATION_ABNORMAL_EXIT in by_type
    abnormal = by_type[ANNOTATION_ABNORMAL_EXIT]
    assert "status=1/FAILURE" in abnormal.message
    assert abnormal.severity == "error"


def test_failure_without_start_evidence_is_not_called_a_startup_failure() -> None:
    """只有 Failed with result、没有 Starting：只能说 Unit 失败，不能说「启动失败」。"""
    conclusions = {
        conclusion.annotation_type
        for conclusion in reconstruct(fixture_records()).conclusions
        if conclusion.unit == "sshd.service"
    }

    assert ANNOTATION_SERVICE_FAILED in conclusions
    assert ANNOTATION_STARTUP_FAILURE not in conclusions


# --- 数据不足：输出「生命周期不完整」，不补全区间 -----------------------------


def test_incomplete_lifecycle_is_reported_and_the_interval_is_not_completed() -> None:
    """redis 第一段只有 Started，既没有 Starting 也没有停止：结束时刻必须保持 null。"""
    first, _second = segments_of("redis.service")
    conclusions = [
        conclusion
        for conclusion in reconstruct(fixture_records()).conclusions
        if conclusion.unit == "redis.service"
        and conclusion.annotation_type == ANNOTATION_LIFECYCLE_INCOMPLETE
    ]

    assert first.active_end_ns is None
    assert first.activation_end_ns is not None  # 运行开始时刻是有的
    assert "生命周期不完整" in conclusions[0].message
    assert "缺少启动（Starting）记录" in conclusions[0].message
    assert conclusions[0].evidence_refs

    # 没有任何区间用「下一段激活的开始时刻」补上第一段的结束时刻
    starts = {
        record["monotonic_ns"]
        for record in reconstruct(fixture_records()).span_records()
        if record["payload"]["span_type"] == SPAN_ACTIVE
    }
    assert first.active_end_ns not in starts


def test_every_conclusion_points_back_to_a_journal_record() -> None:
    records = fixture_records()
    known_ids = {record["record_id"] for record in records}
    reconstruction = reconstruct(records)

    assert reconstruction.conclusions
    for conclusion in reconstruction.conclusions:
        assert conclusion.evidence_refs, f"{conclusion.annotation_type} 没有证据"
        assert set(conclusion.evidence_refs) <= known_ids
    for record in reconstruction.annotation_records():
        assert record["payload"]["evidence_refs"]
        assert set(record["payload"]["evidence_refs"]) <= known_ids


# --- 记录形状：0.2 的统一记录 + 内容摘要 -------------------------------------


def test_derived_records_are_valid_unified_records_with_content_ids() -> None:
    records = reconstruct(fixture_records()).records()

    assert records
    for record in records:
        body = {key: value for key, value in record.items() if key != "record_id"}
        assert record["record_id"] == compute_record_id(body)
        parsed = Record.model_validate(record)
        PAYLOAD_MODELS[parsed.kind].model_validate(parsed.payload)
        assert parsed.source_plugin == PLUGIN_ID
        assert parsed.collection_id == COLLECTION_ID
        assert parsed.machine_id == MACHINE_ID
        assert parsed.boot_id == BOOT_ID


def test_annotations_record_the_analyzer_and_no_fake_confidence() -> None:
    annotations = reconstruct(fixture_records()).annotation_records()

    assert annotations
    for record in annotations:
        payload = record["payload"]
        assert payload["analyzer"] == PLUGIN_ID
        assert payload["confidence"] is None
        assert payload["annotation_type"].startswith("systemd.")
        assert record["kind"] == "annotation"


def test_analysis_is_deterministic() -> None:
    """同样的输入必须得到同样的记录（含 record_id），重复分析才不会互相矛盾。"""
    first = reconstruct(fixture_records()).records()
    second = reconstruct(fixture_records()).records()

    assert [record["record_id"] for record in first] == [record["record_id"] for record in second]


# --- 按时间点查询状态 --------------------------------------------------------


def test_state_at_returns_the_phase_the_unit_was_in() -> None:
    reconstruction = reconstruct(fixture_records())

    at_1200 = {item.unit: item for item in reconstruction.state_at(1200 * MILLISECOND)}
    assert at_1200["nginx.service"].state == "activating"
    assert at_1200["nginx.service"].stale is False

    at_2500 = {item.unit: item for item in reconstruction.state_at(2500 * MILLISECOND)}
    assert at_2500["nginx.service"].state == "active"
    assert at_2500["postgresql.service"].state == "activating"

    at_5000 = {item.unit: item for item in reconstruction.state_at(5000 * MILLISECOND)}
    assert at_5000["postgresql.service"].state == "failed"
    assert at_5000["postgresql.service"].stale is True
    assert at_5000["redis.service"].state == "active"
    assert at_5000["redis.service"].invocation_id == REDIS_FIRST_INVOCATION

    at_8500 = {item.unit: item for item in reconstruction.state_at(8500 * MILLISECOND)}
    assert at_8500["redis.service"].state == "inactive"
    assert at_8500["redis.service"].invocation_id == REDIS_SECOND_INVOCATION
    assert at_8500["nginx.service"].state == "active"


def test_state_at_before_any_evidence_returns_nothing() -> None:
    """没有证据的时刻不说「没在运行」：把「没观测到」说成「不存在」就是编造。"""
    assert reconstruct(fixture_records()).state_at(300 * MILLISECOND) == ()


def test_state_at_reports_how_complete_the_covering_segment_is() -> None:
    at_4000 = {
        item.unit: item for item in reconstruct(fixture_records()).state_at(4_000 * MILLISECOND)
    }

    assert at_4000["nginx.service"].complete is True
    assert at_4000["redis.service"].complete is False


# --- 只认 systemd 的生命周期消息 ---------------------------------------------


def test_messages_from_other_programs_are_ignored() -> None:
    """业务程序打印的「Started ...」不是生命周期消息。"""
    records = fixture_records()
    signals = journal_signals(records)

    assert all("serving requests" not in signal.message for signal in signals)
    redis = [signal for signal in signals if signal.unit == "redis.service"]
    assert len(redis) == 3  # 两次 Started + 一次 Deactivated，噪音不算


def test_normal_process_exit_is_not_an_abnormal_exit() -> None:
    records = [
        journal_event(
            monotonic_ms=1000,
            unit="nginx.service",
            message="nginx.service: Main process exited, code=exited, status=0/SUCCESS",
            invocation=NGINX_INVOCATION,
        )
    ]

    assert journal_signals(records) == ()


@pytest.mark.parametrize(
    ("message", "kind", "detail"),
    [
        ("Starting PostgreSQL RDBMS...", SignalKind.STARTING, {}),
        ("Started A high performance web server.", SignalKind.STARTED, {}),
        ("Stopping PostgreSQL RDBMS...", SignalKind.STOPPING, {}),
        ("Stopped PostgreSQL RDBMS.", SignalKind.STOPPED, {}),
        ("Failed to start PostgreSQL RDBMS.", SignalKind.ACTIVATION_FAILED, {}),
        (
            "postgresql.service: Failed with result 'exit-code'.",
            SignalKind.FAILED_RESULT,
            {"result": "exit-code"},
        ),
        ("Unit entered failed state.", SignalKind.FAILED_RESULT, {"result": None}),
        ("nginx.service: Deactivated successfully.", SignalKind.STOPPED, {}),
        ("systemd-udevd.service: Succeeded.", SignalKind.STOPPED, {}),
        (
            "nginx.service: Main process exited, code=killed, status=9/KILL",
            SignalKind.MAIN_EXIT,
            {"exit_code": "killed", "exit_status": "9/KILL"},
        ),
        ("a business program wrote this line", None, None),
    ],
)
def test_message_classification(
    message: str, kind: SignalKind | None, detail: dict[str, Any] | None
) -> None:
    classified = classify_message(message)

    if kind is None:
        assert classified is None
        return
    assert classified is not None
    assert classified[0] is kind
    assert classified[1] == detail


# --- 采集边界 ----------------------------------------------------------------


def test_segments_never_cross_collection_boundaries() -> None:
    """开放区间只在所属采集的窗口内有效（time-base 第 6 节），跨采集不拼接。"""
    first = journal_event(
        monotonic_ms=1000,
        unit="nginx.service",
        message="Starting A high performance web server...",
        invocation=NGINX_INVOCATION,
    )
    second = with_changes(
        first,
        collection_id="collection:other",
        payload={**first["payload"], "message": "Stopped A high performance web server."},
    )

    reconstruction = reconstruct([first, second])

    # 两次采集各成一段，没有一段跨在两个采集上
    assert sorted(segment.collection_id for segment in reconstruction.segments) == sorted(
        [COLLECTION_ID, "collection:other"]
    )
    assert all(segment.incomplete_reasons for segment in reconstruction.segments)


def test_records_without_monotonic_clock_are_not_placed_on_the_boot_axis() -> None:
    record = with_changes(
        journal_event(
            monotonic_ms=1000,
            unit="nginx.service",
            message="Started A high performance web server.",
            invocation=NGINX_INVOCATION,
        ),
        monotonic_ns=None,
        clock_domain="wall_realtime",
    )

    assert journal_signals([record]) == ()


def test_no_journal_evidence_yields_no_segments() -> None:
    reconstruction = reconstruct([])

    assert reconstruction.segments == ()
    assert reconstruction.conclusions == ()
    assert reconstruction.records() == ()
    assert reconstruction.state_at(1000) == ()


def test_signal_record_ids_are_the_input_record_ids() -> None:
    records = fixture_records()
    by_id: Mapping[str, Any] = {record["record_id"]: record for record in records}

    for signal in journal_signals(records):
        assert signal.record_id in by_id
        assert signal.monotonic_ns == by_id[signal.record_id]["monotonic_ns"]


# --- 真实 Journal（journalctl -o export 原样截取） ----------------------------

REAL_FIXTURE = Path(__file__).parent / "fixtures" / "systemd_lifecycle" / "real-boot.journal.gz"
"""由本机 ``journalctl -o export`` 原样截取的 36 条生命周期消息，覆盖 11 个 Unit。

真实数据的关键差异：systemd 由 PID 1 打印的消息上 ``_SYSTEMD_UNIT`` 恒为 ``init.scope``，
被操作的 Unit 放在 ``UNIT=``；user manager 打印的消息是 ``USER_UNIT=`` / ``USER_INVOCATION_ID=``，
``_SYSTEMD_UNIT`` 是 ``user@1000.service``。手工拼的 fixture 覆盖不到这一点。
"""

REAL_NM_DISPATCHER = "NetworkManager-dispatcher.service"


def real_records() -> tuple[dict[str, Any], ...]:
    parsed = archive.parse_journal_archive(REAL_FIXTURE)
    bundle = archive.build_bundle(parsed, source_sha256="0" * 64)
    return bundle.records


def test_real_journal_rebuilds_the_real_units() -> None:
    """真实数据的 Unit 必须来自 ``UNIT=`` / ``USER_UNIT=``，而不是日志发送方的 init.scope。"""
    units = set(reconstruct(real_records()).units())

    assert len(units) == 11
    assert "init.scope" not in units
    assert "user@1000.service" not in units
    assert REAL_NM_DISPATCHER in units
    assert any(unit.startswith("cri-containerd-") for unit in units)
    assert any(unit.startswith("ca511ece") and unit.endswith(".service") for unit in units)


def test_real_restarts_with_different_invocations_stay_separate() -> None:
    """真实数据里 NetworkManager-dispatcher.service 连续激活四次，每次 INVOCATION_ID 都不同。"""
    segments = reconstruct(real_records()).segments
    dispatcher = [segment for segment in segments if segment.unit == REAL_NM_DISPATCHER]

    assert len(dispatcher) == 4
    assert len({segment.invocation_id for segment in dispatcher}) == 4
    assert all(segment.complete for segment in dispatcher)
    for segment in dispatcher:
        assert segment.invocation_id is not None
        assert segment.startup_duration_ns is not None
        assert segment.startup_duration_ns > 0
        assert segment.startup_duration_ns == (
            segment.activation_end_ns - segment.activation_start_ns
        )


def test_real_user_manager_units_use_their_own_invocation_id() -> None:
    segments = reconstruct(real_records()).segments
    user_units = [segment for segment in segments if segment.unit.startswith("ca511ece")]

    assert user_units
    assert len({segment.invocation_id for segment in user_units}) == len(user_units)
    assert all(segment.terminal_state == "failed" for segment in user_units)


def test_process_identifiers_are_only_attached_when_the_unit_wrote_the_log() -> None:
    """PID 1 写的消息上 ``_PID`` / ``_SYSTEMD_CGROUP`` 是 systemd 自己的，不能算在服务头上。"""
    reconstruction = reconstruct(real_records())
    dispatcher_spans = [
        record
        for record in reconstruction.span_records()
        if record["payload"]["attributes"]["unit"] == REAL_NM_DISPATCHER
    ]

    assert dispatcher_spans
    for record in dispatcher_spans:
        attributes = record["payload"]["attributes"]
        assert "invocation_id" in attributes  # 来自 INVOCATION_ID，属于该 Unit
        assert "pid" not in attributes
        assert "cgroup" not in attributes
        assert record["entity"] == f"service:{REAL_NM_DISPATCHER}"


def test_real_journal_spans_reference_service_entities_of_the_real_units() -> None:
    reconstruction = reconstruct(real_records())

    entities = {entity.entity_id for entity in reconstruction.service_entities()}
    referenced = {record["entity"] for record in reconstruction.span_records()}
    assert referenced
    assert referenced <= entities
    assert entities == {f"service:{unit}" for unit in reconstruction.units()}


# --- 真实场景：同一 boot 多次采集时的状态查询 --------------------------------


def test_state_at_merges_evidence_from_every_collection_of_the_boot() -> None:
    """一次采集里有完整的停止证据，另一次只有 Started：查询不能返回与完整证据矛盾的答案。"""
    complete = [
        journal_event(monotonic_ms=1000, unit="web.service", message="Starting web server..."),
        journal_event(monotonic_ms=2000, unit="web.service", message="Started web server."),
        journal_event(monotonic_ms=3000, unit="web.service", message="Stopped web server."),
    ]
    partial = [
        journal_event(
            monotonic_ms=2500,
            unit="web.service",
            message="Started web server.",
            invocation="22222222222222222222222222222222",
            collection="collection:second",
        )
    ]
    reconstruction = reconstruct([*complete, *partial])

    at_2800 = {item.unit: item for item in reconstruction.state_at(2800 * MILLISECOND)}
    assert at_2800["web.service"].state == "active"
    assert at_2800["web.service"].stale is False  # 3000ms 还有停止证据

    at_5000 = {item.unit: item for item in reconstruction.state_at(5000 * MILLISECOND)}
    assert at_5000["web.service"].state == "inactive"
    assert at_5000["web.service"].stale is True


def test_open_interval_is_stale_once_the_evidence_runs_out() -> None:
    """开放区间在日志用尽之后必须标记为「最后一次观测」，不能一直说「还在运行」。"""
    reconstruction = reconstruct(
        [
            journal_event(monotonic_ms=1000, unit="web.service", message="Starting web server..."),
            journal_event(monotonic_ms=2000, unit="web.service", message="Started web server."),
        ]
    )

    # 这段日志的证据只到 2000ms：之后两个时刻都只能说「最后一次观测到的状态」
    at_2100 = {item.unit: item for item in reconstruction.state_at(2100 * MILLISECOND)}
    assert at_2100["web.service"].state == "active"
    assert at_2100["web.service"].stale is True

    at_10000 = {item.unit: item for item in reconstruction.state_at(10000 * MILLISECOND)}
    assert at_10000["web.service"].state == "active"
    assert at_10000["web.service"].stale is True


# --- Unit 取自结构化字段，发送方标识不冒充 --------------------------------


def test_object_unit_field_wins_over_the_sender_unit() -> None:
    record = journal_event(
        monotonic_ms=1000,
        unit="nginx.service",
        sender_unit="init.scope",
        message="Started A high performance web server.",
        extra_fields={
            "UNIT": "nginx.service",
            "INVOCATION_ID": "33333333333333333333333333333333",
        },
    )
    (signal,) = journal_signals([record])

    assert signal.unit == "nginx.service"
    assert signal.entity == "service:nginx.service"
    assert signal.invocation_id == "33333333333333333333333333333333"
    # 发送方是 PID 1：PID 与 cgroup 不是这个服务的
    assert signal.pid is None
    assert signal.cgroup is None


def test_a_failure_after_a_change_of_invocation_starts_a_new_segment() -> None:
    """只拿到失败记录的 Unit 连着失败两次：编号变了就是两次运行，不能并成一段。"""
    records = [
        journal_event(
            monotonic_ms=1000,
            unit="job.service",
            message="job.service: Failed with result 'exit-code'.",
            invocation="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sender_unit="job.service",
        ),
        journal_event(
            monotonic_ms=5000,
            unit="job.service",
            message="job.service: Failed with result 'exit-code'.",
            invocation="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            sender_unit="job.service",
        ),
    ]

    segments = reconstruct(records).segments
    assert len(segments) == 2
    assert [segment.invocation_id for segment in segments] == [
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ]


# --- 缺少结构化 Unit 字段：不产出结论，只报数量与原因 ------------------------


def records_without_structured_unit_fields() -> tuple[dict[str, Any], ...]:
    """把真实导出里「说的是哪个 Unit」的字段全部去掉，模拟旧版导出与第三方转存。

    systemd v250 之前不用 ``log_struct``，生命周期消息上只剩 ``_SYSTEMD_UNIT``——PID 1 的日志上
    它是 ``init.scope``，user manager 的日志上是 ``user@1000.service``。这时没有任何可信依据判断
    这些消息说的是哪个 Unit。
    """
    stripped: list[dict[str, Any]] = []
    for record in real_records():
        record_copy = _copy.deepcopy(record)
        fields = record_copy["payload"]["attributes"]["fields"]
        for name in ("UNIT", "USER_UNIT", "INVOCATION_ID", "USER_INVOCATION_ID"):
            fields.pop(name, None)
        # 内容变了，record_id 也要按内容重算：否则这份测试数据自己就违反了 0.2 第 8 节
        record_copy["record_id"] = compute_record_id(record_copy)
        stripped.append(record_copy)
    return tuple(stripped)


def test_missing_structured_unit_never_claims_the_systemd_manager_scope() -> None:
    """拿不到明确的 Unit 时不产出生命周期区间：init.scope 不是「被分析的服务」。"""
    reconstruction = reconstruct(records_without_structured_unit_fields())

    assert reconstruction.units() == ()
    assert reconstruction.span_records() == ()
    assert reconstruction.service_entities() == ()


def test_unattributed_messages_are_reported_with_count_and_reason() -> None:
    """丢掉的消息必须在结果里说清楚：多少条、为什么、证据是哪些原始记录。"""
    records = records_without_structured_unit_fields()
    reported = [
        record
        for record in reconstruct(records).annotation_records()
        if record["payload"]["annotation_type"] == ANNOTATION_UNATTRIBUTED
    ]

    assert len(reported) == 1
    payload = reported[0]["payload"]
    assert f"{len(records)} 条生命周期消息缺少结构化 Unit 字段" in payload["message"]
    assert "未产出结论" in payload["message"]
    assert payload["evidence_refs"]
    assert set(payload["evidence_refs"]) <= {record["record_id"] for record in records}


def test_fully_attributed_real_journal_reports_nothing_as_unattributed() -> None:
    """正常路径不能凭空多出这条结论：真实数据每条都带 UNIT=。"""
    annotations = reconstruct(real_records()).annotation_records()

    assert [
        record
        for record in annotations
        if record["payload"]["annotation_type"] == ANNOTATION_UNATTRIBUTED
    ] == []


def test_sender_unit_is_still_used_when_it_provably_is_the_unit() -> None:
    """没有 UNIT= 时，发送方不是 systemd 自己的作用域就可以代表目标 Unit。"""
    records = [
        journal_event(
            monotonic_ms=1000,
            unit="nginx.service",
            sender_unit="nginx.service",
            message="Started A high performance web server.",
        )
    ]

    (signal,) = journal_signals(records)
    assert signal.unit == "nginx.service"
    assert signal.pid == 1  # 发送方就是服务自己，PID／cgroup 属于它
