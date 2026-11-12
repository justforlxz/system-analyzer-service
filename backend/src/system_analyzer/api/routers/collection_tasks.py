"""采集任务的管理接口：创建、列表、状态查询与取消（任务 4.1）。

1.6 只写下了「任务由 API 写进 ``jobs`` 表」这条约定（见 ``collection/jobs.py`` 的模块说明），
对应的路由一直没人建；这一层把它补上。写库的两个动作（取消标记、按类型列任务）放在
``jobs/queue.py`` 里，路由里不出现 SQL。

路径用 ``/api/collection-tasks`` 而不是 ``/api/collections``：后者是 1.6 的「已导入的诊断采集」
列表，与「采集任务」是两件事，共用一个路径会让调用方分不清手里的是什么。

任务的状态只来自 ``jobs`` 表与 ``payload.result.collection``，不在这层重算：成功、失败、取消
都由 Worker 的编排判定并写回，界面只读结果。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query

from system_analyzer.api.deps import PageParams, get_job_queue, get_target_service, page_params
from system_analyzer.api.errors import conflict, not_found
from system_analyzer.api.schemas import (
    CollectionTaskRequest,
    CollectionTaskResponse,
    Page,
    collection_task_response,
)
from system_analyzer.collection.jobs import COLLECTION_JOB_KIND
from system_analyzer.jobs.models import JobStatus
from system_analyzer.jobs.queue import JobQueue, JobRecord
from system_analyzer.remote.errors import TargetNotFound
from system_analyzer.remote.service import SshTargetService

router = APIRouter(prefix="/collection-tasks", tags=["collection-tasks"])


@router.post(
    "",
    response_model=CollectionTaskResponse,
    status_code=202,
    summary="创建采集任务",
)
async def create_collection_task(
    body: CollectionTaskRequest,
    queue: JobQueue = Depends(get_job_queue),
    targets: SshTargetService = Depends(get_target_service),
) -> CollectionTaskResponse:
    """把一条 ``collect.target`` 任务写进队列并立刻返回；真正的采集由 Worker 执行。

    先查一遍目标机器：指向一台不存在的机器时任务必然失败，不如在提交时就报 404。
    ``run_id`` 缺省由服务端生成：它会拼进 Worker 上的临时目录名，不能交给调用方随便填
    （``collection/jobs.py`` 的 ``_run_id`` 只接受 UUID）。
    """
    try:
        await targets.get(body.target_id)
    except TargetNotFound as exc:
        raise not_found(f"目标机器 {body.target_id} 不存在，无法创建采集任务") from exc
    payload = {
        "target_id": body.target_id,
        "run_id": str(body.run_id) if body.run_id is not None else str(uuid4()),
    }
    job_id = await queue.enqueue(COLLECTION_JOB_KIND, payload)
    record = await queue.get(job_id)
    if record is None:
        # 刚写进去就被删掉（例如并发清理）：给一个一致的 404，而不是 500
        raise not_found(f"采集任务 {job_id} 不存在")
    return collection_task_response(record)


@router.get("", response_model=Page[CollectionTaskResponse], summary="采集任务列表")
async def list_collection_tasks(
    target_id: str | None = Query(None, description="只看这台目标机器的任务"),
    status: JobStatus | None = Query(None, description="只看这个状态的任务"),
    page: PageParams = Depends(page_params),
    queue: JobQueue = Depends(get_job_queue),
) -> Page[CollectionTaskResponse]:
    """按创建时间倒序列出采集任务，可按目标机器与任务状态筛选。"""
    records, total = await queue.list_by_kind(
        COLLECTION_JOB_KIND,
        target_id=target_id,
        status=status,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[CollectionTaskResponse](
        items=[collection_task_response(record) for record in records],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{task_id}", response_model=CollectionTaskResponse, summary="采集任务状态")
async def read_collection_task(
    task_id: UUID,
    queue: JobQueue = Depends(get_job_queue),
) -> CollectionTaskResponse:
    return collection_task_response(await _task_or_404(queue, task_id))


@router.post(
    "/{task_id}/cancel",
    response_model=CollectionTaskResponse,
    summary="取消采集任务",
)
async def cancel_collection_task(
    task_id: UUID,
    queue: JobQueue = Depends(get_job_queue),
) -> CollectionTaskResponse:
    """给任务行置上取消标记；只对排队中与执行中的任务有效。

    取消不会让任务立刻停下：Worker 在两个步骤之间读一次标记（见 ``collection/jobs.py``）。
    取消不算任务失败，任务的最终状态仍由 Worker 写回，界面按采集记录判断这次采集的结果。
    """
    updated = await queue.request_cancel(task_id, kind=COLLECTION_JOB_KIND)
    if updated is not None:
        return collection_task_response(updated)
    current = await _task_or_404(queue, task_id)
    raise conflict(
        f"采集任务 {task_id} 当前状态是 {current.status}，只有排队中或执行中的任务可以取消"
    )


async def _task_or_404(queue: JobQueue, task_id: UUID) -> JobRecord:
    """按 id 取一条采集任务；不存在、或者那个 id 是别的类型的任务时都报 404。"""
    record = await queue.get(task_id)
    if record is None or record.kind != COLLECTION_JOB_KIND:
        raise not_found(f"采集任务 {task_id} 不存在")
    return record
