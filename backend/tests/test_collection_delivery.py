"""采集回传与 Post 环境恢复互不影响的测试（任务 3.7）。

``collection.plan`` 的 Post 阶段按顺序执行「停止采样 → 收集数据 → 恢复环境」。回传失败时如果
直接抛异常，恢复步骤会被编排跳过，目标机器就停在半改状态。这里用真实编排与假 SSH 连接证明：
回传失败后远端配置仍被恢复，并且失败原因写进记录。
"""

from __future__ import annotations

import asyncio
import json
import shlex
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from collection_fakes import FakeConnector
from system_analyzer.collection import delivery
from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.orchestrator import CollectionOutcome, run_collection
from system_analyzer.collection.packaging import PackagingInput
from system_analyzer.collection.plan import (
    CollectionStep,
    PhasePlan,
    default_plan,
    gather_collected_data,
)
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus
from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    MachineEntity,
    compute_record_id,
)
from system_analyzer.importers.bootrec_importer import BootrecImportOutcome
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import SshConnectError

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"
CHANGED_CONFIG = b"[Seat:*]\n# temporary\n"

MACHINE_ID = "machine:uos-152"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
COLLECTION_ID = "collection:delivery-0001"
CREATED_NS = 1_794_009_600_000_000_000
REMOTE_PACK = "/var/tmp/sas/delivery.bootrec"
DATABASE_URL = "postgresql+asyncpg://unused"

PRE = CollectionPhase.PRE
IN_PROGRESS = CollectionPhase.IN_PROGRESS
POST = CollectionPhase.POST


def with_steps(plans: dict, phase: CollectionPhase, *steps) -> dict:
    result = dict(plans)
    result[phase] = PhasePlan(phase=phase, steps=tuple(steps))
    return result


def change_config(next_value: bytes):
    async def action(ctx) -> None:
        async with ctx.connect() as client:
            client.files[CONFIG_PATH] = next_value

    return action


async def run(connector: FakeConnector, scratch_dir: Path, **kwargs) -> CollectionOutcome:
    return await run_collection(
        target_id=TARGET,
        run_id="run-delivery",
        connector=connector,
        scratch_dir=scratch_dir,
        **kwargs,
    )


async def test_delivery_failure_still_restores_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connector = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})

    async def explode(_ctx):
        raise delivery.DeliveryError("测试注入：回传中断")

    monkeypatch.setattr("system_analyzer.collection.plan.deliver_from_context", explode)
    plans = with_steps(
        default_plan(),
        IN_PROGRESS,
        CollectionStep("改临时配置", change_config(CHANGED_CONFIG)),
    )

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH], "delivery": {}},
    )

    assert outcome.status is AttemptStatus.FAILED
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG
    assert outcome.attempt.data["delivery"]["status"] == "failed"
    assert outcome.attempt.phases[POST].status is PhaseStatus.FAILED


async def test_delivery_is_skipped_without_explicit_option(tmp_path: Path) -> None:
    connector = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})

    outcome = await run(connector, tmp_path)

    assert outcome.status is AttemptStatus.SUCCEEDED
    assert outcome.attempt.data["delivery"]["status"] == "skipped"


class _DownloadClient:
    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        if self._fail:
            raise SshConnectError("测试注入：连接已断开")
        Path(local_path).write_bytes(b"bootrec-bytes")  # noqa: ASYNC240  测试假实现

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        raise AssertionError("本次测试不上传")

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
        check: bool = True,
    ) -> CommandResult:
        return CommandResult(command=command, stdout="", stderr="", exit_status=0)


class _ReconnectingConnector:
    def __init__(self, clients: list[_DownloadClient]) -> None:
        self._clients = clients
        self.connections = 0

    def connect(self, target_id: str):
        @asynccontextmanager
        async def connected():
            self.connections += 1
            yield self._clients.pop(0)

        return connected()


async def test_delivery_opens_a_new_connection_after_network_loss(tmp_path: Path) -> None:
    connector = _ReconnectingConnector([_DownloadClient(fail=True), _DownloadClient(fail=False)])
    ctx = PhaseContext(
        target_id="uos-152",
        run_id="run-reconnect",
        attempt=1,
        options={},
        scratch_dir=tmp_path,
        data={},
        connector=connector,
    )

    report = await delivery._pull_with_reconnect(
        ctx,
        "/tmp/delivery.bootrec",
        tmp_path / "local.bootrec",
        attempts=2,
        delay_seconds=0,
    )

    assert report.succeeded is True
    assert [attempt.succeeded for attempt in report.attempts] == [False, True]
    assert connector.connections == 2
    assert (tmp_path / "local.bootrec").read_bytes() == b"bootrec-bytes"


