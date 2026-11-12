"""FastAPI 应用工厂。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from system_analyzer import __version__
from system_analyzer.api.errors import install_error_handlers
from system_analyzer.api.routers import (
    collection_tasks,
    health,
    imports,
    query,
    targets,
    timeline,
)
from system_analyzer.core.config import get_settings
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.registry import get_registry


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程退出时释放连接池。

    引擎与存储对象都是第一次用到请求时才创建的（构造应用与导出 OpenAPI 不连库），
    所以这里只释放「已经创建过」的那一个。
    """
    yield
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        await engine.dispose()
        app.state.engine = None
        app.state.store = None
        app.state.timeline = None
        app.state.job_queue = None


def create_app() -> FastAPI:
    """构造 API 应用。

    插件注册只在这里做一次，Worker 启动时调用同一个 ``register_builtins``，
    两处共享 ``plugins.builtins.BUILTIN_PLUGINS`` 这一份清单。
    全部失败响应由 ``install_error_handlers`` 收敛成统一错误结构。
    """
    settings = get_settings()
    app = FastAPI(
        title="system-analyzer-service",
        version=__version__,
        description="Linux 远程诊断与启动过程分析平台 API",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.plugins = register_builtins(get_registry())
    install_error_handlers(app)
    app.include_router(health.router, prefix="/api")
    app.include_router(query.router, prefix="/api")
    app.include_router(timeline.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    app.include_router(targets.router, prefix="/api")
    app.include_router(collection_tasks.router, prefix="/api")
    return app
