"""存储层规则的测试：导入状态迁移与写入前校验。

这些规则决定了「什么数据能落库」与「缺失怎么表达」，全部是纯函数，不需要数据库。
真实样例（``docs/samples/bootrec-v1``）也要能通过这些校验，所以这里既测拒绝也测接受。
"""

from typing import Any

import pytest

from sample_bootrec import load_sample
from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    CollectionMode,
    CollectionRef,
    EventPayload,
    GenericEntity,
    MachineEntity,
    MetricStreamEntity,
    Record,
    RecordKind,
    compute_record_id,
)
from system_analyzer.storage.models import ImportStatus, can_transition
from system_analyzer.storage.validation import (
    bundle_problems,
    entity_problems,
    identity_problems,
    incoming_record,
    incoming_records,
    record_problems,
    resources_problems,
    stored_content_problems,
)

MACHINE_ID = "machine:test-pc"
BOOT_ID = "11111111-2222-3333-4444-555555555555"
COLLECTION_ID = "collection:test-0001"


def _collection(**overrides: Any) -> CollectionRef:
    values: dict[str, Any] = {
        "collection_id": COLLECTION_ID,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "mode": CollectionMode.SSH_COLLECTION,
        "created_realtime_ns": 1_794_009_600_000_000_000,
    }
    values.update(overrides)
    return CollectionRef(**values)


def _entities() -> list[Any]:
    return [
        MachineEntity(entity_type="machine", machine_id=MACHINE_ID, hostname="test-pc"),
        BootEntity(
            entity_type="boot",
            boot_id=BOOT_ID,
            machine_id=MACHINE_ID,
            started_realtime_ns=1_794_009_600_000_000_000,
        ),
        GenericEntity(
            entity_type="service",
            entity_id="service:nginx.service",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            name="nginx.service",
        ),
        MetricStreamEntity(
            entity_type="metric_stream",
            entity_id="metric:cpu.total.utilization",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            name="cpu.total.utilization",
            unit="percent",
            available=True,
        ),
    ]


def _raw_record(**overrides: Any) -> dict[str, Any]:
    """按规范组装一条原始记录字典：``record_id`` 由这个字典自己算出，与生产方一致。"""
    values: dict[str, Any] = {
        "kind": RecordKind.EVENT,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "systemd",
        "stream_id": "systemd.units",
        "monotonic_ns": 1_200_000_000,
        "realtime_ns": 1_794_009_601_200_000_000,
        "clock_domain": ClockDomain.BOOT_MONOTONIC,
        "entity": "service:nginx.service",
        "payload": {"event_type": "systemd.unit.started"},
    }
    values.update(overrides)
    values["record_id"] = compute_record_id(
        {key: value for key, value in values.items() if key != "record_id"}
    )
    return values


def _record(**overrides: Any) -> Record:
    """同 ``_raw_record``，但返回模型，用于不需要核对摘要的校验。"""
    return Record.model_validate(_raw_record(**overrides))


def _metric_record(**overrides: Any) -> Record:
    values: dict[str, Any] = {
        "kind": RecordKind.METRIC,
        "entity": "metric:cpu.total.utilization",
        "source_plugin": "resource-sampler",
        "stream_id": "cpu",
        "payload": {"name": "cpu.total.utilization", "value": 9.5, "unit": "percent"},
    }
    values.update(overrides)
    return _record(**values)


# ---------------------------------------------------------------------------
# 导入状态
# ---------------------------------------------------------------------------

ALLOWED = [
    (ImportStatus.PENDING, ImportStatus.IMPORTING),
    (ImportStatus.PENDING, ImportStatus.FAILED),
    (ImportStatus.IMPORTING, ImportStatus.IMPORTED),
    (ImportStatus.IMPORTING, ImportStatus.FAILED),
    (ImportStatus.FAILED, ImportStatus.IMPORTING),
    (ImportStatus.IMPORTED, ImportStatus.IMPORTING),
]

FORBIDDEN = [
    (ImportStatus.PENDING, ImportStatus.IMPORTED),
    (ImportStatus.IMPORTED, ImportStatus.FAILED),
    (ImportStatus.FAILED, ImportStatus.IMPORTED),
    (ImportStatus.IMPORTING, ImportStatus.PENDING),
]


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_allowed_transitions(current: ImportStatus, target: ImportStatus) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(("current", "target"), FORBIDDEN)
def test_forbidden_transitions(current: ImportStatus, target: ImportStatus) -> None:
    assert not can_transition(current, target)


@pytest.mark.parametrize("status", list(ImportStatus))
def test_transition_to_itself_is_a_no_op(status: ImportStatus) -> None:
    assert can_transition(status, status)


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


def test_valid_entities_have_no_problems() -> None:
    assert entity_problems(_collection(), _entities()) == []


def test_missing_machine_row_is_reported() -> None:
    problems = entity_problems(_collection(), _entities()[1:])
    assert any("machine" in problem for problem in problems)


def test_missing_boot_row_is_reported() -> None:
    problems = entity_problems(_collection(), [*_entities()[:1], *_entities()[2:]])
    assert any("boot" in problem for problem in problems)