def _packaging_source() -> PackagingInput:
    body = {
        "kind": "metric",
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "collection_id": COLLECTION_ID,
        "source_plugin": "resource-sampler",
        "stream_id": "system.cpu",
        "monotonic_ns": 2_000_000_000,
        "realtime_ns": CREATED_NS,
        "clock_domain": "boot_monotonic",
        "entity": "metric:system.cpu",
        "payload": {"name": "system.cpu.percent", "unit": "percent", "value": 12.5},
    }
    return PackagingInput(
        collection=CollectionRef(
            collection_id=COLLECTION_ID,
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            mode=CollectionMode.SSH_COLLECTION,
            created_realtime_ns=CREATED_NS,
        ),
        machine=MachineEntity(
            entity_type="machine",
            machine_id=MACHINE_ID,
            hostname="uos-152",
        ),
        boot=BootEntity(
            entity_type="boot",
            machine_id=MACHINE_ID,
            boot_id=BOOT_ID,
            started_realtime_ns=CREATED_NS - 1_000_000_000,
        ),
        records=({"record_id": compute_record_id(body), **body},),
    )


def _import_outcome() -> BootrecImportOutcome:
    return BootrecImportOutcome(
        collection_id=COLLECTION_ID,
        status="imported",
        records_written=1,
        records_skipped=0,
        entities_written=0,
        entities_skipped=0,
        journal_entries=0,
        artifacts=(),
        digest="",
        warnings=(),
    )


class _DeliveryClient:
    """回传测试用的假 SSH 客户端：远端文件存在 ``storage`` 里，``run`` 只模拟删除。"""

    def __init__(
        self,
        storage: dict[str, bytes],
        events: list[str],
        *,
        fail_download: bool,
    ) -> None:
        self._storage = storage
        self._events = events
        self._fail_download = fail_download

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        self._events.append("download")
        if self._fail_download:
            raise SshConnectError("测试注入：连接已断开")
        payload = self._storage.get(remote_path, b"remote-bootrec")
        self._storage[remote_path] = payload
        Path(local_path).write_bytes(payload)  # noqa: ASYNC240  测试假实现

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        self._storage[remote_path] = Path(local_path).read_bytes()  # noqa: ASYNC240

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
        check: bool = True,
    ) -> CommandResult:
        if command.startswith("rm -f -- "):
            self._storage.pop(shlex.split(command)[-1], None)
            self._events.append("rm")
        return CommandResult(command=command, stdout="", stderr="", exit_status=0)


class _DeliveryConnector:
    def __init__(
        self, storage: dict[str, bytes], events: list[str], *, fail_download: bool
    ) -> None:
        self._storage = storage
        self._events = events
        self._fail_download = fail_download
        self.connections = 0

    def connect(self, target_id: str):
        @asynccontextmanager
        async def connected():
            self.connections += 1
            yield _DeliveryClient(
                self._storage,
                self._events,
                fail_download=self._fail_download,
            )

        return connected()


def _context(
    tmp_path: Path,
    connector: _DeliveryConnector,
    *,
    options: dict,
    run_id: str = "run-delivery",
) -> PhaseContext:
    return PhaseContext(
        target_id=TARGET,
        run_id=run_id,
        attempt=1,
        options=options,
        scratch_dir=tmp_path,
        data={},
        connector=connector,
    )


def _delivery_options(**extra: object) -> dict:
    return {"delivery": {"remote_path": REMOTE_PACK, **extra}, "database_url": DATABASE_URL}


async def test_deliver_from_context_imports_then_removes_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = {REMOTE_PACK: b"remote-bootrec"}
    events: list[str] = []
    connector = _DeliveryConnector(storage, events, fail_download=False)

    async def fake_source(_ctx):
        return _packaging_source()

    def fake_import(_path: str, _database_url: str) -> BootrecImportOutcome:
        events.append("import")
        return _import_outcome()

    monkeypatch.setattr(delivery, "_load_source", fake_source)
    monkeypatch.setattr(delivery, "import_bootrec_file", fake_import)

    ctx = _context(tmp_path, connector, options=_delivery_options())
    report = await delivery.deliver_from_context(ctx)

    assert report is not None
    assert report.status == "imported"
    assert report.imported is True
    assert report.records_written == 1
    assert report.import_status == "imported"
    assert report.remote_removed is True
    assert report.transfer is not None and report.transfer["succeeded"] is True
    assert Path(report.pack_path or "").is_file()  # noqa: ASYNC240  测试断言
    assert delivery.NO_JOURNAL_WARNING in report.warnings
    assert events == ["download", "import", "rm"]
    assert connector.connections == 2
    assert REMOTE_PACK not in storage


