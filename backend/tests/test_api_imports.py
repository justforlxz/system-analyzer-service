"""导入接口的单元测试：上传、提交任务、查询任务状态。

不连数据库：任务队列用 ``tests/api_fakes.py`` 的替身，插件用仓库里已有的样例插件与一个
「调用时崩溃」的插件。真实链路上「上传 → 导入 → 查询」三步走通在
``tests/integration/test_api_import_timeline.py``。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api_fakes import FakeJobQueue, FakeStore, collection, make_app, make_client
from fake_plugins import FakeAnalyzer
from system_analyzer.core.config import Settings
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND, JobStatus
from system_analyzer.jobs.queue import JobRecord
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    ImportResult,
    ImportSource,
    PluginBase,
    PluginKind,
    PluginSpec,
)
from system_analyzer.plugins.registry import PluginRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "journal_archive"
JOURNAL_FIXTURE = FIXTURES / "boot.journal.gz"
ROOT = Path(__file__).parent.parent.parent
BOOTREC_SAMPLE = ROOT / "docs" / "samples" / "bootrec-v1" / "full.bootrec"


class CrashingImporter(PluginBase):
    """``detect`` 直接抛异常的导入插件：证明插件崩溃表现为内部错误，而不是裸 500 或静默成功。"""

    spec = PluginSpec(
        id="sample.crashing-importer",
        kind=PluginKind.IMPORTER,
        name="会崩溃的导入插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.crash",),
    )

    def detect(self, source: ImportSource) -> bool:
        raise RuntimeError("插件在 detect 里崩了")

    def import_source(self, source: ImportSource, context) -> ImportResult:  # pragma: no cover
        return ImportResult(records_written=0)


def _assert_saved(saved: Path, upload_dir: Path, payload: bytes) -> None:
    """上传结果：文件在上传目录里、内容与上传的一致。

    断言放在同步函数里，因为 ``Path`` 的读写是阻塞调用，不该直接写进协程。
    """
    assert saved.is_file()
    assert saved.parent == upload_dir
    assert saved.read_bytes() == payload


def _files(directory: Path) -> list[Path]:
    """上传目录里还剩哪些文件（``Path.iterdir`` 是阻塞调用，放进同步函数）。"""
    return sorted(directory.iterdir())


def _settings(tmp_path: Path) -> Settings:
    return Settings(upload_dir=str(tmp_path))


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    return tmp_path


async def test_upload_journal_archive_submits_import_job(upload_dir: Path) -> None:
    queue = FakeJobQueue()
    payload = JOURNAL_FIXTURE.read_bytes()
    async with make_client(make_app(queue=queue, settings=_settings(upload_dir))) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("boot.journal.gz", payload, "application/gzip")},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["plugin_id"] == "journal.archive"
    assert body["filename"] == "boot.journal.gz"
    assert body["size_bytes"] == len(payload)
    assert body["source_sha256"] == hashlib.sha256(payload).hexdigest()

    assert len(queue.enqueued) == 1
    kind, job_payload = queue.enqueued[0]
    assert kind == SOURCE_IMPORT_JOB_KIND
    assert job_payload["plugin_id"] == "journal.archive"
    assert job_payload["upload_id"] == body["upload_id"]
    saved = Path(job_payload["upload_path"])
    await asyncio.to_thread(_assert_saved, saved, upload_dir, payload)


async def test_upload_bootrec_submits_import_job(upload_dir: Path) -> None:
    queue = FakeJobQueue()
    payload = BOOTREC_SAMPLE.read_bytes()
    async with make_client(make_app(queue=queue, settings=_settings(upload_dir))) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("full.bootrec", payload, "application/zip")},
        )

    assert response.status_code == 202
    assert response.json()["plugin_id"] == "bootrec.importer"
    assert queue.enqueued[0][1]["plugin_id"] == "bootrec.importer"


async def test_upload_unknown_format_is_invalid_format(upload_dir: Path) -> None:
    queue = FakeJobQueue()
    async with make_client(make_app(queue=queue, settings=_settings(upload_dir))) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("notes.txt", b"just text", "text/plain")},
        )

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["type"] == "invalid_format"
    assert "notes.txt" in body["message"]
    assert any("journal.archive" in detail for detail in body["details"]), body
    assert queue.enqueued == []
    # 被拒的上传不留下已写入的文件
    assert await asyncio.to_thread(_files, upload_dir) == [], "被拒的上传不留下已写入的文件"


async def test_upload_with_unknown_plugin_is_invalid_parameter(upload_dir: Path) -> None:
    queue = FakeJobQueue()
    async with make_client(make_app(queue=queue, settings=_settings(upload_dir))) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("boot.journal.gz", JOURNAL_FIXTURE.read_bytes(), "application/gzip")},
            data={"plugin_id": "missing.importer"},
        )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["type"] == "invalid_parameter"
    assert "missing.importer" in body["message"]


async def test_upload_with_non_importer_plugin_is_invalid_parameter(upload_dir: Path) -> None:
    registry = PluginRegistry()
    registry.register(FakeAnalyzer())
    queue = FakeJobQueue()
    async with make_client(
        make_app(queue=queue, plugins=registry, settings=_settings(upload_dir))
    ) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("boot.journal.gz", JOURNAL_FIXTURE.read_bytes(), "application/gzip")},
            data={"plugin_id": "sample.analyzer"},
        )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["type"] == "invalid_parameter"
    assert "不是导入插件" in body["message"]


async def test_plugin_crash_is_internal_error(upload_dir: Path) -> None:
    registry = PluginRegistry()
    registry.register(CrashingImporter())
    queue = FakeJobQueue()
    async with make_client(
        make_app(queue=queue, plugins=registry, settings=_settings(upload_dir))
    ) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("boot.journal.gz", JOURNAL_FIXTURE.read_bytes(), "application/gzip")},
        )

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["type"] == "internal_error"
    # 只给固定文案加请求 id，插件抛出的异常类型与原文不进响应
    assert "请求 id" in body["message"]
    assert "RuntimeError" not in response.text
    assert "插件在 detect 里崩了" not in response.text
    assert queue.enqueued == [], "插件崩溃时不应该把任务排进队列"


async def test_import_status_unknown_job_is_not_found() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/imports/0f0f0f0f-0f0f-0f0f-0f0f-0f0f0f0f0f0f")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


async def test_import_status_reports_result_and_collection() -> None:
    queue = FakeJobQueue()
    store = FakeStore(
        collections={"collection:imported-0001": collection("collection:imported-0001")}
    )
    when = datetime(2026, 1, 1, tzinfo=UTC)
    job_id = await queue.enqueue(
        SOURCE_IMPORT_JOB_KIND,
        {
            "plugin_id": "journal.archive",
            "upload_id": "upload-1",
            "upload_path": "/tmp/upload-1",
            "filename": "boot.journal.gz",
        },
    )
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=SOURCE_IMPORT_JOB_KIND,
        payload={
            "plugin_id": "journal.archive",
            "upload_id": "upload-1",
            "upload_path": "/tmp/upload-1",
            "filename": "boot.journal.gz",
            "result": {
                "collection_id": "collection:imported-0001",
                "import_status": "imported",
                "records_written": 4,
                "records_skipped": 0,
                "server_only_key": "不该透出去",
            },
        },
        status=JobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=3,
        last_error=None,
        created_at=when,
        updated_at=when,
    )

    async with make_client(make_app(store=store, queue=queue)) as client:
        response = await client.get(f"/api/imports/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "succeeded"
    assert body["job"]["failure"] is None
    assert body["job"]["plugin_id"] == "journal.archive"
    assert body["job"]["result"] == {
        "collection_id": "collection:imported-0001",
        "import_status": "imported",
        "records_written": 4,
        "records_skipped": 0,
        "skipped_reason": None,
    }
    assert "server_only_key" not in body["job"]["result"]
    assert body["collection"]["collection_id"] == "collection:imported-0001"


async def test_import_status_without_result_has_no_collection() -> None:
    queue = FakeJobQueue()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    job_id = await queue.enqueue(SOURCE_IMPORT_JOB_KIND, {"plugin_id": "journal.archive"})
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=SOURCE_IMPORT_JOB_KIND,
        payload={"plugin_id": "journal.archive", "upload_id": "upload-2"},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        last_error=None,
        created_at=when,
        updated_at=when,
    )
    async with make_client(make_app(queue=queue)) as client:
        response = await client.get(f"/api/imports/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["result"] is None
    assert body["collection"] is None


async def test_upload_over_limit_is_rejected_and_partial_file_removed(upload_dir: Path) -> None:
    queue = FakeJobQueue()
    settings = Settings(upload_dir=str(upload_dir), max_upload_bytes=16)
    async with make_client(make_app(queue=queue, settings=settings)) as client:
        response = await client.post(
            "/api/imports",
            files={"file": ("big.journal.gz", b"x" * 64, "application/gzip")},
        )

    assert response.status_code == 413
    body = response.json()["error"]
    assert body["type"] == "invalid_format"
    assert "上限" in body["message"]
    assert queue.enqueued == []
    assert await asyncio.to_thread(_files, upload_dir) == [], "超限的上传不能留下半份文件"


async def test_import_status_does_not_expose_job_last_error() -> None:
    """任务行的 last_error 带异常类型与原文；状态接口只给处理器写回的失败分类。"""
    queue = FakeJobQueue()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    job_id = await queue.enqueue(SOURCE_IMPORT_JOB_KIND, {"plugin_id": "journal.archive"})
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=SOURCE_IMPORT_JOB_KIND,
        payload={
            "plugin_id": "journal.archive",
            "upload_id": "upload-3",
            "result": {
                "failure": {
                    "kind": "internal",
                    "message": "导入任务执行失败，具体原因见 Worker 日志",
                }
            },
        },
        status=JobStatus.FAILED,
        attempts=3,
        max_attempts=3,
        last_error="PluginExecutionError: /var/lib/sas/uploads/x.zip 打不开",
        created_at=when,
        updated_at=when,
    )

    async with make_client(make_app(queue=queue)) as client:
        response = await client.get(f"/api/imports/{job_id}")

    assert response.status_code == 200
    job = response.json()["job"]
    assert job["failure"] == {
        "kind": "internal",
        "message": "导入任务执行失败，具体原因见 Worker 日志",
    }
    assert "PluginExecutionError" not in response.text
    assert "/var/lib" not in response.text
