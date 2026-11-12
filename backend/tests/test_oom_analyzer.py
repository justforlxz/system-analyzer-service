"""OOM 与进程异常退出分析的单元测试（任务 2.2）。

规则部分不连库：直接构造 1.4 会写出的 ``event`` 记录，断言认出的 OOM 事实、写出的
``annotation`` 记录，以及「直接证据」与「只靠时间接近的疑似关联」两类结论的差别。
插件入口按 ``docs/plugin-contracts.md`` 第 4 节经 ``PluginRegistry.invoke`` 调用。
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

import pytest

from system_analyzer.analyzers.oom import (
    ANNOTATION_TYPE_KERNEL,
    ANNOTATION_TYPE_SYSTEMD_OOMD,
    CONFIDENCE_KILL_ONLY,
    CONFIDENCE_PROXIMITY,
    CONFIDENCE_UNIT_FROM_SAME_LINE,
    CONFIG_PROXIMITY_WINDOW_NS,
    DEFAULT_PROXIMITY_WINDOW_NS,
    OomAnalyzer,
    detect_kill,
    plan_annotations,
)
from system_analyzer.domain.bootrec import (
    Record,
    RecordKind,
    Severity,
    compute_record_id,
)
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    AnalysisRequest,
    AnalysisResult,
    AnalyzerPlugin,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
    PluginSpec,
)
from system_analyzer.plugins.registry import PluginRegistry

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
OTHER_BOOT_ID = "11111111-2222-3333-4444-555555555555"

KERNEL_OOM = (
    "Out of memory: Killed process 4310 (firefox) total-vm:2846920kB, "
    "anon-rss:1804808kB, file-rss:1024kB, shmem-rss:0kB, UID:1000 oom_score_adj:0"
)
OOMD_KILL = (
    "Killed /user.slice/user-1000.slice/session-2.scope due to memory pressure "
    "92.00% > 80.00% for > 10s"
)
UNIT_FAILED = "firefox.service: Failed with result 'oom-kill'."


def journal_event(
    message: str,
    *,
    monotonic_ns: int,
    fields: dict[str, str] | None = None,
    entity: str | None = None,
    boot_id: str = BOOT_ID,
) -> Record:
    """构造一条 1.4 会写出的 ``event`` 记录：``derived_from=journal``，原始字段在 ``fields``。"""
    raw = dict(fields or {})
    attributes: dict[str, Any] = {"derived_from": "journal", "priority": 3, "fields": raw}
    for name, field_name in (("unit", "_SYSTEMD_UNIT"), ("identifier", "SYSLOG_IDENTIFIER")):
        if field_name in raw:
            attributes[name] = raw[field_name]
    body: dict[str, Any] = {
        "kind": RecordKind.EVENT.value,
        "machine_id": MACHINE_ID,
        "boot_id": boot_id,
        "collection_id": f"journal-import:{MACHINE_ID}:{boot_id}",
        "source_plugin": "journal.archive",
        "stream_id": "journal.entries",
        "monotonic_ns": monotonic_ns,
        "realtime_ns": 1_794_000_000_000_000_000 + monotonic_ns,
        "clock_domain": "boot_monotonic",
        "entity": entity,
        "payload": {
            "event_type": "journal.entry",
            "severity": "error",
            "message": message,
            "attributes": attributes,
        },
    }
    return Record.model_validate({"record_id": compute_record_id(body), **body})


def kernel_oom_record() -> Record:
    return journal_event(KERNEL_OOM, monotonic_ns=175_831_201_539, fields={"_TRANSPORT": "kernel"})


def unit_started_record() -> Record:
    """同一个 Unit 的一条正常日志：它的 ``entity`` 让库里的 ``service:<unit>`` 实体可被认出来。"""
    return journal_event(
        "Started firefox.service.",
        monotonic_ns=175_830_000_000,
        fields={"_SYSTEMD_UNIT": "firefox.service"},
        entity="service:firefox.service",
    )


def unit_failed_record(start_ns: int, delay_ns: int) -> Record:
    return journal_event(
        UNIT_FAILED,
        monotonic_ns=start_ns + delay_ns,
        fields={"_SYSTEMD_UNIT": "firefox.service"},
        entity="service:firefox.service",
    )


def oomd_kill_record(delay_ns: int = 900_000_000) -> Record:
    return journal_event(
        OOMD_KILL,
        monotonic_ns=175_831_201_539 + delay_ns,
        fields={
            "SYSLOG_IDENTIFIER": "systemd-oomd",
            "_SYSTEMD_UNIT": "systemd-oomd.service",
            "_PID": "720",
        },
    )


def two_source_records() -> list[Record]:
    """一份固定的两类 OOM 事件：内核 OOM 一行、systemd-oomd 一行，外加几条无关日志。"""
    return [
        unit_started_record(),
        kernel_oom_record(),
        unit_failed_record(175_831_201_539, 30_000_000),
        oomd_kill_record(),
        journal_event(
            "Considered 3 cgroups and found 1 with pressure above threshold.",
            monotonic_ns=175_831_100_000,
            fields={"SYSLOG_IDENTIFIER": "systemd-oomd"},
        ),
    ]


class FakeAnalysisData:
    """``PluginDataAccess`` 的替身：按插件要求记下写了什么，够断言即可。"""

    def __init__(self, records: Sequence[Record], *, fail_with: Exception | None = None) -> None:
        self.records = list(records)
        self.written: list[dict[str, Any]] = []
        self.queried_kinds: tuple[str, ...] = ()
        self.fail_with = fail_with

    def append_records(self, records: Sequence[Any], *, plugin_id: str, source: str) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        self.written.extend(records)
        return len(records)

    def query_records(
        self, *, record_kinds: Sequence[str] = (), limit: int | None = None
    ) -> Sequence[Any]:
        self.queried_kinds = tuple(record_kinds)
        return self.records


class AsyncAnalysisData:
    """协程版数据入口：模拟 Core 没把存储层包成同步入口的情况。"""

    async def append_records(self, records: Any, *, plugin_id: str, source: str) -> int:
        return 0

    async def query_records(self, *, record_kinds: Any = (), limit: Any = None) -> Any:
        return ()


def run_analyzer(
    records: Sequence[Record],
    *,
    request: AnalysisRequest | None = None,
    config: dict[str, Any] | None = None,
    data: Any = None,
) -> AnalysisResult:
    """按 Core 的接法调插件。

    经 ``PluginRegistry.invoke`` 调用，配置与数据入口都从 ``PluginContext`` 进。
    """
    registry = register_builtins(PluginRegistry())
    access = data if data is not None else FakeAnalysisData(records)
    return registry.invoke(
        OomAnalyzer.spec.id,
        lambda plugin, context: plugin.analyze(
            request or AnalysisRequest(boot_id=BOOT_ID), context
        ),
        data=access,
        config=config or {},
        run_id="run-oom-1",
    )


# ---------------------------------------------------------------------------
# 规则：两类 OOM 各自的直接证据
# ---------------------------------------------------------------------------


def test_kernel_oom_line_gives_pid_and_process_from_its_own_message() -> None:
    kill = detect_kill(kernel_oom_record())

    assert kill is not None
    assert kill.source == "kernel"
    assert kill.pid == 4310
    assert kill.process == "firefox"
    # 内核那一行没有 Unit 字段：这里不给出 Unit，不从别处补
    assert kill.unit is None
    assert kill.unit_field is None
    assert "Killed process 4310 (firefox)" in kill.quote


@pytest.mark.parametrize(
    ("message", "pid", "process"),
    [
        ("Memory cgroup out of memory: Killed process 77 (java) total-vm:1024kB", 77, "java"),
        (
            "Out of memory: Kill process 1234 (postgres) score 900 or sacrifice child",
            1234,
            "postgres",
        ),
    ],
)
def test_other_kernel_oom_wordings_are_recognised(message: str, pid: int, process: str) -> None:
    kill = detect_kill(journal_event(message, monotonic_ns=1_000, fields={"_TRANSPORT": "kernel"}))

    assert kill is not None
    assert kill.source == "kernel"
    assert (kill.pid, kill.process) == (pid, process)


def test_systemd_oomd_kill_line_gives_the_victim_unit_from_its_own_message() -> None:
    kill = detect_kill(oomd_kill_record())

    assert kill is not None
    assert kill.source == "systemd-oomd"
    assert kill.cgroup == "/user.slice/user-1000.slice/session-2.scope"
    assert kill.unit == "session-2.scope"
    assert kill.unit_field == "payload.message"
    # oomd 的 Killed 行不写 PID 与进程名：不猜
    assert kill.pid is None
    assert kill.process is None


def test_systemd_oomd_bookkeeping_lines_are_not_kills() -> None:
    record = journal_event(
        "Considered 3 cgroups and found 1 with pressure above threshold.",
        monotonic_ns=1_000,
        fields={"SYSLOG_IDENTIFIER": "systemd-oomd"},
    )

    assert detect_kill(record) is None


def test_unrelated_error_lines_are_not_kills() -> None:
    record = journal_event(
        "sshd.service: Failed with result 'exit-code'.",
        monotonic_ns=1_000,
        fields={"_SYSTEMD_UNIT": "sshd.service"},
    )

    assert detect_kill(record) is None


def test_kernel_oom_text_from_a_non_kernel_source_is_not_an_oom_fact() -> None:
    """转发 dmesg 原文的服务不是受害者：不能算内核 OOM，也不能拿它自己的 Unit 做归属。"""
    quoted = journal_event(
        "Out of memory: Killed process 4310 (firefox) total-vm:2846920kB "
        "(quoted from dmesg for the report)",
        monotonic_ns=1_000_000_000,
        fields={"_TRANSPORT": "journal", "_SYSTEMD_UNIT": "sysreport.service"},
        entity="service:sysreport.service",
    )

    assert detect_kill(quoted) is None
    assert plan_annotations([quoted]) == []


def test_kernel_oom_is_recognised_from_either_kernel_source_field() -> None:
    """认来源的两个字段：`_TRANSPORT=kernel` 或 `SYSLOG_IDENTIFIER=kernel`，命中一个就够。"""
    by_transport = journal_event(KERNEL_OOM, monotonic_ns=1_000, fields={"_TRANSPORT": "kernel"})
    by_identifier = journal_event(
        KERNEL_OOM, monotonic_ns=1_000, fields={"SYSLOG_IDENTIFIER": "kernel"}
    )
    from_stdout = journal_event(
        KERNEL_OOM,
        monotonic_ns=1_000,
        fields={"_TRANSPORT": "stdout", "SYSLOG_IDENTIFIER": "report"},
    )

    assert detect_kill(by_transport) is not None
    assert detect_kill(by_identifier) is not None
    assert detect_kill(from_stdout) is None


@pytest.mark.parametrize(
    "attributes",
    [
        {"fields": "not-a-mapping"},
        "not-a-mapping",
    ],
)
def test_malformed_record_is_skipped_and_the_rest_still_produce_conclusions(
    attributes: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """一条结构不对的记录不能让同批其它记录的分析结果一起丢掉，跳过它并记下原因。"""
    normal = journal_event(KERNEL_OOM, monotonic_ns=2_000_000_000, fields={"_TRANSPORT": "kernel"})
    bad_body: dict[str, Any] = {
        "kind": RecordKind.EVENT.value,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": f"journal-import:{MACHINE_ID}:{BOOT_ID}",
        "source_plugin": "manual.fake",
        "stream_id": "journal.entries",
        "monotonic_ns": 1_000_000_000,
        "realtime_ns": 1_794_000_000_000_000_000,
        "clock_domain": "boot_monotonic",
        "entity": None,
        "payload": {
            "event_type": "journal.entry",
            "severity": "error",
            "message": KERNEL_OOM,
            "attributes": attributes,
        },
    }
    bad = Record.model_validate({"record_id": compute_record_id(bad_body), **bad_body})

    access = FakeAnalysisData([bad, normal])
    with caplog.at_level("WARNING"):
        result = run_analyzer([bad, normal], data=access)

    assert result.annotations_written == 1
    assert [item["payload"]["evidence_refs"][0] for item in access.written] == [normal.record_id]
    assert bad.record_id in caplog.text
    assert "不是对象" in caplog.text


# ---------------------------------------------------------------------------
# 结论：一条 OOM 一条 annotation，能指回原文
# ---------------------------------------------------------------------------


def test_two_sources_each_produce_one_annotation_pointing_at_their_own_record() -> None:
    records = two_source_records()
    annotations = plan_annotations(records)

    assert len(annotations) == 2
    by_type = {item["payload"]["annotation_type"]: item for item in annotations}
    assert set(by_type) == {ANNOTATION_TYPE_KERNEL, ANNOTATION_TYPE_SYSTEMD_OOMD}

    kernel = by_type[ANNOTATION_TYPE_KERNEL]
    kernel_source = next(item for item in records if item.payload["message"] == KERNEL_OOM)
    assert kernel["payload"]["evidence_refs"][0] == kernel_source.record_id
    assert "Killed process 4310 (firefox)" in kernel["payload"]["message"]
    assert kernel["payload"]["severity"] == Severity.ERROR.value

    oomd = by_type[ANNOTATION_TYPE_SYSTEMD_OOMD]
    oomd_source = next(item for item in records if item.payload["message"] == OOMD_KILL)
    assert oomd["payload"]["evidence_refs"][0] == oomd_source.record_id
    assert "payload.attributes.identifier=systemd-oomd" in oomd["payload"]["message"]


def test_annotation_records_are_valid_data_model_records() -> None:
    """写进库的每条都是合法的 annotation 记录：``record_id`` 按内容算，作用域字段与证据一致。"""
    annotations = plan_annotations(two_source_records())

    assert annotations
    for item in annotations:
        body = {key: value for key, value in item.items() if key != "record_id"}
        assert item["record_id"] == compute_record_id(body)
        assert item["kind"] == RecordKind.ANNOTATION.value
        assert (item["machine_id"], item["boot_id"]) == (MACHINE_ID, BOOT_ID)
        assert item["source_plugin"] == "oom.analyzer"
        assert item["payload"]["analyzer"] == "oom.analyzer"
        assert item["stream_id"] == "analysis.oom"


def test_kernel_oom_annotation_follows_the_lifetime_to_the_service_entity() -> None:
    """Unit 的 ``service`` 实体在库里时，结论挂在它上面；关联依据写在 ``message`` 里。"""
    annotations = plan_annotations(two_source_records())
    kernel = next(
        item for item in annotations if item["payload"]["annotation_type"] == ANNOTATION_TYPE_KERNEL
    )

    assert kernel["entity"] == "service:firefox.service"
    # 内核那一行没有 Unit 字段，Unit 来自时间窗口内的失败日志：结论是疑似
    assert kernel["payload"]["confidence"] == CONFIDENCE_PROXIMITY
    assert "payload.attributes.unit" in kernel["payload"]["message"]
    assert "只凭时间接近" in kernel["payload"]["message"]


def test_oomd_unit_comes_from_its_own_line_so_the_conclusion_is_direct() -> None:
    """systemd-oomd 那一行自己写着被杀的 cgroup：Unit 是直接证据，不是疑似。"""
    annotations = plan_annotations(two_source_records())
    oomd = next(
        item
        for item in annotations
        if item["payload"]["annotation_type"] == ANNOTATION_TYPE_SYSTEMD_OOMD
    )

    assert oomd["payload"]["confidence"] == CONFIDENCE_UNIT_FROM_SAME_LINE
    assert (
        'payload.message 含 "Killed /user.slice/user-1000.slice/session-2.scope"'
        in (oomd["payload"]["message"])
    )
    assert "Unit session-2.scope 由payload.message给出" in oomd["payload"]["message"]
    assert "只凭时间接近" not in oomd["payload"]["message"]
    # 这个 Unit 在日志里没有任何记录引用它，所以没有数据库实体可挂：如实说明，不硬造实体
    assert oomd["entity"] is None
    assert "库里没有 service:session-2.scope 实体" in oomd["payload"]["message"]


# ---------------------------------------------------------------------------
# 直接证据与疑似关联的区别
# ---------------------------------------------------------------------------


def test_unit_only_by_time_proximity_is_marked_suspected_not_causal() -> None:
    kill = kernel_oom_record()
    failure = unit_failed_record(kill.monotonic_ns, 30_000_000)
    annotations = plan_annotations([unit_started_record(), kill, failure])

    assert len(annotations) == 1
    annotation = annotations[0]
    payload = annotation["payload"]

    # 疑似：置信度低，且写明「只凭时间接近」，不写成因果
    assert payload["confidence"] == CONFIDENCE_PROXIMITY
    assert payload["confidence"] < 0.5
    assert "疑似" in payload["message"]
    assert "只凭时间接近" in payload["message"]
    assert "不能当作因果结论" in payload["message"]
    assert "30000000 纳秒" in payload["message"]

    # 两条日志都是证据：OOM 那一行与被推断出的 Unit 失败那一行
    assert payload["evidence_refs"] == [kill.record_id, failure.record_id]
    # 仍然关联到该 Unit 的 service 实体
    assert annotation["entity"] == "service:firefox.service"


def test_unit_outside_the_window_is_not_associated_at_all() -> None:
    kill = kernel_oom_record()
    failure = unit_failed_record(kill.monotonic_ns, 10_000_000_000)
    annotations = plan_annotations([unit_started_record(), kill, failure])

    assert len(annotations) == 1
    payload = annotations[0]["payload"]

    assert payload["confidence"] == CONFIDENCE_KILL_ONLY
    assert payload["evidence_refs"] == [kill.record_id]
    assert "不给出 Unit 归属" in payload["message"]
    assert "疑似" not in payload["message"]
    assert annotations[0]["entity"] is None


def test_unit_failure_outside_oom_kill_result_is_not_used_as_evidence() -> None:
    """只有 ``Result=oom-kill`` 才当作 OOM 的线索；普通退出码失败不参与关联。"""
    kill = kernel_oom_record()
    other = journal_event(
        "firefox.service: Failed with result 'exit-code'.",
        monotonic_ns=kill.monotonic_ns + 10_000_000,
        fields={"_SYSTEMD_UNIT": "firefox.service"},
        entity="service:firefox.service",
    )
    annotations = plan_annotations([unit_started_record(), kill, other])

    assert annotations[0]["payload"]["confidence"] == CONFIDENCE_KILL_ONLY
    assert annotations[0]["entity"] is None


def test_proximity_window_is_configurable() -> None:
    kill = kernel_oom_record()
    failure = unit_failed_record(kill.monotonic_ns, 30_000_000)

    wide = plan_annotations([kill, failure], window_ns=60_000_000)
    narrow = plan_annotations([kill, failure], window_ns=1_000_000)

    assert wide[0]["payload"]["confidence"] == CONFIDENCE_PROXIMITY
    assert narrow[0]["payload"]["confidence"] == CONFIDENCE_KILL_ONLY


# ---------------------------------------------------------------------------
# 可重复：同一批输入重复分析不叠加出互相矛盾的结论
# ---------------------------------------------------------------------------


def test_same_input_gives_the_same_records_on_every_run() -> None:
    records = two_source_records()

    first = plan_annotations(records)
    second = plan_annotations(records)

    assert first == second
    assert [item["record_id"] for item in first] == [item["record_id"] for item in second]
    assert len({item["record_id"] for item in first}) == len(first)


# ---------------------------------------------------------------------------
# 插件入口与契约
# ---------------------------------------------------------------------------


def test_analyze_is_synchronous_and_matches_the_analyzer_protocol() -> None:
    plugin = OomAnalyzer()

    assert isinstance(plugin, AnalyzerPlugin)
    assert plugin.spec.kind is PluginKind.ANALYZER
    assert plugin.spec.api_version == PLUGIN_API_VERSION
    assert not inspect.iscoroutinefunction(plugin.analyze)
    assert list(inspect.signature(plugin.analyze).parameters) == ["request", "context"]


def test_registry_invoke_returns_a_real_result_and_writes_both_annotations() -> None:
    access = FakeAnalysisData(two_source_records())

    result = run_analyzer(two_source_records(), data=access)

    assert isinstance(result, AnalysisResult)
    assert result.annotations_written == 2
    assert len(access.written) == 2
    assert access.queried_kinds == (RecordKind.EVENT.value,)
    assert {item["payload"]["annotation_type"] for item in access.written} == {
        ANNOTATION_TYPE_KERNEL,
        ANNOTATION_TYPE_SYSTEMD_OOMD,
    }


def test_analyzer_rejects_a_data_entry_that_is_not_synchronous() -> None:
    with pytest.raises(PluginInputError) as failure:
        run_analyzer([], data=AsyncAnalysisData())

    assert "同步" in str(failure.value)


def test_analyzer_rejects_a_missing_data_entry_with_a_readable_reason() -> None:
    with pytest.raises(PluginInputError) as failure:
        run_analyzer([], data=object())

    assert "query_records" in str(failure.value)


def test_unexpected_error_is_wrapped_as_plugin_execution_error() -> None:
    access = FakeAnalysisData(two_source_records(), fail_with=RuntimeError("数据库掉线了"))

    with pytest.raises(PluginExecutionError) as failure:
        run_analyzer(two_source_records(), data=access)

    assert failure.value.plugin_id == OomAnalyzer.spec.id
    assert isinstance(failure.value.cause, RuntimeError)


def test_boot_id_limits_the_analysis() -> None:
    foreign = journal_event(
        KERNEL_OOM,
        monotonic_ns=500,
        fields={"_TRANSPORT": "kernel"},
        boot_id=OTHER_BOOT_ID,
    )
    access = FakeAnalysisData([kernel_oom_record(), foreign])

    result = run_analyzer([], request=AnalysisRequest(boot_id=BOOT_ID), data=access)

    assert result.annotations_written == 1
    assert access.written[0]["boot_id"] == BOOT_ID


def test_record_ids_limit_which_records_count_as_evidence() -> None:
    records = two_source_records()
    oomd = next(item for item in records if item.payload["message"] == OOMD_KILL)

    annotations = plan_annotations(records, record_ids=(oomd.record_id,))

    assert [item["payload"]["annotation_type"] for item in annotations] == [
        ANNOTATION_TYPE_SYSTEMD_OOMD
    ]


def test_empty_input_writes_nothing() -> None:
    access = FakeAnalysisData([])

    result = run_analyzer([], data=access)

    assert result.annotations_written == 0
    assert access.written == []


def test_config_window_is_read_from_the_plugin_context() -> None:
    kill = kernel_oom_record()
    failure = unit_failed_record(kill.monotonic_ns, 30_000_000)
    access = FakeAnalysisData([kill, failure])

    wide = run_analyzer([], config={CONFIG_PROXIMITY_WINDOW_NS: 60_000_000}, data=access)
    narrow = run_analyzer([], config={CONFIG_PROXIMITY_WINDOW_NS: 1}, data=access)

    assert wide.annotations_written == narrow.annotations_written == 1
    assert access.written[0]["payload"]["confidence"] == CONFIDENCE_PROXIMITY
    assert access.written[1]["payload"]["confidence"] == CONFIDENCE_KILL_ONLY


def test_default_window_is_five_seconds() -> None:
    assert DEFAULT_PROXIMITY_WINDOW_NS == 5_000_000_000
    assert OomAnalyzer.spec.config_schema["properties"][CONFIG_PROXIMITY_WINDOW_NS]["type"] == (
        "integer"
    )


def test_oom_analyzer_is_registered_alongside_existing_builtins() -> None:
    registry = register_builtins(PluginRegistry())

    analyzer_ids = [plugin.spec.id for plugin in registry.by_kind(PluginKind.ANALYZER)]
    # 只断言「我在清单里、类别对」，不用全等比较其它任务往同一清单追加的插件
    assert OomAnalyzer.spec.id in analyzer_ids
    assert registry.spec(OomAnalyzer.spec.id).kind is PluginKind.ANALYZER
    # 各任务轮流往同一份清单追加：既有项一个都不能少
    ids = [plugin.spec.id for plugin in registry.all()]
    assert ids[:2] == ["bootrec.importer", "journal.archive"]
    # 只要求追加在既有项之后，不要求自己排在最后：2.1 也会往同一份清单追加
    assert ids.index(OomAnalyzer.spec.id) > ids.index("journal.archive")
    assert isinstance(OomAnalyzer.spec, PluginSpec)