async def test_deliver_from_context_keeps_remote_when_import_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = {REMOTE_PACK: b"remote-bootrec"}
    events: list[str] = []
    connector = _DeliveryConnector(storage, events, fail_download=False)

    async def fake_source(_ctx):
        return _packaging_source()

    def failing_import(_path: str, _database_url: str) -> BootrecImportOutcome:
        events.append("import")
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(delivery, "_load_source", fake_source)
    monkeypatch.setattr(delivery, "import_bootrec_file", failing_import)

    ctx = _context(tmp_path, connector, options=_delivery_options())
    with pytest.raises(delivery.DeliveryError, match="导入失败"):
        await delivery.deliver_from_context(ctx)

    assert events == ["download", "import"]
    assert REMOTE_PACK in storage


async def test_deliver_from_context_keeps_remote_when_download_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = {REMOTE_PACK: b"remote-bootrec"}
    events: list[str] = []
    connector = _DeliveryConnector(storage, events, fail_download=True)

    async def fake_source(_ctx):
        return _packaging_source()

    def unexpected_import(_path: str, _database_url: str) -> BootrecImportOutcome:
        raise AssertionError("下载失败时不应导入")

    monkeypatch.setattr(delivery, "_load_source", fake_source)
    monkeypatch.setattr(delivery, "import_bootrec_file", unexpected_import)

    with pytest.raises(delivery.DeliveryError, match="回传失败"):
        await delivery.deliver_from_context(
            _context(
                tmp_path,
                connector,
                options=_delivery_options(download_attempts=2, retry_delay_seconds=0),
            )
        )

    assert events == ["download", "download"]
    assert connector.connections == 2
    assert REMOTE_PACK in storage


async def test_gather_restores_environment_when_post_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connector = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})

    async def slow_delivery(_ctx):
        await asyncio.sleep(60)

    monkeypatch.setattr("system_analyzer.collection.plan.deliver_from_context", slow_delivery)
    plans = with_steps(
        default_plan(),
        IN_PROGRESS,
        CollectionStep("改临时配置", change_config(CHANGED_CONFIG)),
    )
    plans = with_steps(plans, POST, CollectionStep("收集数据", gather_collected_data))

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH], "delivery": {}},
        phase_timeouts={POST: 0.05},
    )

    assert outcome.status is AttemptStatus.FAILED
    assert outcome.attempt.phases[POST].status is PhaseStatus.TIMED_OUT
    assert outcome.attempt.data["delivery"]["status"] == "failed"
    assert connector.files[TARGET][CONFIG_PATH] == ORIGINAL_CONFIG


def _journal_context(tmp_path: Path, *, options: dict | None = None) -> PhaseContext:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    connector = _DeliveryConnector({}, [], fail_download=False)
    return _context(scratch, connector, options=options or {"delivery": {}}, run_id="run-journal")


def test_journal_artifacts_are_discovered_from_collector_cursor(tmp_path: Path) -> None:
    work = tmp_path / "journal-run-journal"
    work.mkdir()
    export = work / "journal-run-journal.export"
    export.write_text("export", encoding="utf-8")
    entities = work / "journal-run-journal.entities.jsonl"
    entities.write_text("{}\n", encoding="utf-8")
    (work / "journal-run-journal.cursor.json").write_text(
        json.dumps(
            {"export_file": export.name, "entities_file": entities.name},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ctx = _journal_context(tmp_path)

    artifacts = delivery.resolve_journal_artifacts(ctx)

    assert artifacts.export == export.resolve()
    assert artifacts.entities == entities.resolve()
    assert ctx.data["journal"]["export_file"] == str(export.resolve())
    assert ctx.data["journal"]["work_dir"] == str(work.resolve())


def test_journal_artifacts_resolve_relative_explicit_path(tmp_path: Path) -> None:
    work = tmp_path / "journal-run-journal"
    work.mkdir()
    export = work / "journal-run-journal.export"
    export.write_text("export", encoding="utf-8")
    ctx = _journal_context(
        tmp_path,
        options={"delivery": {"journal_export": export.name}},
    )

    artifacts = delivery.resolve_journal_artifacts(ctx)

    assert artifacts.export == export.resolve()


def test_journal_artifacts_reject_configured_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.export"
    ctx = _journal_context(
        tmp_path,
        options={"delivery": {"journal_export": str(missing)}},
    )

    with pytest.raises(delivery.DeliveryError, match="Journal 导出文件不存在"):
        delivery.resolve_journal_artifacts(ctx)
