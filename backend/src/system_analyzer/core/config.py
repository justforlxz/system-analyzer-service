"""运行配置。

所有配置项都从环境变量读取，前缀 ``SAS_``，本地开发时由 ``backend/.env`` 提供
（``.env`` 不进仓库，仓库里只有 ``.env.example``）。凭据只通过环境变量传入，
代码与仓库不保存任何密码。
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """后端进程的配置。字段缺失时启动直接失败，不做静默兜底。"""

    model_config = SettingsConfigDict(
        env_prefix="SAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        min_length=1,
        description="PostgreSQL 异步连接串，密码只通过环境变量注入，不写进仓库",
    )
    log_level: str = Field(default="INFO", description="日志级别")
    api_host: str = Field(default="127.0.0.1", description="API 监听地址")
    api_port: int = Field(default=8000, description="API 监听端口")

    worker_id: str = Field(default="worker-1", description="Worker 实例标识，写入任务锁定字段")
    worker_poll_interval_seconds: float = Field(
        default=2.0,
        gt=0,
        description="任务队列为空时的轮询间隔",
    )
    worker_lock_timeout_seconds: int = Field(
        default=900,
        gt=0,
        description="任务锁超时时间，超时后任务可被其他 Worker 重新领取",
    )


@lru_cache
def get_settings() -> Settings:
    """读取并缓存配置。测试里改环境变量后需调用 ``get_settings.cache_clear()``。"""
    return Settings()
