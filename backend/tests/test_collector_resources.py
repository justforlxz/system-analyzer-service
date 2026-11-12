"""远程资源采集插件的单元测试：契约形状、采样换算、缺失语义与数据入口。"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from fake_plugins import FakeDataAccess
from system_analyzer.collectors.resources import (
    CGROUP_CPU,
    CGROUP_DISK_READ,
    CGROUP_DISK_WRITE,
    CGROUP_MEMORY,
    PLUGIN_ID,
    SYSTEM_CPU,
    SYSTEM_DISK_READ,
    SYSTEM_DISK_WRITE,
    SYSTEM_MEMORY,
    UNIT_CPU,
    UNIT_DISK_READ,
    UNIT_DISK_WRITE,
    UNIT_MEMORY,
    ResourceCollector,
    build_records,
    metric_stream_entities,
    parse_capture,
)
from system_analyzer.domain.bootrec import compute_record_id
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    CollectorPlugin,
    CollectRequest,
    PluginContext,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry

BOOT_ID = "abcdef12-3456-4789-abcd-ef0123456789"
MACHINE_ID = "machine:sas-test"
COLLECTION_ID = "collection:sas-test"
RUN_ID = "11111111-2222-4333-8444-555555555555"


class FakeSampler:
    """返回固定 JSON Lines，并记下收到的命令。"""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.commands: list[tuple[str, str, float]] = []

    def capture(self, target_id: str, command: str, *, timeout: float) -> str:
        self.commands.append((target_id, command, timeout))
        return self.stdout


class ExplodingSampler:
    def capture(self, target_id: str, command: str, *, timeout: float) -> str:
        raise RuntimeError("远程采样器坏了")


def _meta() -> dict[str, Any]:
    return {
        "type": "meta",
        "hostname": "sas-test",
        "boot_id": BOOT_ID,
        "cpu_count": 16,
        "kernel_version": "6.6.0-test",
    }


def _sample(
    *,
    monotonic_ns: int,
    realtime_ns: int,
    previous_cpu: dict[str, int],
    current_cpu: dict[str, int],
    previous_disk: dict[str, int],
    current_disk: dict[str, int],
    memory_used: int | None,
    previous_values: dict[str, int] | None,
    current_values: dict[str, int] | None,
    memory_current: int | None,
) -> dict[str, Any]:
    return {
        "type": "sample",
        "monotonic_ns": monotonic_ns,
        "realtime_ns": realtime_ns,
        "elapsed_ns": 1_000_000_000,
        "system": {
            "cpu": current_cpu,
            "memory": None if memory_used is None else {"used_bytes": memory_used},
            "disk": current_disk,
        },
        "units": {} if current_values is None else {"/system.slice/sshd.service": current_values},
        "cgroups": {}
        if current_values is None
        else {
            "/system.slice/sshd.service": current_values,
            "/user.slice": {"memory_current": memory_current},
        },
        "previous": {
            "system": {
                "cpu": previous_cpu,
                "memory": None,
                "disk": previous_disk,
            },
            "units": {}
            if previous_values is None
            else {"/system.slice/sshd.service": previous_values},
            "cgroups": {}
            if previous_values is None
            else {
                "/system.slice/sshd.service": previous_values,
                "/user.slice": {"memory_current": memory_current},
            },
        },
    }


def _capture_lines(
    *,
    system_memory_used: int | None = 4 * 1024**3,
    unit_memory_current: int | None = 100 * 1024**2,
    cgroup_memory_current: int | None = 64 * 1024**2,
) -> str:
    previous_values = {
        "cpu_usage_usec": 1_000_000,
        "memory_current": unit_memory_current,
        "io_rbytes": 0,
        "io_wbytes": 0,
    }
    current_values_one = {
        "cpu_usage_usec": 2_000_000,
        "memory_current": unit_memory_current,
        "io_rbytes": 1024,
        "io_wbytes": 512,
    }
    current_values_two = {
        "cpu_usage_usec": 3_000_000,
        "memory_current": unit_memory_current,
        "io_rbytes": 2048,
        "io_wbytes": 1024,
    }
    lines = [
        json.dumps(_meta()),
        json.dumps(
            _sample(
                monotonic_ns=1_000_000_000,
                realtime_ns=1_794_367_420_210_127_000,
                previous_cpu={"total": 1000, "idle": 900},
                current_cpu={"total": 1600, "idle": 1200},
                previous_disk={"read_bytes": 1024, "write_bytes": 512},
                current_disk={"read_bytes": 10_240, "write_bytes": 5_120},
                memory_used=system_memory_used,
                previous_values=previous_values,
                current_values=current_values_one,
                memory_current=cgroup_memory_current,
            )
        ),
        json.dumps(
            _sample(
                monotonic_ns=2_000_000_000,
                realtime_ns=1_794_367_421_210_127_000,
                previous_cpu={"total": 1600, "idle": 1200},
                current_cpu={"total": 2200, "idle": 1500},
                previous_disk={"read_bytes": 10_240, "write_bytes": 5_120},
                current_disk={"read_bytes": 12_288, "write_bytes": 6_144},
                memory_used=None if system_memory_used is None else system_memory_used + 1024,
                previous_values=current_values_one,
                current_values=current_values_two,
                memory_current=cgroup_memory_current,
            )
        ),
    ]
    return "\n".join(lines) + "\n"


def _context(data: Any, config: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(plugin_id=PLUGIN_ID, data=data, config=config or {}, run_id=RUN_ID)


def _request(options: dict[str, Any] | None = None) -> CollectRequest:
    return CollectRequest(
        target_id="target-1",
        run_id=RUN_ID,
        options={
            "machine_id": MACHINE_ID,
            "boot_id": BOOT_ID,
            "collection_id": COLLECTION_ID,
            **(options or {}),
        },
    )


def _records() -> list[dict[str, Any]]:
    capture = parse_capture(_capture_lines())
    return build_records(
        capture,
        plugin_id=PLUGIN_ID,
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
    )


# ---------------------------------------------------------------------------
# 契约形状
# ---------------------------------------------------------------------------


def test_collect_is_synchronous_and_matches_the_collector_contract() -> None:
    plugin = ResourceCollector()
    signature = inspect.signature(ResourceCollector.collect)

    assert isinstance(plugin, CollectorPlugin)
    assert inspect.iscoroutinefunction(plugin.collect) is False
    assert list(signature.parameters) == ["self", "request", "context"]


def test_spec_is_a_collector_with_resource_capabilities() -> None:
    assert ResourceCollector.spec.kind is PluginKind.COLLECTOR
    assert ResourceCollector.spec.capabilities
    assert "system.resources" in ResourceCollector.spec.capabilities


def test_builtins_register_the_collector_once() -> None:
    registry = register_builtins(PluginRegistry())

    collectors = registry.by_kind(PluginKind.COLLECTOR)
    assert [plugin.spec.id for plugin in collectors].count(PLUGIN_ID) == 1
    assert any(isinstance(plugin, ResourceCollector) for plugin in collectors)


# ---------------------------------------------------------------------------
# 记录换算：系统级、Unit 级与 cgroup 级可区分
# ---------------------------------------------------------------------------


def test_build_records_separates_system_unit_and_cgroup_dimensions() -> None:
    records = _records()
    by_name = {record["payload"]["name"]: record for record in records}

    assert set(by_name) == {
        SYSTEM_CPU,
        SYSTEM_MEMORY,
        SYSTEM_DISK_READ,
        SYSTEM_DISK_WRITE,
        UNIT_CPU,
        UNIT_MEMORY,
        UNIT_DISK_READ,
        UNIT_DISK_WRITE,
        CGROUP_CPU,
        CGROUP_MEMORY,
        CGROUP_DISK_READ,
        CGROUP_DISK_WRITE,
    }

    assert by_name[SYSTEM_CPU]["stream_id"] == "cpu"
    assert by_name[SYSTEM_CPU]["entity"] == "metric:cpu.total.utilization"
    assert by_name[SYSTEM_CPU]["payload"]["value"] == pytest.approx(50.0)

    assert by_name[UNIT_CPU]["stream_id"].startswith("cpu.unit:")
    assert by_name[UNIT_CPU]["entity"].startswith("metric:cpu.unit.utilization:")
    assert by_name[CGROUP_CPU]["stream_id"].startswith("cpu.cgroup:")
    assert by_name[CGROUP_CPU]["entity"].startswith("metric:cpu.cgroup.utilization:")


def test_every_metric_has_a_valid_time_and_entity() -> None:
    for record in _records():
        assert record["kind"] == "metric"
        assert record["clock_domain"] == "boot_monotonic"
        assert isinstance(record["monotonic_ns"], int) and record["monotonic_ns"] >= 0
        assert isinstance(record["realtime_ns"], int) and record["realtime_ns"] > 0
        assert record["machine_id"] == MACHINE_ID
        assert record["boot_id"] == BOOT_ID
        assert record["collection_id"] == COLLECTION_ID
        assert record["entity"]
        assert record["record_id"] == compute_record_id(record)


def test_missing_metrics_are_absent_instead_of_zero() -> None:
    capture = parse_capture(
        _capture_lines(
            system_memory_used=None,
            unit_memory_current=None,
            cgroup_memory_current=None,
        )
    )
    records = build_records(
        capture,
        plugin_id=PLUGIN_ID,
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
    )

    names = {record["payload"]["name"] for record in records}
    assert SYSTEM_MEMORY not in names
    assert UNIT_MEMORY not in names
    assert CGROUP_MEMORY not in names
    assert SYSTEM_CPU in names


def test_metric_stream_entities_are_derived_from_records() -> None:
    records = _records()
    entities = metric_stream_entities(records)
    by_id = {entity.entity_id: entity for entity in entities}

    assert len(entities) == len(by_id) == len({record["entity"] for record in records})
    for record in records:
        entity = by_id[record["entity"]]
        assert entity.name == record["payload"]["name"]
        assert entity.unit == record["payload"]["unit"]
        assert entity.machine_id == MACHINE_ID
        assert entity.boot_id == BOOT_ID
        assert entity.available is True


# ---------------------------------------------------------------------------
# collect 入口：窗口配置、写入与异常边界
# ---------------------------------------------------------------------------


def test_collect_writes_records_through_context_data() -> None:
    sampler = FakeSampler(_capture_lines())
    plugin = ResourceCollector(lambda _url: sampler)
    data = FakeDataAccess()

    result = plugin.collect(_request({"window_seconds": 2}), _context(data))

    assert result.records_written == len(data.records) == len(_records())
    assert data.written == [(PLUGIN_ID, RUN_ID)]
    assert sampler.commands[0][0] == "target-1"
    assert '"window_seconds":2.0' in sampler.commands[0][1]
    assert sampler.commands[0][2] >= 60


def test_default_sampling_window_is_thirty_seconds() -> None:
    sampler = FakeSampler(_capture_lines())
    plugin = ResourceCollector(lambda _url: sampler)

    plugin.collect(_request(), _context(FakeDataAccess()))

    assert '"window_seconds":30.0' in sampler.commands[0][1]


@pytest.mark.parametrize("window", [0, -1, "30", True])
def test_invalid_window_is_rejected(window: Any) -> None:
    plugin = ResourceCollector(lambda _url: FakeSampler(_capture_lines()))

    with pytest.raises(PluginInputError):
        plugin.collect(_request({"window_seconds": window}), _context(FakeDataAccess()))


def test_collect_returns_zero_when_the_target_reports_no_metrics() -> None:
    plugin = ResourceCollector(lambda _url: FakeSampler(json.dumps(_meta())))
    data = FakeDataAccess()

    result = plugin.collect(_request(), _context(data))

    assert result.records_written == 0
    assert data.records == []


def test_non_plugin_error_is_wrapped_by_registry_invoke() -> None:
    registry = PluginRegistry()
    registry.register(ResourceCollector(lambda _url: ExplodingSampler()))

    with pytest.raises(PluginExecutionError) as excinfo:
        registry.invoke(
            PLUGIN_ID,
            lambda plugin, context: plugin.collect(_request(), context),
            data=FakeDataAccess(),
            run_id=RUN_ID,
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
