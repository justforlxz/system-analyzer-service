"""采集任务管理接口的单元测试：创建、列表、状态查询、取消与失败原因。

任务队列与目标机器服务都用替身：SQL 部分（``JobQueue.list_by_kind`` 与
``request_cancel``）由 ``tests/integration/test_collection_task_jobs_db.py`` 在真实
PostgreSQL 上验证，这里只验证 HTTP 层的行为与响应形状。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from api_fakes import FakeJobQueue, make_app, make_client
from system_analyzer.api.deps import get_job_queue, get_target_service
from system_analyzer.collection.jobs import COLLECTION_JOB_KIND
from system_analyzer.jobs.models import SOURCE_IMPORT_JOB_KIND, JobStatus
from system_analyzer.jobs.queue import JobRecord
from system_analyzer.remote.errors import TargetNotFound

_WHEN = datetime(2026, 1, 1, tzinfo=UTC)


class FakeTargets:
    """只需要 ``get``：创建任务前确认目标机器存在。"""

    def __init__(self) -> None:
        self.known: set[str] = set()

    async def get(self, target_id: str):
        if target_id not in self.known:
            raise TargetNotFound(f"目标机器不存在：{target_id}")
        return object()


def task_client(queue: FakeJobQueue, targets: FakeTargets) -> httpx.AsyncClient:
    app = make_app(queue=queue)
    app.dependency_overrides[get_job_queue] = lambda: queue
    app.dependency_overrides[get_target_service] = lambda: targets
    return make_client(app)


def collection_record(
    *,
    run_id: str,
    target_id: str,
    attempt_status: str = "failed",
    phase_status: str = "failed",
    detail: str | None = "步骤 collect.cpu 失败：远端命令以退出码 1 结束",
) -> dict:
    """一份最小可用的采集记录，形状取自 ``collection/record.py`` 的 ``as_mapping``。"""
    return {
        "run_id": run_id,
        "target_id": target_id,
        "attempts": [
            {
                "attempt": 1,
                "status": attempt_status,
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:10+00:00",
                "resumed_from": None,
                "phases": [
                    {
                        "phase": "in_progress",
                        "status": phase_status,
                        "started_at": "2026-01-01T00:00:01+00:00",
                        "finished_at": "2026-01-01T00:00:09+00:00",
                        "detail": detail,
                    }
                ],
                "events": [
                    {
                        "at": "2026-01-01T00:00:09+00:00",
                        "kind": "phase_failed",
                        "phase": "in_progress",
                        "step": "collect.cpu",
                        "detail": detail,
                    }
                ],
                "data": {},
            }
        ],
    }


async def test_create_task_enqueues_a_collect_job() -> None:
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    async with task_client(queue, targets) as client:
        response = await client.post("/api/collection-tasks", json={"target_id": "target:1"})

    assert response.status_code == 202
    body = response.json()
    assert body["target_id"] == "target:1"
    assert body["status"] == "queued"
    assert body["cancel_requested"] is False
    assert body["latest_attempt"] is None
    assert body["failure"] is None
    UUID(body["run_id"])  # 缺省由服务端生成，且必须是 UUID（会拼进 Worker 的临时目录名）
    kind, payload = queue.enqueued[0]
    assert kind == COLLECTION_JOB_KIND
    assert payload == {"target_id": "target:1", "run_id": body["run_id"], "options": {}}


async def test_create_task_passes_collection_options_to_the_worker() -> None:
    """LightDM 自动登录与回传都靠 options 打开，接口不能把它们丢掉。"""
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    options = {
        "backup_paths": ["/etc/systemd/system.conf"],
        "desktop_user": "uos",
        "delivery": {"remote_path": "/var/tmp/collection.bootrec"},
    }
    async with task_client(queue, targets) as client:
        response = await client.post(
            "/api/collection-tasks", json={"target_id": "target:1", "options": options}
        )

    assert response.status_code == 202
    assert queue.enqueued[0][1]["options"] == options


async def test_create_task_drops_worker_internal_options() -> None:
    """``options`` 只放行调用方可以设置的键，Worker 内部键一律丢弃。

    ``database_url`` 会让 Worker 连接调用方指定的数据库，``artifacts`` 会让打包读取 Worker
    主机上的任意文件；这类键不能从接口进入任务 payload。
    """
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    options = {
        "backup_paths": ["/etc/systemd/system.conf"],
        "desktop_user": "uos",
        "desktop_login_timeout": 30,
        "phase_timeouts": {"in_progress": 60},
        "delivery": {
            "remote_path": "/var/tmp/collection.bootrec",
            "stage_local_pack": True,
            "retry_delay_seconds": 0.5,
            "download_attempts": 3,
            "database_url": "postgresql+asyncpg://attacker@203.0.113.9/leak",
            "collection_id": "collection:attacker",
            "journal_export": "/etc/shadow",
            "journal_work_dir": "/etc",
            "artifacts": [{"source": "/etc/shadow", "path": "artifacts/shadow"}],
        },
        "database_url": "postgresql+asyncpg://attacker@203.0.113.9/leak",
        "collection_id": "collection:attacker",
        "machine_id": "machine:attacker",
        "boot_id": "attacker",
        "journal_work_dir": "/etc",
        "work_dir": "/etc",
    }
    async with task_client(queue, targets) as client:
        response = await client.post(
            "/api/collection-tasks", json={"target_id": "target:1", "options": options}
        )

    assert response.status_code == 202
    assert queue.enqueued[0][1]["options"] == {
        "backup_paths": ["/etc/systemd/system.conf"],
        "desktop_user": "uos",
        "desktop_login_timeout": 30,
        "phase_timeouts": {"in_progress": 60},
        "delivery": {
            "remote_path": "/var/tmp/collection.bootrec",
            "stage_local_pack": True,
            "retry_delay_seconds": 0.5,
            "download_attempts": 3,
        },
    }


async def test_create_task_keeps_a_supplied_run_id() -> None:
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    run_id = str(uuid4())
    async with task_client(queue, targets) as client:
        response = await client.post(
            "/api/collection-tasks", json={"target_id": "target:1", "run_id": run_id}
        )

    assert response.json()["run_id"] == run_id
    assert queue.enqueued[0][1]["run_id"] == run_id


@pytest.mark.parametrize("run_id", ["abc", "../escape"])
async def test_create_task_rejects_a_non_uuid_run_id(run_id: str) -> None:
    """``run_id`` 会拼进 Worker 的临时目录名，非 UUID 必须在接口层拒绝，不能写进任务。"""
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    async with task_client(queue, targets) as client:
        response = await client.post(
            "/api/collection-tasks", json={"target_id": "target:1", "run_id": run_id}
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "invalid_parameter"
    assert any("run_id" in detail for detail in error["details"])
    assert queue.enqueued == []


async def test_create_task_for_unknown_target_is_not_found() -> None:
    queue = FakeJobQueue()
    async with task_client(queue, FakeTargets()) as client:
        response = await client.post("/api/collection-tasks", json={"target_id": "target:missing"})

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"
    assert queue.enqueued == []


async def test_create_task_rejects_empty_target_id() -> None:
    queue = FakeJobQueue()
    async with task_client(queue, FakeTargets()) as client:
        response = await client.post("/api/collection-tasks", json={"target_id": ""})

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_parameter"


async def test_list_tasks_returns_a_page_and_honours_filters() -> None:
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.update({"target:1", "target:2"})
    async with task_client(queue, targets) as client:
        for target_id in ("target:1", "target:1", "target:2"):
            await client.post("/api/collection-tasks", json={"target_id": target_id})
        everything = await client.get("/api/collection-tasks")
        only_first = await client.get("/api/collection-tasks?target_id=target:1")
        only_queued = await client.get("/api/collection-tasks?status=queued")
        nothing = await client.get("/api/collection-tasks?status=succeeded")

    assert everything.status_code == 200
    assert everything.json()["total"] == 3
    assert [item["target_id"] for item in only_first.json()["items"]] == ["target:1", "target:1"]
    assert only_first.json()["total"] == 2
    assert only_queued.json()["total"] == 3
    assert nothing.json()["items"] == []
    assert nothing.json()["total"] == 0


async def test_read_task_returns_the_same_shape_as_create() -> None:
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    async with task_client(queue, targets) as client:
        created = (
            await client.post("/api/collection-tasks", json={"target_id": "target:1"})
        ).json()
        response = await client.get(f"/api/collection-tasks/{created['task_id']}")

    assert response.status_code == 200
    assert response.json() == created


async def test_read_task_unknown_is_not_found() -> None:
    async with task_client(FakeJobQueue(), FakeTargets()) as client:
        response = await client.get(f"/api/collection-tasks/{uuid4()}")

    assert response.status_code == 404


async def test_read_import_job_as_collection_task_is_not_found() -> None:
    """别的类型的任务不冒充采集任务：``import.source`` 的 id 查不到采集任务。"""
    queue = FakeJobQueue()
    job_id = uuid4()
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=SOURCE_IMPORT_JOB_KIND,
        payload={},
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=3,
        last_error=None,
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    async with task_client(queue, FakeTargets()) as client:
        response = await client.get(f"/api/collection-tasks/{job_id}")

    assert response.status_code == 404


async def test_cancel_queued_task_sets_the_flag() -> None:
    queue = FakeJobQueue()
    targets = FakeTargets()
    targets.known.add("target:1")
    async with task_client(queue, targets) as client:
        created = (
            await client.post("/api/collection-tasks", json={"target_id": "target:1"})
        ).json()
        response = await client.post(f"/api/collection-tasks/{created['task_id']}/cancel")

    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True
    assert queue.jobs[UUID(created["task_id"])].payload["cancel_requested"] is True


async def test_cancel_finished_task_is_conflict() -> None:
    queue = FakeJobQueue()
    job_id = uuid4()
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=COLLECTION_JOB_KIND,
        payload={"target_id": "target:1"},
        status=JobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=3,
        last_error=None,
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    async with task_client(queue, FakeTargets()) as client:
        response = await client.post(f"/api/collection-tasks/{job_id}/cancel")

    assert response.status_code == 409
    error = response.json()["error"]
    assert "只有排队中或执行中的任务可以取消" in error["message"]


async def test_cancel_unknown_task_is_not_found() -> None:
    async with task_client(FakeJobQueue(), FakeTargets()) as client:
        response = await client.post(f"/api/collection-tasks/{uuid4()}/cancel")

    assert response.status_code == 404


async def test_failure_reason_comes_from_the_phase_detail() -> None:
    """界面显示的失败原因取编排写下的阶段说明，不是 ``last_error`` 里的异常原文。"""
    queue = FakeJobQueue()
    job_id = uuid4()
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=COLLECTION_JOB_KIND,
        payload={
            "target_id": "target:1",
            "run_id": "run-1",
            "result": {
                "collection": collection_record(run_id="run-1", target_id="target:1"),
            },
        },
        status=JobStatus.FAILED,
        attempts=3,
        max_attempts=3,
        last_error="CollectionJobFailed: 步骤 collect.cpu 失败：远端命令以退出码 1 结束\n"
        "  File '/srv/app/collection/orchestrator.py', line 1",
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    async with task_client(queue, FakeTargets()) as client:
        response = await client.get(f"/api/collection-tasks/{job_id}")

    body = response.json()
    assert body["failure"] == {
        "phase": "in_progress",
        "message": "步骤 collect.cpu 失败：远端命令以退出码 1 结束",
    }
    assert body["latest_attempt"]["status"] == "failed"
    assert [phase["phase"] for phase in body["latest_attempt"]["phases"]] == ["in_progress"]
    assert body["latest_attempt"]["events"][0]["step"] == "collect.cpu"
    # 异常原文与文件路径只留在 Worker 日志里，不透给调用方
    assert "/srv/app" not in response.text
    assert "last_error" not in body


async def test_failure_without_a_record_is_still_readable() -> None:
    """任务在进入编排之前就失败时，也要给一句能看懂的原因，而不是只有「失败」两字。"""
    queue = FakeJobQueue()
    job_id = uuid4()
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=COLLECTION_JOB_KIND,
        payload={"target_id": "target:1"},
        status=JobStatus.FAILED,
        attempts=3,
        max_attempts=3,
        last_error="ValueError: payload.run_id 必须是 UUID",
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    async with task_client(queue, FakeTargets()) as client:
        response = await client.get(f"/api/collection-tasks/{job_id}")

    body = response.json()
    assert body["failure"] == {
        "phase": None,
        "message": "任务执行失败，采集记录里没有写下失败原因；详细原因见 Worker 日志",
    }
    assert body["latest_attempt"] is None


@pytest.mark.parametrize("attempt_status", ["cancelled", "succeeded"])
async def test_cancelled_or_succeeded_task_has_no_failure(attempt_status: str) -> None:
    """取消与成功都不是失败：取消会执行清理阶段，与失败要分开显示。"""
    queue = FakeJobQueue()
    job_id = uuid4()
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=COLLECTION_JOB_KIND,
        payload={
            "target_id": "target:1",
            "run_id": "run-1",
            "cancel_requested": True,
            "result": {
                "collection": collection_record(
                    run_id="run-1",
                    target_id="target:1",
                    attempt_status=attempt_status,
                    phase_status=attempt_status,
                    detail=None,
                )
            },
        },
        status=JobStatus.SUCCEEDED,
        attempts=1,
        max_attempts=3,
        last_error=None,
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    async with task_client(queue, FakeTargets()) as client:
        response = await client.get(f"/api/collection-tasks/{job_id}")

    body = response.json()
    assert body["failure"] is None
    assert body["latest_attempt"]["status"] == attempt_status
    assert body["cancel_requested"] is True


async def test_running_task_without_result_has_no_failure() -> None:
    queue = FakeJobQueue()
    job_id = uuid4()
    queue.jobs[job_id] = JobRecord(
        id=job_id,
        kind=COLLECTION_JOB_KIND,
        payload={"target_id": "target:1"},
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        last_error=None,
        created_at=_WHEN,
        updated_at=_WHEN,
    )
    async with task_client(queue, FakeTargets()) as client:
        response = await client.get(f"/api/collection-tasks/{job_id}")
        listed = await client.get("/api/collection-tasks")

    assert response.json()["status"] == "running"
    assert response.json()["failure"] is None
    assert listed.json()["total"] == 1
    assert queue.jobs[job_id].status is JobStatus.RUNNING
