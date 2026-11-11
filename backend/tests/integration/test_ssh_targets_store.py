"""目标机器持久化的集成测试，需要真实 PostgreSQL。

运行方式见 ``docs/implementation-baseline.md``：先起库并执行 ``alembic upgrade head``，再执行
``uv run pytest -m integration``。测试库地址取自 ``SAS_TEST_DATABASE_URL``，未配置时直接报错。

这里验证三件事：仓储在真实库上的增删改查、迁移 ``0003_ssh_targets`` 与模型一致、
以及同步入口（``SyncSshTargetService``）在真实库 + 真实 SSH 连接上可用。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ssh_fakes import TEST_PASSWORD, fake_ssh_server
from system_analyzer.core.config import Settings
from system_analyzer.remote.credentials import PASSWORD_ENV_VAR
from system_analyzer.remote.errors import TargetNotFound
from system_analyzer.remote.models import SshTargetRow  # noqa: F401  让 Base.metadata 含 ssh_targets
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.sync import SyncSshTargetService
from system_analyzer.remote.targets import SshTargetDraft, SshTargetRepository
from system_analyzer.storage.models import Base

pytestmark = pytest.mark.integration

SYSTEM_INFO_OUTPUT = """hostname=uos-PC
os_release=UOS Desktop 25 Professional
kernel_version=6.6.0-amd64-desktop
architecture=x86_64
cpu_count=16
memory_total_bytes=16732954624
boot_id=3f0b3e0e-2a55-4e1e-9d3f-6f0f0e5a1b2c
"""


async def _ssh_probe_handler(command: str) -> tuple[str, str, int]:
    """假服务端对连接测试脚本的回复。"""
    return SYSTEM_INFO_OUTPUT, "", 0


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """每个用例从空表开始，避免上一条用例的数据被查出来。"""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM ssh_targets"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def repository(engine: AsyncEngine) -> SshTargetRepository:
    return SshTargetRepository(engine)


def draft(**overrides: Any) -> SshTargetDraft:
    values: dict[str, Any] = {"host": "10.20.50.152", "username": "uos"}
    values.update(overrides)
    return SshTargetDraft(**values)


# ---------------------------------------------------------------------------
# 增删改查
# ---------------------------------------------------------------------------


async def test_create_persists_the_target(repository: SshTargetRepository) -> None:
    created = await repository.create(draft())

    loaded = await repository.get(created.target_id)

    assert loaded is not None
    assert loaded.host == "10.20.50.152"
    assert loaded.port == 22
    assert loaded.username == "uos"
    assert loaded.password_env_var == PASSWORD_ENV_VAR
    assert loaded.created_at.tzinfo is not None


async def test_list_is_ordered_by_creation(repository: SshTargetRepository) -> None:
    first = await repository.create(draft(host="10.0.0.1"))
    second = await repository.create(draft(host="10.0.0.2"))

    listed = await repository.list()

    assert [target.target_id for target in listed] == [first.target_id, second.target_id]


async def test_update_replaces_the_editable_fields(repository: SshTargetRepository) -> None:
    created = await repository.create(draft())

    updated = await repository.update(
        created.target_id,
        draft(host="10.20.50.153", username="root", port=2222),
    )

    assert updated.target_id == created.target_id
    assert updated.host == "10.20.50.153"
    assert updated.username == "root"
    assert updated.port == 2222
    assert updated.updated_at >= created.updated_at

    reloaded = await repository.get(created.target_id)
    assert reloaded is not None
    assert reloaded.host == "10.20.50.153"


async def test_update_missing_target_raises(repository: SshTargetRepository) -> None:
    with pytest.raises(TargetNotFound):
        await repository.update("nope", draft())


async def test_delete_removes_the_row(repository: SshTargetRepository) -> None:
    created = await repository.create(draft())

    await repository.delete(created.target_id)

    assert await repository.get(created.target_id) is None


async def test_delete_missing_target_raises(repository: SshTargetRepository) -> None:
    with pytest.raises(TargetNotFound):
        await repository.delete("nope")


async def test_database_rejects_an_out_of_range_port(engine: AsyncEngine) -> None:
    """模型校验之外还有数据库约束：绕过模型直接写也写不进非法端口。"""
    async with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO ssh_targets (target_id, host, port, username, password_env_var)"
                    " VALUES ('x', 'h', 0, 'u', 'SAS_SSH_PASSWORD')"
                )
            )


async def test_table_has_no_column_for_a_plaintext_password(engine: AsyncEngine) -> None:
    """表结构本身不存口令：只有 password_env_var（变量名）。"""
    async with engine.begin() as conn:
        columns = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'ssh_targets'"
                )
            )
        ).scalars()
        names = set(columns)

    assert "password_env_var" in names
    assert "password" not in names
    assert "private_key" not in names


def _only_ssh_targets(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """只对照 ssh_targets 这一张表：别的表（jobs、诊断数据）各有自己的对照用例。"""
    if type_ == "table":
        return name == "ssh_targets"
    table = getattr(obj, "table", None)
    return getattr(table, "name", None) == "ssh_targets"


async def test_migration_matches_models(engine: AsyncEngine) -> None:
    def diff(sync_connection: Any) -> list[Any]:
        context = MigrationContext.configure(
            sync_connection, opts={"include_object": _only_ssh_targets}
        )
        return compare_metadata(context, Base.metadata)

    async with engine.connect() as connection:
        assert await connection.run_sync(diff) == []


# ---------------------------------------------------------------------------
# 同步入口（3.4 的接缝）
# ---------------------------------------------------------------------------


async def test_sync_service_crud_runs_in_a_worker_thread(test_database_url: str) -> None:
    """同步入口只在 Worker 的工作线程里用，这里用线程模拟那一侧。"""
    service = SyncSshTargetService(
        test_database_url,
        settings=Settings(database_url=test_database_url, ssh_password=TEST_PASSWORD),
    )

    created = await asyncio.to_thread(service.create_target, draft(host="10.20.50.199"))
    listed = await asyncio.to_thread(service.list_targets)
    loaded = await asyncio.to_thread(service.get_target, created.target_id)
    await asyncio.to_thread(service.delete_target, created.target_id)
    remaining = await asyncio.to_thread(service.list_targets)

    assert [target.target_id for target in listed] == [created.target_id]
    assert loaded.host == "10.20.50.199"
    assert remaining == []


async def test_sync_service_connection_test_works_end_to_end(
    test_database_url: str, tmp_path: Path
) -> None:
    """同步入口 + 真实数据库 + 真实 SSH 协议栈：连接测试返回系统基本信息。"""
    service = SyncSshTargetService(
        test_database_url,
        settings=Settings(database_url=test_database_url, ssh_password=TEST_PASSWORD),
    )

    async with fake_ssh_server(tmp_path, handler=_ssh_probe_handler) as server:
        created = await asyncio.to_thread(
            service.create_target, draft(host="127.0.0.1", port=server.port)
        )

        info = await asyncio.to_thread(service.test_connection, created.target_id)

    assert info.hostname == "uos-PC"
    assert info.cpu_count == 16
    assert info.kernel_version == "6.6.0-amd64-desktop"


async def test_async_service_connection_test_works_end_to_end(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """异步服务层同样对着假服务端跑一遍，确保两套入口行为一致。"""
    repository = SshTargetRepository(engine)
    service = SshTargetService(
        repository,
        settings=Settings(database_url="postgresql+asyncpg://unused", ssh_password=TEST_PASSWORD),
    )

    async with fake_ssh_server(tmp_path, handler=_ssh_probe_handler) as server:
        created = await service.create(draft(host="127.0.0.1", port=server.port))
        info = await service.test_connection(created.target_id)

    assert info.hostname == "uos-PC"
    assert info.memory_total_bytes == 16732954624