def test_unavailable_stream_requires_reason() -> None:
    entities = [
        entity
        if not isinstance(entity, MetricStreamEntity)
        else entity.model_copy(update={"available": False})
        for entity in _entities()
    ]
    problems = entity_problems(_collection(), entities)
    assert any("必须写明不可用原因" in problem for problem in problems)


def test_available_stream_must_not_carry_reason() -> None:
    entities = [
        entity
        if not isinstance(entity, MetricStreamEntity)
        else entity.model_copy(update={"unavailable_reason": "不该写"})
        for entity in _entities()
    ]
    problems = entity_problems(_collection(), entities)
    assert any("却写了不可用原因" in problem for problem in problems)


def test_entity_from_another_boot_is_reported() -> None:
    entities = _entities()
    entities[2] = entities[2].model_copy(update={"boot_id": "22222222-0000-0000-0000-000000000000"})
    problems = entity_problems(_collection(), entities)
    assert any("boot_id 与采集不一致" in problem for problem in problems)


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


def test_valid_records_have_no_problems() -> None:
    entities = _entities()
    assert record_problems(_collection(), entities, [_record(), _metric_record()]) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collection_id", "collection:other"),
        ("machine_id", "machine:other"),
        ("boot_id", "33333333-0000-0000-0000-000000000000"),
    ],
)
def test_record_scope_must_match_collection(field: str, value: str) -> None:
    problems = record_problems(_collection(), _entities(), [_record(**{field: value})])
    assert any(field in problem for problem in problems)


def test_record_needs_at_least_one_timestamp() -> None:
    record = _record(monotonic_ns=None, realtime_ns=None, clock_domain=ClockDomain.WALL_REALTIME)
    problems = record_problems(_collection(), _entities(), [record])
    assert any("都为空" in problem for problem in problems)


def test_clock_domain_must_follow_monotonic_ns() -> None:
    record = _record(clock_domain=ClockDomain.WALL_REALTIME)
    problems = record_problems(_collection(), _entities(), [record])
    assert any("clock_domain" in problem for problem in problems)


def test_tampered_record_content_is_rejected() -> None:
    """内容改了但 record_id 没重算：按原始字典重算摘要就能发现。"""
    raw = _raw_record()
    tampered = {**raw, "payload": {"event_type": "systemd.unit.failed"}}
    problems = identity_problems(incoming_record(tampered))
    assert any("与内容算出的" in problem for problem in problems)


def test_record_id_must_be_32_hex() -> None:
    raw = {**_raw_record(), "record_id": "not-a-digest"}
    problems = identity_problems(incoming_record(raw))
    assert any("32 位小写十六进制" in problem for problem in problems)


def test_record_without_optional_fields_is_accepted() -> None:
    """0.2 校验器按原始字典算摘要：没写 entity、没写 realtime_ns 的记录也必须能导入。

    这正是按「补齐默认值后的模型」重算会误判的写法（可选字段缺省时模型会补上默认值）。
    """
    raw = _raw_record()
    del raw["entity"]
    del raw["realtime_ns"]
    raw["record_id"] = compute_record_id(
        {key: value for key, value in raw.items() if key != "record_id"}
    )

    incoming = [incoming_record(raw)]
    assert identity_problems(incoming[0]) == []
    assert bundle_problems(_collection(), _entities(), incoming) == []


def test_record_with_unknown_field_is_accepted() -> None:
    """本主版本不认识的字段参与摘要计算，不能因为不认识就判成「内容被改动」。"""
    raw = _raw_record()
    raw["future_field"] = {"added": "in a later minor version"}
    raw["record_id"] = compute_record_id(
        {key: value for key, value in raw.items() if key != "record_id"}
    )
    assert identity_problems(incoming_record(raw)) == []


def test_model_input_loses_unknown_fields_and_says_so() -> None:
    """只给模型时未知字段已经丢了，摘要对不上；提示里要说明该传原始字典。"""
    raw = _raw_record()
    raw["future_field"] = {"added": "in a later minor version"}
    raw["record_id"] = compute_record_id(
        {key: value for key, value in raw.items() if key != "record_id"}
    )
    problems = identity_problems(incoming_record(Record.model_validate(raw)))
    assert any("请传原始字典" in problem for problem in problems)


def test_duplicate_record_id_in_one_batch_is_reported() -> None:
    record = _record()
    problems = record_problems(_collection(), _entities(), [record, record])
    assert any("出现两次" in problem for problem in problems)


def test_metric_record_must_point_to_metric_stream() -> None:
    record = _metric_record(entity="service:nginx.service")
    problems = record_problems(_collection(), _entities(), [record])
    assert any("不是 metric_stream" in problem for problem in problems)


def test_metric_record_must_reference_an_entity() -> None:
    record = _metric_record(entity=None)
    problems = record_problems(_collection(), _entities(), [record])
    assert any("必须关联实体" in problem for problem in problems)


