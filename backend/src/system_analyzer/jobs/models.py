"""任务状态的取值与失败后的去向。

「哪个状态可以迁移到哪个状态」由 ``jobs`` 表的写入条件与数据库的
``jobs_status_check`` 约束保证（见 ``jobs/queue.py`` 与迁移 ``0001_jobs``）。
这里只保留纯计算：任务失败后应该回到队列，还是进入失败终态。
"""

from enum import StrEnum


class JobStatus(StrEnum):
    """后台任务状态，与 ``jobs.status`` 列的取值一一对应。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def status_after_failure(attempts: int, max_attempts: int) -> JobStatus:
    """任务执行失败后的下一个状态。

    还有重试次数就回到队列，否则进入失败终态。``attempts`` 是本次执行前已执行的次数，
    调用方在领取任务时已经把它加一。
    """
    if attempts < max_attempts:
        return JobStatus.QUEUED
    return JobStatus.FAILED
