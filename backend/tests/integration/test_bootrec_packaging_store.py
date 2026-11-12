"""打包结果在真实 PostgreSQL 上的导入、读回与重复导入（任务 3.7）。

单元测试证明容器结构正确；这里证明 1.3 的导入路径真的收下了它，并且：
- Journal 六个原始字段写进库后仍存在，内核 OOM 仍能被 2.2 识别；
- 同一份文件导入两次，第二次整批跳过，记录数不增长；
- 只有资源采集的包导入后，缺失指标流以 ``available = false`` 读回。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.analyzers.oom import detect_kill
from system_analyzer.collection.import_access import (
    SynchronousCollectionAccess,
    import_bootrec_file,
)
from system_analyzer.collection.packaging import PackagingInput, build_bootrec
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    Record,
    compute_record_id,
)
from system_analyzer.storage import DiagnosticStore

pytestmark = pytest.mark.integration

MACHINE_ID = "machine:uos-packaging"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
COLLECTION_ID = "collection:packaging-test"
CREATED_NS = 1_794_009_600_000_000_000
TABLES = ("records", "entities", "collections", "boots", "machines")

SIX_FIELDS = (
    "_TRANSPORT",
    "SYSLOG_IDENTIFIER",
    "UNIT",
    "USER_UNIT",
    "_SYSTEMD_UNIT",
    "INVOCATION_ID",
)

OOM_EXPORT = """\
__CURSOR=s=abc;i=1
__REALTIME_TIMESTAMP=1794009601200000
__MONOTONIC_TIMESTAMP=1200000
_BOOT_ID=3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11
_HOSTNAME=uos-packaging
_KERNEL_VERSION=6.6.0-1-amd64
_TRANSPORT=kernel
SYSLOG_IDENTIFIER=kernel
PRIORITY=3
_SYSTEMD_UNIT=firefox.service
UNIT=firefox.service
USER_UNIT=app-firefox.service
INVOCATION_ID=11112222333344445555666677778888
_PID=4310
MESSAGE=Out of memory: Killed process 4310 (firefox) total-vm:2846920kB, anon-rss:1804808kB

"""


def run_store(url: str, call: Callable[[DiagnosticStore], Coroutine[Any, Any, Any]]) -> Any:
    async def body() -> Any:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            return await call(DiagnosticStore(engine))
        finally:
            await engine.dispose()

    return asyncio.run(body())


def clear(url: str) -> None:
    async def body() -> None:
        engine: AsyncEngine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                for table in TABLES:
                    await connection.execute(text(f"DELETE FROM {table}"))
        finally:
            await engine.dispose()

    asyncio.run(body())


@pytest.fixture
def database_url(test_database_url: str) -> str:
    clear(test_database_url)
    return test_database_url


def collection() -> CollectionRef:
    return CollectionRef(
        collection_id=COLLECTION_ID,
        machine_id=MACHINE_ID,
        boot_id=BOOT_ID,
        mode=CollectionMode.SSH_COLLECTION,
        created_realtime_ns=CREATED_NS,
    )


def machine() -> MachineEntity:
    return MachineEntity(entity_type="machine", machine_id=MACHINE_ID, hostname="uos-packaging")


def boot() -> BootEntity:
    return BootEntity(entity_type="boot", machine_id=MACHINE_ID, boot_id=BOOT_ID)


def metric_record(name: str, unit: str, value: float, entity: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "metric",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "resource-sampler",
        "stream_id": name.split(".")[0],
        "monotonic_ns": 2_000_000_000,
        "realtime_ns": CREATED_NS,
        "clock_domain": "boot_monotonic",
        "entity": entity,
        "payload": {"name": name, "unit": unit, "value": value},
    }
    return {"record_id": compute_record_id(body), **body}


def test_journal_pack_imports_twice_and_keeps_original_fields(
    tmp_path: Path, database_url: str
) -> None:
    export = tmp_path / "journal.export"
    export.write_text(OOM_EXPORT, encoding="utf-8")
    from system_analyzer.collection.packaging import packaging_input_from_journal

    pack = build_bootrec(
        packaging_input_from_journal(collection(), export, resources_expected=False),
        tmp_path / "journal.bootrec",
    )

    first = import_bootrec_file(str(pack.path), database_url)
    second = import_bootrec_file(str(pack.path), database_url)

    assert first.records_written == 1
    assert second.records_written == 0
    assert second.skipped_reason is not None
    assert run_store(database_url, lambda store: store.count_records(COLLECTION_ID)) == 1
    assert (
        run_store(database_url, lambda store: store.check_collection_integrity(COLLECTION_ID)) == []
    )

    records = run_store(database_url, lambda store: store.list_records_in_collection(COLLECTION_ID))
    kernel = next(
        record
        for record in records
        if record.kind.value == "event"
        and record.payload.get("attributes", {}).get("fields", {}).get("_TRANSPORT") == "kernel"
    )
    expected = {
        "_TRANSPORT": "kernel",
        "SYSLOG_IDENTIFIER": "kernel",
        "UNIT": "firefox.service",
        "USER_UNIT": "app-firefox.service",
        "_SYSTEMD_UNIT": "firefox.service",
        "INVOCATION_ID": "11112222333344445555666677778888",
    }
    for name in SIX_FIELDS:
        assert kernel.payload["attributes"]["fields"][name] == expected[name]
    kill = detect_kill(Record.model_validate(kernel.model_dump(mode="json")))
    assert kill is not None and kill.source == "kernel"


def test_resources_only_pack_reads_unavailable_streams(tmp_path: Path, database_url: str) -> None:
    data = PackagingInput(
        collection=collection(),
        machine=machine(),
        boot=boot(),
        records=(
            metric_record("cpu.total.utilization", "percent", 12.5, "metric:cpu.total.utilization"),
        ),
        resources_expected=True,
    )
    pack = build_bootrec(data, tmp_path / "resources.bootrec")

    outcome = import_bootrec_file(str(pack.path), database_url)

    assert outcome.records_written == 1
    streams = {
        stream.entity_id: stream
        for stream in run_store(
            database_url,
            lambda store: store.list_metric_streams(MACHINE_ID, BOOT_ID),
        )
    }
    assert set(streams) == {
        "metric:cpu.total.utilization",
        "metric:memory.used",
        "metric:disk.read_bytes_per_second",
        "metric:disk.write_bytes_per_second",
    }
    assert streams["metric:cpu.total.utilization"].available is True
    assert streams["metric:memory.used"].available is False
    assert streams["metric:memory.used"].unavailable_reason
    assert (
        run_store(database_url, lambda store: store.check_collection_integrity(COLLECTION_ID)) == []
    )


def test_synchronous_access_is_not_callable_from_event_loop(database_url: str) -> None:
    access = SynchronousCollectionAccess(database_url)

    async def body() -> None:
        with pytest.raises(RuntimeError):
            access.get_collection(COLLECTION_ID)

    asyncio.run(body())