def test_metric_on_unavailable_stream_is_rejected() -> None:
    """缺失的指标流不得写 0 或占位值，连一条记录都不该有。"""
    entities = [
        entity
        if not isinstance(entity, MetricStreamEntity)
        else entity.model_copy(
            update={"available": False, "unavailable_reason": "/proc/meminfo 读取失败"}
        )
        for entity in _entities()
    ]
    problems = record_problems(_collection(), entities, [_metric_record()])
    assert any("标记为不可用" in problem for problem in problems)


def test_metric_name_and_unit_must_match_stream() -> None:
    record = _metric_record(
        payload={"name": "cpu.total.utilization", "value": 1.0, "unit": "bytes"}
    )
    problems = record_problems(_collection(), _entities(), [record])
    assert any("unit 不一致" in problem for problem in problems)


def test_invalid_payload_is_reported() -> None:
    record = _record(payload={"event_type": "systemd.unit.started", "severity": "fatal"})
    problems = record_problems(_collection(), _entities(), [record])
    assert any("payload 不合法" in problem for problem in problems)


# ---------------------------------------------------------------------------
# 读出侧：库里重建的记录是否仍与 record_id 对得上
# ---------------------------------------------------------------------------


def _stored_content(raw: dict[str, Any]) -> dict[str, Any]:
    """模拟从库里重建出来的内容：11 个字段都在，缺省的可空字段是 null。"""
    return Record.model_validate(raw).model_dump(mode="json", exclude={"record_id"})


def test_stored_content_matches_the_full_form() -> None:
    raw = _raw_record()
    assert stored_content_problems(raw["record_id"], _stored_content(raw)) == []


def test_stored_content_matches_when_optional_keys_were_omitted() -> None:
    """原始文件省略了 entity 与 realtime_ns：库里重建出 null，摘要仍要能核对上。"""
    raw = _raw_record()
    del raw["entity"]
    del raw["realtime_ns"]
    raw["record_id"] = compute_record_id(
        {key: value for key, value in raw.items() if key != "record_id"}
    )

    assert stored_content_problems(raw["record_id"], _stored_content(raw)) == []


def test_stored_content_reports_a_changed_record() -> None:
    raw = _raw_record()
    stored = _stored_content(raw)
    stored["payload"] = {"event_type": "systemd.unit.failed"}
    problems = stored_content_problems(raw["record_id"], stored)
    assert any("写入之后被改过" in problem for problem in problems)


def test_stored_content_checks_id_format() -> None:
    problems = stored_content_problems("not-a-digest", _stored_content(_raw_record()))
    assert any("32 位小写十六进制" in problem for problem in problems)


# ---------------------------------------------------------------------------
# 批量转换：字典不合法要变成问题列表，而不是抛 Pydantic 的异常
# ---------------------------------------------------------------------------


def test_incoming_records_collects_field_problems() -> None:
    good = _raw_record()
    bad = {**_raw_record(), "kind": "not-a-kind"}
    bad["record_id"] = compute_record_id(
        {key: value for key, value in bad.items() if key != "record_id"}
    )

    incoming, problems = incoming_records([good, bad])

    assert [item.record.record_id for item in incoming] == [good["record_id"]]
    assert len(problems) == 1
    assert problems[0].startswith("第 2 条记录不合法：kind")


# ---------------------------------------------------------------------------
# 资源数据的整体可用性
# ---------------------------------------------------------------------------


def test_resources_unknown_produces_no_problem() -> None:
    assert resources_problems(None, None, []) == []


def test_resources_absent_requires_reason() -> None:
    problems = resources_problems(False, None, [])
    assert any("必须写明原因" in problem for problem in problems)


def test_resources_present_must_not_carry_reason() -> None:
    problems = resources_problems(True, "不该写", [])
    assert any("却写了没有资源数据的原因" in problem for problem in problems)


def test_resources_absent_with_metric_records_is_rejected() -> None:
    """整份文件声明没有资源数据，却写了 metric 记录，就是伪造数据。"""
    problems = resources_problems(False, "journal_import：没有采集资源指标", [_metric_record()])
    assert any("metric 记录" in problem for problem in problems)


def test_resources_present_with_metric_records_is_fine() -> None:
    assert resources_problems(True, None, [_metric_record()]) == []


# ---------------------------------------------------------------------------
# 真实样例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["full.bootrec", "journal-only.bootrec", "resources-missing.bootrec"]
)
def test_repository_samples_pass_validation(name: str) -> None:
    """仓库里的样例按文件里的原始字典核对摘要，逐条都要通过。"""
    sample = load_sample(name)
    incoming = [incoming_record(line) for line in sample.record_lines]
    assert bundle_problems(sample.collection, sample.entities, incoming) == []


def test_sample_event_payload_round_trips_through_its_model() -> None:
    """样例的 record_id 由内容算出，能被 EventPayload 解析；两者一起说明样例与校验器口径一致。"""
    sample = load_sample("full.bootrec")
    event = next(record for record in sample.records if record.kind is RecordKind.EVENT)
    assert EventPayload.model_validate(event.payload).event_type == "systemd.unit.started"
    assert compute_record_id(event.model_dump(mode="json", exclude={"record_id"})) == (
        event.record_id
    )
