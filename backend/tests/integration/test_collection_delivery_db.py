"""已导入采集再次回传时只清理远端（任务 MKACG-805）。

单元测试用假存储覆盖了这条分支；这里在真实 PostgreSQL 上走完「导入成功 → 再次回传」，
证明导入状态确实从 ``collections.import_status`` 读到，并且重试不会再读记录、不会生成新包。
"""

from __future__ import annotations

import asyncio
import shlex
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.delivery import deliver_from_context
from system_analyzer.collection.import_access import import_bootrec_file
from system_analyzer.collection.packaging import PackagingInput, build_bootrec
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    compute_record_id,
)
from system_analyzer.remote.client import CommandResult

pytestmark = pytest.mark.integration

MACHINE_ID = "machine:uos-retry"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
COLLECTION_ID = "collection:delivery-retry"
CREATED_NS = 1_794_009_600_000_000_000
REMOTE_PACK = "/var/tmp/sas/delivery-retry.bootrec"
TABLES = ("records", "entities", "collections", "boots", "machines")


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


def metric_record() -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "metric",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "resource-sampler",
        "stream_id": "system.cpu",
        "monotonic_ns": 2_000_000_000,
        "realtime_ns": CREATED_NS,
        "clock_domain": "boot_monotonic",
        "entity": "metric:cpu.total.utilization",
        "payload": {"name": "cpu.total.utilization", "unit": "percent", "value": 12.5},
    }
    return {"record_id": compute_record_id(body), **body}


class _RemoteClient:
    def __init__(self, storage: dict[str, bytes], events: list[str]) -> None:
        self._storage = storage
        self._events = events

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        raise AssertionError("已经导入成功的采集不应重新下载")

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        raise AssertionError("已经导入成功的采集不应重新上传")

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
        check: bool = True,
    ) -> CommandResult:
        self._events.append("rm")
        self._storage.pop(shlex.split(command)[-1], None)
        return CommandResult(command=command, stdout="", stderr="", exit_status=0)


class _Connector:
    def __init__(self, storage: dict[str, bytes], events: list[str]) -> None:
        self._storage = storage
        self._events = events
        self.connections = 0

    def connect(self, target_id: str):
        @asynccontextmanager
        async def connected():
            self.connections += 1
            yield _RemoteClient(self._storage, self._events)

        return connected()


async def test_already_imported_collection_retry_only_cleans_remote(
    tmp_path: Path, database_url: str
) -> None:
    pack = build_bootrec(
        PackagingInput(
            collection=collection(),
            machine=MachineEntity(
                entity_type="machine", machine_id=MACHINE_ID, hostname="uos-retry"
            ),
            boot=BootEntity(entity_type="boot", machine_id=MACHINE_ID, boot_id=BOOT_ID),
            records=(metric_record(),),
            resources_expected=True,
        ),
        tmp_path / "first.bootrec",
    )
    outcome = await asyncio.to_thread(import_bootrec_file, str(pack.path), database_url)
    assert outcome.status == "imported"

    storage = {REMOTE_PACK: b"remote-bootrec"}
    events: list[str] = []
    connector = _Connector(storage, events)
    scratch = tmp_path / "retry-scratch"
    scratch.mkdir()
    ctx = PhaseContext(
        target_id="uos-152",
        run_id="run-delivery-retry",
        attempt=2,
        options={
            "delivery": {"remote_path": REMOTE_PACK},
            "database_url": database_url,
            "collection_id": COLLECTION_ID,
        },
        scratch_dir=scratch,
        data={},
        connector=connector,
    )

    report = await deliver_from_context(ctx)

    assert report is not None
    assert report.status == "already_imported"
    assert report.imported is True
    assert report.import_status == "imported"
    assert report.remote_removed is True
    assert events == ["rm"]
    assert connector.connections == 1
    assert REMOTE_PACK not in storage
    assert list(scratch.glob("*.bootrec")) == []
