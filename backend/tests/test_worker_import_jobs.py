"""Worker 导入任务处理器的单元测试：插件调用、结果写回、失败原样上抛。

不连数据库：数据入口用替身，插件用本地定义的小插件。真实链路上「插件经
``registry.invoke`` 把数据写进 PostgreSQL」的用例在
``tests/integration/test_api_import_timeline.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api_fakes import machine
from system_analyzer.domain.bootrec import CollectionMode, CollectionRef
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND
from system_analyzer.jobs.queue import Job
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    ImportResult,
    ImportSource,
    PluginBase,
    PluginContext,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
    PluginSpec,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import ImportOutcome, ImportStatus
from system_analyzer.worker.import_jobs import import_handlers, run_import_job

COLLECTION_ID = "collection:worker-0001"


class FakeAccess:
    """导入插件要求的数据入口：同步方法，把写入结果留在 ``last_outcome``。"""

    def __init__(self) -> None:
        self.last_outcome: ImportOutcome | None = None
        self.calls: list[tuple[str, str]] = []

    def get_collection(self, collection_id: str) -> None:
        return None

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Any,
        records: Any,
        *,
        source_sha256: str | None = None,
        resources: Any = None,
    ) -> ImportOutcome:
        self.calls.append((collection.collection_id, source_sha256 or ""))
        self.last_outcome = ImportOutcome(
            collection_id=collection.collection_id,
            status=ImportStatus.IMPORTED,
            records_written=len(records),
            entities_written=len(entities),
            skipped_reason=None,
        )
        return self.last_outcome


class WritingImporter(PluginBase):
    """正常插件：把一份采集交给数据入口，返回写入条数。"""

    spec = PluginSpec(
        id="sample.writer",
        kind=PluginKind.IMPORTER,
        name="写入样例插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.write",),
    )

    def __init__(self) -> None:
        self.contexts: list[PluginContext] = []

    def detect(self, source: ImportSource) -> bool:
        return True

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        self.contexts.append(context)
        collection = CollectionRef(
            collection_id=COLLECTION_ID,
            machine_id="machine:uos-PC",
            boot_id="boot:a",
            mode=CollectionMode.JOURNAL_IMPORT,
        )
        outcome = context.data.import_collection(
            collection, [machine()], [{"record": 1}, {"record": 2}]
        )
        return ImportResult(records_written=outcome.records_written)


class ExplodingImporter(PluginBase):
    """插件抛出的非 ``PluginError`` 异常：应被包成 ``PluginExecutionError``。"""

    spec = PluginSpec(
        id="sample.exploding",
        kind=PluginKind.IMPORTER,
        name="会崩溃的样例插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.explode",),
    )

    def detect(self, source: ImportSource) -> bool:
        return True

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        raise RuntimeError("插件内部炸了")


class RejectingImporter(PluginBase):
    """插件主动拒绝输入：``PluginInputError`` 原样上抛，由 Worker 记成任务失败。"""

    spec = PluginSpec(
        id="sample.rejecting",
        kind=PluginKind.IMPORTER,
        name="会拒绝输入的样例插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.reject",),
    )

    def detect(self, source: ImportSource) -> bool:
        return True

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        raise PluginInputError("这份归档不完整")


class RecordingQueue:
    """``JobResultStore`` 的替身：只记下任务结果。"""

    def __init__(self) -> None:
        self.results: list[tuple[Any, dict[str, Any]]] = []

    async def record_result(self, job_id: Any, result: dict[str, Any]) -> None:
        self.results.append((job_id, dict(result)))


def _job(upload_path: Path, plugin_id: str) -> Job:
    return Job(
        id=uuid4(),
        kind=SOURCE_IMPORT_JOB_KIND,
        payload={
            "plugin_id": plugin_id,
            "upload_id": "upload-1",
            "upload_path": str(upload_path),
            "filename": upload_path.name,
        },
        attempts=1,
    )


def _registry(plugin: PluginBase) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(plugin)
    return registry


async def test_import_job_writes_through_access_and_records_result(tmp_path: Path) -> None:
    upload = tmp_path / "boot.journal.gz"
    upload.write_bytes(b"payload")
    plugin = WritingImporter()
    queue = RecordingQueue()
    access = FakeAccess()
    job = _job(upload, plugin.spec.id)

    await run_import_job(
        job,
        registry=_registry(plugin),
        queue=queue,
        access_factory=lambda: access,
    )

    assert access.calls == [(COLLECTION_ID, "")]
    assert len(plugin.contexts) == 1
    # run_id 是任务 id：插件写的数据因此能关联到这次任务
    assert plugin.contexts[0].run_id == str(job.id)
    assert plugin.contexts[0].data is access
    assert queue.results == [
        (
            job.id,
            {
                "records_written": 2,
                "collection_id": COLLECTION_ID,
                "import_status": "imported",
                "records_skipped": 0,
                "skipped_reason": None,
            },
        )
    ]


async def test_non_plugin_error_is_wrapped_and_recorded_as_internal(tmp_path: Path) -> None:
    upload = tmp_path / "boot.journal.gz"
    upload.write_bytes(b"payload")
    queue = RecordingQueue()
    job = _job(upload, "sample.exploding")

    with pytest.raises(PluginExecutionError) as caught:
        await run_import_job(
            job,
            registry=_registry(ExplodingImporter()),
            queue=queue,
            access_factory=FakeAccess,
        )

    assert isinstance(caught.value.cause, RuntimeError)
    # 只写回稳定分类与固定文案，插件异常原文只进日志
    assert queue.results == [
        (
            job.id,
            {
                "failure": {
                    "kind": "internal",
                    "message": "导入任务执行失败，具体原因见 Worker 日志",
                }
            },
        )
    ]
    assert "插件内部炸了" not in str(queue.results)


async def test_plugin_input_error_propagates_and_is_recorded_as_invalid_input(
    tmp_path: Path,
) -> None:
    upload = tmp_path / "boot.journal.gz"
    upload.write_bytes(b"payload")
    queue = RecordingQueue()
    job = _job(upload, "sample.rejecting")

    with pytest.raises(PluginInputError):
        await run_import_job(
            job,
            registry=_registry(RejectingImporter()),
            queue=queue,
            access_factory=FakeAccess,
        )

    assert queue.results == [
        (job.id, {"failure": {"kind": "invalid_input", "message": "这份归档不完整"}})
    ]


async def test_missing_upload_file_fails_before_calling_the_plugin(tmp_path: Path) -> None:
    queue = RecordingQueue()
    plugin = WritingImporter()

    with pytest.raises(PluginInputError):
        await run_import_job(
            _job(tmp_path / "gone.journal.gz", plugin.spec.id),
            registry=_registry(plugin),
            queue=queue,
            access_factory=FakeAccess,
        )

    assert plugin.contexts == []
    assert [result["failure"]["kind"] for _, result in queue.results] == ["invalid_input"]


async def test_invalid_payload_fails_validation(tmp_path: Path) -> None:
    job = Job(
        id=uuid4(),
        kind=SOURCE_IMPORT_JOB_KIND,
        payload={"upload_path": str(tmp_path / "x.journal.gz")},
        attempts=1,
    )
    queue = RecordingQueue()

    with pytest.raises(ValidationError):
        await run_import_job(
            job,
            registry=_registry(WritingImporter()),
            queue=queue,
            access_factory=FakeAccess,
        )

    assert queue.results == [
        (
            job.id,
            {
                "failure": {
                    "kind": "internal",
                    "message": "导入任务执行失败，具体原因见 Worker 日志",
                }
            },
        )
    ]


def test_import_handlers_registers_the_import_job_kind() -> None:
    handlers = import_handlers()
    assert set(handlers) == {SOURCE_IMPORT_JOB_KIND}
    assert callable(handlers[SOURCE_IMPORT_JOB_KIND])
