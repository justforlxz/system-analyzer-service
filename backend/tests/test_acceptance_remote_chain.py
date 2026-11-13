"""验收：Mock SSH 下的远程采集、取消、超时与网络中断后的重新下载导入。

不需要验证机的部分全部用 ``tests/collection_fakes.py`` 的真编排假连接完成：

- 远程命令执行与文件传输都被记录下来，恢复后的远端文件与改动前逐字节一致；
- 取消与超时都走 Post 阶段，临时 LightDM 片段被删除，主配置不变；
- 回传下载中断时远端文件保留、本地旧包不被半截文件覆盖，恢复网络后重新下载并导入成功。

需要真实 PostgreSQL 的只有最后一步「导入」。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from collection_fakes import (
    LIFECYCLE_BOOT_ID,
    FakeConnector,
    FakeJournalCollector,
    FakeResourceCollector,
)
from sample_bootrec import SAMPLE_DIR
from system_analyzer.collection import sampling
from system_analyzer.collection.import_access import import_bootrec_file
from system_analyzer.collection.lightdm import AUTOLOGIN_FRAGMENT
from system_analyzer.collection.orchestrator import CollectionOutcome, run_collection
from system_analyzer.collection.plan import (
    CollectionStep,
    PhasePlan,
    backup_configuration,
    default_plan,
    inspect_target,
)
from system_analyzer.collection.states import AttemptStatus, CollectionPhase, PhaseStatus
from system_analyzer.collection.transfer import pull_with_retry, remove_remote_file
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import SshConnectError
from system_analyzer.storage import DiagnosticStore, ImportStatus
from system_analyzer.timeline import TimelineEngine

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

TARGET = "uos-152"
CONFIG_PATH = "/etc/lightdm/lightdm.conf"
ORIGINAL_CONFIG = b"[Seat:*]\n# original\n"
CHANGED_CONFIG = b"[Seat:*]\n# temporary\n"
TABLES = ("records", "entities", "collections", "boots", "machines", "jobs")

PRE = CollectionPhase.PRE
IN_PROGRESS = CollectionPhase.IN_PROGRESS
POST = CollectionPhase.POST


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url)
    async with engine.begin() as connection:
        for table in TABLES:
            await connection.execute(text(f"DELETE FROM {table}"))
    try:
        yield engine
    finally:
        await engine.dispose()


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
        run_id="run-acceptance",
        connector=connector,
        scratch_dir=scratch_dir,
        **kwargs,
    )


def assert_remote_restored(connector: FakeConnector) -> None:
    files = connector.files[TARGET]
    assert files[CONFIG_PATH] == ORIGINAL_CONFIG
    assert AUTOLOGIN_FRAGMENT not in files


async def test_mock_ssh_runs_commands_transfers_files_and_restores_lightdm(tmp_path: Path) -> None:
    connector = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})
    plans = dict(default_plan())
    plans[IN_PROGRESS] = PhasePlan(
        phase=IN_PROGRESS,
        steps=(
            *plans[IN_PROGRESS].steps,
            CollectionStep("改临时配置", change_config(CHANGED_CONFIG)),
        ),
    )

    outcome = await run(
        connector,
        tmp_path,
        plans=plans,
        options={
            "backup_paths": [CONFIG_PATH],
            "desktop_user": "uos",
            "desktop_login_timeout": 5,
        },
    )

    assert outcome.status is AttemptStatus.SUCCEEDED
    assert outcome.attempt.data["desktop_login"]["established"] is True
    assert connector.lightdm_restarts
    clients = connector.clients[TARGET]
    assert any(client.commands for client in clients)
    assert any(client.uploads for client in clients)
    assert connector.downloads_of(TARGET) == [CONFIG_PATH]
    assert_remote_restored(connector)


async def test_cancel_and_timeout_leave_no_temporary_remote_configuration(tmp_path: Path) -> None:
    cancel_connector = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})
    state = {"requested": False}

    async def request_cancel(_ctx) -> None:
        state["requested"] = True

    async def cancel_requested() -> bool:
        return state["requested"]

    plan = default_plan()
    plans = {
        PRE: PhasePlan(
            phase=PRE,
            steps=(
                CollectionStep("检查目标环境", inspect_target),
                CollectionStep("备份系统配置", backup_configuration),
                CollectionStep("改临时配置", change_config(CHANGED_CONFIG)),
                CollectionStep("请求取消", request_cancel),
            ),
        ),
        IN_PROGRESS: phase_with_noop("启动采样"),
        POST: plan[POST],
    }
    outcome = await run(
        cancel_connector,
        tmp_path,
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        cancelled=cancel_requested,
    )
    assert outcome.status is AttemptStatus.CANCELLED
    assert outcome.attempt.phases[POST].status is PhaseStatus.SUCCEEDED
    assert_remote_restored(cancel_connector)

    timed_out = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})

    async def slow(_ctx) -> None:
        async with _ctx.connect() as client:
            client.files[CONFIG_PATH] = CHANGED_CONFIG
        await asyncio.sleep(30)

    plans = with_steps(default_plan(), IN_PROGRESS, CollectionStep("持续采集", slow))
    outcome = await run(
        timed_out,
        tmp_path / "timeout",
        plans=plans,
        options={"backup_paths": [CONFIG_PATH]},
        phase_timeouts={IN_PROGRESS: 0.05},
    )
    assert outcome.status is AttemptStatus.TIMED_OUT
    assert outcome.attempt.phases[POST].status is PhaseStatus.SUCCEEDED
    assert_remote_restored(timed_out)


def phase_with_noop(name: str) -> PhasePlan:
    async def noop(_ctx) -> None:
        return None

    return PhasePlan(phase=IN_PROGRESS, steps=(CollectionStep(name, noop),))


class _InterruptedDownload:
    """第一次下载写半截文件后断开，第二次完整写出；远端文件始终保留。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.downloads = 0
        self.removed = False

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        self.downloads += 1
        path = Path(local_path)
        if self.downloads <= 2:
            path.write_bytes(self._payload[:64])  # noqa: ASYNC240  测试假实现
            raise SshConnectError("测试注入：传输中途连接断开")
        path.write_bytes(self._payload)  # noqa: ASYNC240  测试假实现

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        raise AssertionError("本次测试不上传")

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
        check: bool = True,
    ) -> CommandResult:
        if command.startswith("rm -f --"):
            self.removed = True
        return CommandResult(command=command, stdout="", stderr="", exit_status=0)


