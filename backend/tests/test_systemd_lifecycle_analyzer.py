"""Systemd 生命周期分析插件的接口级测试。

覆盖任务 2.1 的接口要求：``analyze`` 同步、签名与 ``docs/plugin-contracts.md`` 第 3.4 节一致、
经 ``PluginRegistry.invoke`` 拿到真实 ``AnalysisResult`` 且结论写进数据入口、非 ``PluginError``
异常被包成 ``PluginExecutionError``、数据入口形状不对或不是同步入口时给出可读的
``PluginInputError``。真实 PostgreSQL 上的写入见
``tests/integration/test_systemd_lifecycle_analysis.py``。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from system_analyzer.analyzers.systemd_lifecycle import (
    ANNOTATION_ABNORMAL_EXIT,
    ANNOTATION_LIFECYCLE_INCOMPLETE,
    ANNOTATION_UNATTRIBUTED,
    PLUGIN_ID,
    SPAN_ACTIVATING,
    SPAN_ACTIVE,
)
from system_analyzer.analyzers.systemd_lifecycle_analyzer import (
    MACHINE_ID_OPTION,
    LifecycleDataAccess,
    SystemdLifecycleAnalyzer,
)
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.importers import journal_archive as archive
from system_analyzer.plugins.builtins import BUILTIN_PLUGINS, register_builtins
from system_analyzer.plugins.contracts import (
    AnalysisRequest,
    AnalysisResult,
    AnalyzerPlugin,
    PluginContext,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry, reset_registry
from test_systemd_lifecycle import (
    BOOT_ID,
    COLLECTION_ID,
    FIXTURE,
    MACHINE_ID,
    fixture_records,
    records_without_structured_unit_fields,
)


class RecordingAccess:
    """形状与 ``LifecycleDataAccess`` 一致的替身：只记录读写，不连数据库。"""

    def __init__(self, records: Any = ()) -> None:
        self.records = list(records)
        self.list_calls: list[dict[str, Any]] = []
        self.append_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.entity_calls: list[tuple[str, list[Any]]] = []

    def list_records_in_boot(
        self,
        machine_id: str,
        boot_id: str,
        *,
        kinds: Any = None,
        limit: Any = None,
    ) -> list[Any]:
        self.list_calls.append(
            {"machine_id": machine_id, "boot_id": boot_id, "kinds": kinds, "limit": limit}
        )
        return list(self.records)

    def ensure_entities(self, collection_id: str, entities: Any) -> int:
        batch = list(entities)
        self.entity_calls.append((collection_id, batch))
        return len(batch)

    def append_records(self, collection_id: str, records: Any) -> int:
        batch = list(records)
        self.append_calls.append((collection_id, batch))
        return len(batch)

    def appended(self, kind: str) -> list[dict[str, Any]]:
        return [
            record for _, batch in self.append_calls for record in batch if record["kind"] == kind
        ]


def request(**options: Any) -> AnalysisRequest:
    options.setdefault(MACHINE_ID_OPTION, MACHINE_ID)
    return AnalysisRequest(boot_id=BOOT_ID, options=options)


def context(data: Any) -> PluginContext:
    return PluginContext(plugin_id=PLUGIN_ID, data=data)


def invoke(plugin: SystemdLifecycleAnalyzer, data: Any, **options: Any) -> AnalysisResult:
    registry = PluginRegistry()
    registry.register(plugin)
    return registry.invoke(
        PLUGIN_ID,
        lambda impl, ctx: impl.analyze(request(**options), ctx),
        data=data,
    )


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


# --- manifest 与注册 ---------------------------------------------------------


def test_manifest_declares_an_analyzer() -> None:
    plugin = SystemdLifecycleAnalyzer()

    assert plugin.spec.id == PLUGIN_ID
    assert plugin.spec.kind is PluginKind.ANALYZER
    assert plugin.spec.api_version == "1.0"
    assert plugin.spec.capabilities == ("systemd.lifecycle",)
    assert isinstance(plugin, AnalyzerPlugin)


def test_analyze_is_synchronous_and_matches_the_contract() -> None:
    signature = inspect.signature(SystemdLifecycleAnalyzer.analyze)

    assert not inspect.iscoroutinefunction(SystemdLifecycleAnalyzer.analyze)
    assert list(signature.parameters) == ["self", "request", "context"]


def test_plugin_is_registered_in_the_single_builtin_list() -> None:
    ids = [plugin.spec.id for plugin in BUILTIN_PLUGINS]

    # 各任务轮流往同一份清单追加：既有项一个都不能少，本插件追加在它们之后。
    # 不要求自己排在最后：同 stage 的 2.2 也往同一份清单追加。
    assert ids[:2] == ["bootrec.importer", "journal.archive"]
    assert PLUGIN_ID in ids
    assert ids.index(PLUGIN_ID) > ids.index("journal.archive")
    registry = register_builtins()
    spec = registry.spec(PLUGIN_ID)
    assert spec is not None
    assert spec.kind is PluginKind.ANALYZER


# --- 经注册表调用：拿到真实结果，结论写进数据入口 -----------------------------


def test_registry_invoke_returns_a_real_result_and_writes_the_conclusions() -> None:
    access = RecordingAccess(fixture_records())

    result = invoke(SystemdLifecycleAnalyzer(), access)

    assert isinstance(result, AnalysisResult)
    assert result.annotations_written == 7
    assert access.list_calls == [
        {
            "machine_id": MACHINE_ID,
            "boot_id": BOOT_ID,
            "kinds": (RecordKind.EVENT,),
            "limit": None,
        }
    ]

    spans = access.appended("span")
    annotations = access.appended("annotation")
    assert len(spans) == 5
    assert len(annotations) == 7
    assert {collection_id for collection_id, _ in access.append_calls} == {COLLECTION_ID}

    # 区间在前、结论在后；两类记录都带证据与来源
    kinds = [record["kind"] for _, batch in access.append_calls for record in batch]
    assert kinds.index("annotation") > len(spans) - 1
    assert {record["source_plugin"] for _, batch in access.append_calls for record in batch} == {
        PLUGIN_ID
    }
    assert {record["payload"]["span_type"] for record in spans} == {
        SPAN_ACTIVATING,
        SPAN_ACTIVE,
    }
    assert all(record["payload"]["evidence_refs"] for record in annotations)

    # 记录引用的 service 实体先补齐：真实 Journal 的导入只为 _SYSTEMD_UNIT 建过实体
    assert access.entity_calls
    entities = [entity for _, batch in access.entity_calls for entity in batch]
    assert {entity.entity_id for entity in entities} == {
        "service:nginx.service",
        "service:postgresql.service",
        "service:redis.service",
        "service:sshd.service",
    }
    assert {entity.name for entity in entities} == {
        "nginx.service",
        "postgresql.service",
        "redis.service",
        "sshd.service",
    }
    assert {entity.boot_id for entity in entities} == {BOOT_ID}


def test_analyze_does_not_touch_the_database_or_open_an_event_loop() -> None:
    """插件里的存储层调用都经过数据入口；不满足形状时直接拒绝，而不是自己开事件循环。"""
    assert isinstance(RecordingAccess(), LifecycleDataAccess)
    with pytest.raises(PluginInputError, match="list_records_in_boot"):
        invoke(SystemdLifecycleAnalyzer(), object())


# --- 失败路径 ----------------------------------------------------------------


def test_missing_machine_scope_is_refused_with_a_readable_reason() -> None:
    access = RecordingAccess(fixture_records())
    request_without_machine = AnalysisRequest(boot_id=BOOT_ID)

    with pytest.raises(PluginInputError, match="machine_id"):
        SystemdLifecycleAnalyzer().analyze(request_without_machine, context(access))
    assert access.list_calls == []


def test_empty_boot_id_is_refused() -> None:
    access = RecordingAccess(fixture_records())

    with pytest.raises(PluginInputError, match="boot_id"):
        SystemdLifecycleAnalyzer().analyze(
            AnalysisRequest(boot_id="  ", options={MACHINE_ID_OPTION: MACHINE_ID}),
            context(access),
        )
    assert access.list_calls == []


def test_async_data_entry_is_refused_instead_of_silently_doing_nothing() -> None:
    class AsyncAccess:
        async def list_records_in_boot(
            self, machine_id: str, boot_id: str, *, kinds: Any = None, limit: Any = None
        ) -> list[Any]:
            return []

        def ensure_entities(self, collection_id: str, entities: Any) -> int:
            return len(list(entities))

        def append_records(self, collection_id: str, records: Any) -> int:
            return 0

    with pytest.raises(PluginInputError, match="协程"):
        invoke(SystemdLifecycleAnalyzer(), AsyncAccess())


def test_data_entry_without_the_entity_step_is_refused() -> None:
    """记录要引用实体；数据入口没有补齐实体的能力时直接说明，而不是写到一半才发现。"""

    class NoEntityAccess:
        def list_records_in_boot(self, machine_id: str, boot_id: str, **_: Any) -> list[Any]:
            return list(fixture_records())

        def append_records(self, collection_id: str, records: Any) -> int:
            return len(list(records))

    with pytest.raises(PluginInputError, match="ensure_entities"):
        invoke(SystemdLifecycleAnalyzer(), NoEntityAccess())


def test_append_must_report_the_written_count() -> None:
    class LyingAccess(RecordingAccess):
        def append_records(self, collection_id: str, records: Any) -> int:
            return None  # type: ignore[return-value]

    with pytest.raises(PluginInputError, match="写入条数"):
        invoke(SystemdLifecycleAnalyzer(), LyingAccess(fixture_records()))


def test_unexpected_exception_is_wrapped_as_plugin_execution_error() -> None:
    class ExplodingAccess(RecordingAccess):
        def list_records_in_boot(
            self, machine_id: str, boot_id: str, *, kinds: Any = None, limit: Any = None
        ) -> list[Any]:
            raise RuntimeError("数据库连接断了")

    registry = PluginRegistry()
    registry.register(SystemdLifecycleAnalyzer())

    with pytest.raises(PluginExecutionError) as caught:
        registry.invoke(
            PLUGIN_ID,
            lambda impl, ctx: impl.analyze(request(), ctx),
            data=ExplodingAccess(),
        )

    assert caught.value.plugin_id == PLUGIN_ID
    assert isinstance(caught.value.__cause__, RuntimeError)
    # 崩溃不影响注册表，也不影响随后正常的一次调用
    assert len(registry) == 1
    result = registry.invoke(
        PLUGIN_ID,
        lambda impl, ctx: impl.analyze(request(), ctx),
        data=RecordingAccess(fixture_records()),
    )
    assert result.annotations_written == 7


# --- 作用范围 ----------------------------------------------------------------


def test_no_journal_evidence_is_an_empty_result_not_a_failure() -> None:
    access = RecordingAccess()

    result = invoke(SystemdLifecycleAnalyzer(), access)

    assert result == AnalysisResult(annotations_written=0)
    assert access.append_calls == []


def test_record_ids_narrow_the_analysis_to_the_given_records() -> None:
    records = fixture_records()
    sshd_exit = next(
        record
        for record in records
        if record["payload"]["attributes"].get("unit") == "sshd.service"
    )
    access = RecordingAccess(records)
    registry = PluginRegistry()
    registry.register(SystemdLifecycleAnalyzer())

    result = registry.invoke(
        PLUGIN_ID,
        lambda impl, ctx: impl.analyze(
            AnalysisRequest(
                boot_id=BOOT_ID,
                record_ids=(sshd_exit["record_id"],),
                options={MACHINE_ID_OPTION: MACHINE_ID},
            ),
            ctx,
        ),
        data=access,
    )

    # 只分析这一条记录：sshd 没有 Starting / Started，因此没有区间，只剩两条结论
    assert result.annotations_written == 2
    assert access.appended("span") == []
    assert {record["entity"] for record in access.appended("annotation")} == {
        "service:sshd.service"
    }
    assert {record["payload"]["annotation_type"] for record in access.appended("annotation")} == {
        ANNOTATION_ABNORMAL_EXIT,
        ANNOTATION_LIFECYCLE_INCOMPLETE,
    }


def test_plugin_does_not_keep_state_between_calls() -> None:
    plugin = SystemdLifecycleAnalyzer()
    first = invoke(plugin, RecordingAccess(fixture_records()))
    second = invoke(plugin, RecordingAccess(fixture_records()))

    assert first == second


def test_fixture_is_the_fixed_journal_export_used_by_the_tests() -> None:
    assert FIXTURE.is_file()
    parsed = archive.parse_journal_archive(FIXTURE)
    assert len(parsed.entries) == 13


# --- 归属不到 Unit 的消息：报数量与原因，不记到 init.scope 头上 -----------------


def test_unattributable_messages_write_a_summary_instead_of_fake_spans() -> None:
    """旧版 / 第三方转存的 Journal 没有 UNIT=：一条区间都不产出，只报数量与原因。"""
    records = records_without_structured_unit_fields()
    access = RecordingAccess(records)

    result = invoke(SystemdLifecycleAnalyzer(), access)

    assert access.appended("span") == []
    assert access.entity_calls == []  # 没有 Unit 要补实体
    annotations = access.appended("annotation")
    assert [record["payload"]["annotation_type"] for record in annotations] == [
        ANNOTATION_UNATTRIBUTED
    ]
    assert result.annotations_written == 1
    summary = annotations[0]
    assert summary["entity"] is None
    assert f"{len(records)} 条生命周期消息缺少结构化 Unit 字段" in summary["payload"]["message"]
    assert "未产出结论" in summary["payload"]["message"]
