"""FastAPI 应用工厂。"""

from fastapi import FastAPI

from system_analyzer import __version__
from system_analyzer.api.routers import health
from system_analyzer.core.config import get_settings
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.registry import get_registry


def create_app() -> FastAPI:
    """构造 API 应用。

    插件注册只在这里做一次，Worker 启动时调用同一个 ``register_builtins``，
    两处共享 ``plugins.builtins.BUILTIN_PLUGINS`` 这一份清单。
    """
    settings = get_settings()
    app = FastAPI(
        title="system-analyzer-service",
        version=__version__,
        description="Linux 远程诊断与启动过程分析平台 API",
    )
    app.state.settings = settings
    app.state.plugins = register_builtins(get_registry())
    app.include_router(health.router, prefix="/api")
    return app