async def test_network_interruption_keeps_data_and_redownloads_before_import(
    tmp_path: Path, test_database_url: str, engine: AsyncEngine
) -> None:
    source = (SAMPLE_DIR / "full.bootrec").read_bytes()
    client = _InterruptedDownload(source)
    local = tmp_path / "downloads" / "full.bootrec"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"old pack must survive")

    failed = await pull_with_retry(client, "/var/tmp/full.bootrec", local, attempts=1)
    assert failed.succeeded is False
    assert failed.remote_retained is True
    assert local.read_bytes() == b"old pack must survive"
    assert not (local.parent / (local.name + ".part")).exists()
    assert await remove_remote_file(client, "/var/tmp/full.bootrec", imported=False) is False
    assert client.removed is False

    report = await pull_with_retry(client, "/var/tmp/full.bootrec", local, attempts=2)
    assert report.succeeded is True
    assert [attempt.succeeded for attempt in report.attempts] == [False, True]
    assert local.read_bytes() == source

    outcome = await asyncio.to_thread(import_bootrec_file, str(local), test_database_url)
    assert outcome.records_written > 0
    store = DiagnosticStore(engine)
    state = await store.get_collection(outcome.collection_id)
    assert state is not None and state.import_status.value == "imported"
    assert await remove_remote_file(client, "/var/tmp/full.bootrec", imported=True) is True
    assert client.removed is True


async def test_ssh_collection_produces_bootrec_with_resources_and_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    engine: AsyncEngine,
) -> None:
    """一次完整采集：采样 → 打包 → 回传 → 导入 → 时间线可读。

    资源与 Journal 采集器用假插件替代：它们按插件契约写记录与产物，但不连真机。这样验证的
    是编排有没有真的调用采集器、产物有没有进入打包与导入，而不是采集器自身的解析逻辑。
    """
    registry = PluginRegistry()
    registry.register(FakeResourceCollector())
    registry.register(FakeJournalCollector())
    monkeypatch.setattr(sampling, "get_registry", lambda: registry)
    connector = FakeConnector({TARGET: {CONFIG_PATH: ORIGINAL_CONFIG}})
    remote_pack = "/var/tmp/sas-acceptance-784.bootrec"

    outcome = await run(
        connector,
        tmp_path,
        options={
            "backup_paths": [CONFIG_PATH],
            "delivery": {"remote_path": remote_pack, "stage_local_pack": True},
            "database_url": test_database_url,
        },
    )

    assert outcome.status is AttemptStatus.SUCCEEDED
    expected_delivery = outcome.attempt.data["delivery"]
    assert expected_delivery["status"] == "imported"
    assert expected_delivery["import_status"] == "imported"
    assert expected_delivery["records_written"] >= 2
    assert expected_delivery["remote_removed"] is True

    collection_id = expected_delivery["collection_id"]
    assert collection_id == "collection:run-acceptance"
    store = DiagnosticStore(engine)
    state = await store.get_collection(collection_id)
    assert state is not None
    assert state.import_status is ImportStatus.IMPORTED
    assert [item.collection.collection_id for item in await store.list_collections()] == [
        collection_id
    ]

    records = await store.list_records_in_collection(collection_id)
    assert any(record.kind is RecordKind.METRIC for record in records)
    assert any(record.kind is RecordKind.EVENT for record in records)
    tracks = await TimelineEngine(store).tracks(f"machine:{TARGET}", LIFECYCLE_BOOT_ID)
    assert tracks.resources.available is True
    assert any(track.kind is RecordKind.METRIC for track in tracks.tracks)

    local_path = Path(expected_delivery["local_path"])
    with zipfile.ZipFile(local_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        journal_rows = [line for line in archive.read("journal.jsonl").splitlines() if line]
    assert manifest["counts"]["journal_entries"] > 0
    assert manifest["resources"]["available"] is True
    assert journal_rows
