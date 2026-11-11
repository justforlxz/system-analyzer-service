"""API 的请求依赖：数据库、时间线引擎、任务队列、插件注册表与公共查询参数。

数据库引擎在第一次用到时才创建，并挂在 ``app.state`` 上：构造应用与导出 OpenAPI 都不需要
连库，只有真正处理请求才连。应用退出时由 ``app.py`` 的 lifespan 释放连接池。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import Query, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from system_analyzer.api.errors import invalid_parameter
from system_analyzer.core.config import Settings
from system_analyzer.core.db import create_engine
from system_analyzer.jobs.queue import JobQueue
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import DiagnosticStore
from system_analyzer.timeline import TimelineEngine

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MAX_PAGE_OFFSET = 100_000
"""``offset`` 的上界。

采集内记录的分页要把 ``offset + limit`` 当成 SQL ``LIMIT`` 下推（存储层没有 OFFSET 参数），
上界让最坏情况最多取回 100200 行；不设上界时 ``offset=100000000`` 会把整次采集搬进内存。
"""


def app_settings(request: Request) -> Settings:
    """本次应用使用的配置（``create_app`` 已经读好并挂在 ``app.state`` 上）。"""
    return request.app.state.settings


def get_engine(request: Request) -> AsyncEngine:
    """进程内共用的异步引擎，第一次用到时创建。"""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        engine = create_engine(request.app.state.settings.database_url)
        request.app.state.engine = engine
    return engine


def get_store(request: Request) -> DiagnosticStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = DiagnosticStore(get_engine(request))
        request.app.state.store = store
    return store


def get_timeline(request: Request) -> TimelineEngine:
    """时间线引擎。读侧只经由 ``DiagnosticStore``，与任务 1.5 的接线一致。"""
    engine = getattr(request.app.state, "timeline", None)
    if engine is None:
        engine = TimelineEngine(get_store(request))
        request.app.state.timeline = engine
    return engine


def get_job_queue(request: Request) -> JobQueue:
    queue = getattr(request.app.state, "job_queue", None)
    if queue is None:
        queue = JobQueue(
            get_engine(request),
            lock_timeout_seconds=request.app.state.settings.worker_lock_timeout_seconds,
        )
        request.app.state.job_queue = queue
    return queue


def get_plugins(request: Request) -> PluginRegistry:
    """本进程已经注册过的插件清单（``create_app`` 启动时注册一次）。"""
    return request.app.state.plugins


@dataclass(frozen=True, slots=True)
class PageParams:
    """分页参数。``limit`` 与 ``offset`` 都有上界，避免一次查询把整张表搬到内存里。"""

    limit: int
    offset: int


def page_params(
    limit: int = Query(
        DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description=f"每页条数，1..{MAX_PAGE_LIMIT}",
    ),
    offset: int = Query(
        0,
        ge=0,
        le=MAX_PAGE_OFFSET,
        description=f"跳过的条数，0..{MAX_PAGE_OFFSET}",
    ),
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


def paginate[T](items: Sequence[T], params: PageParams) -> tuple[list[T], int]:
    """按分页参数切一段，返回（本页内容，筛选后的总条数）。"""
    return list(items[params.offset : params.offset + params.limit]), len(items)


@dataclass(frozen=True, slots=True)
class WindowParams:
    """时间范围参数。闭区间，两端都可以缺省表示无界。"""

    start_ns: int | None
    end_ns: int | None


def window_params(
    start_ns: int | None = Query(None, ge=0, description="起始时刻（含），单位纳秒"),
    end_ns: int | None = Query(None, ge=0, description="结束时刻（含），单位纳秒"),
) -> WindowParams:
    """时间范围参数，起点晚于终点时直接报参数错误，不做静默交换。"""
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise invalid_parameter(f"时间范围起点 {start_ns} 晚于终点 {end_ns}，空区间不查询")
    return WindowParams(start_ns=start_ns, end_ns=end_ns)


def parse_kinds(value: str | None, allowed: Sequence[str]) -> list[str] | None:
    """把 ``kinds=event,span`` 这样的逗号分隔参数拆成白名单里的取值。

    不认识的取值直接报参数错误并列出允许的取值，不静默忽略：调用方写错种类时，
    返回一个少了几条轨道的结果比报错更难查。
    """
    if value is None or not value.strip():
        return None
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise invalid_parameter(
            f"不认识的记录种类：{'、'.join(unknown)}",
            details=[f"允许的取值：{'、'.join(allowed)}"],
        )
    # 保持顺序去重，调用方传入的顺序不影响结果
    return list(dict.fromkeys(names))
