"""Alembic 运行环境。

连接串取自 ``Settings.database_url``（环境变量 ``SAS_DATABASE_URL``），
迁移目录与表结构一起进仓库，任何环境都靠 ``alembic upgrade head`` 建表。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# SSH 目标机器表（任务 3.1）与诊断数据共用一份 MetaData，模型声明在 remote/models.py。
# 只导入、不使用：没有这一行，autogenerate 会把它当成「数据库里多出来的表」生成删表操作。
from system_analyzer.core.config import get_settings
from system_analyzer.remote import models as _remote_models  # noqa: F401
from system_analyzer.storage.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 声明式模型在 system_analyzer.storage.models，表结构与迁移在这里对照：
# ``alembic revision --autogenerate`` 与 ``alembic check`` 都依赖这份 MetaData。
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式，只生成 SQL。"""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式，使用异步引擎执行迁移。"""
    engine = create_async_engine(get_settings().database_url, future=True)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
