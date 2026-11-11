"""数据库连接与会话构造。

只暴露连接与工厂，不承载业务规则；具体读写放在 ``storage`` 与 ``jobs``。
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """按连接串创建异步引擎。"""
    return create_async_engine(database_url, pool_pre_ping=True, future=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建会话工厂，提交后不过期对象，便于调用方继续读取属性。"""
    return async_sessionmaker(engine, expire_on_commit=False)
