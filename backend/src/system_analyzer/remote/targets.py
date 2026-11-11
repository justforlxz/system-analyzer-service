"""目标机器的领域模型与仓储：新增、修改、删除、列表、查询。

``SshTarget`` 是对外暴露的形状——API 响应、日志、任务结果都直接用它。它没有能装口令的字段，
所以「输出里出现明文口令」在结构上就不可能发生：口令只以 ``password_env_var``（变量名）出现。

``SshTargetRepository`` 只管读写 ``ssh_targets`` 表，不做校验之外的业务判断：连接测试在
``service.py``，凭据解析在 ``credentials.py``。
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from system_analyzer.core.db import create_session_factory
from system_analyzer.remote.credentials import PASSWORD_ENV_VAR
from system_analyzer.remote.errors import TargetNotFound
from system_analyzer.remote.models import SshTargetRow

ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
"""环境变量名的形状：大写字母开头，只含大写字母、数字与下划线。"""


class SshTargetDraft(BaseModel):
    """新增或修改一台目标机器时提交的内容（不含 id 与时间戳）。

    修改是整体替换：提交什么就是什么，字段缺省值会生效。这样调用方不会遇到「只改了一个字段，
    其它字段被隐式保留成旧值」的意外。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1, max_length=255, description="主机名或 IP 地址")
    username: str = Field(min_length=1, max_length=64, description="SSH 用户名")
    port: int = Field(default=22, ge=1, le=65535, description="SSH 端口")
    password_env_var: str = Field(
        default=PASSWORD_ENV_VAR,
        min_length=1,
        max_length=128,
        description="存放 SSH 口令的环境变量名；口令本身不入库",
    )

    @field_validator("host", "username")
    @classmethod
    def _no_surrounding_spaces(cls, value: str) -> str:
        """去掉首尾空白；去掉后为空说明只填了空格，直接拒绝。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空或只包含空白字符")
        return stripped

    @field_validator("password_env_var")
    @classmethod
    def _valid_env_var(cls, value: str) -> str:
        """引用必须是合法的环境变量名，否则运行期必然解析失败，不如在写入时拒绝。"""
        if not ENV_VAR_NAME_PATTERN.match(value):
            raise ValueError(
                f"password_env_var 必须是环境变量名（大写字母、数字、下划线，且以字母开头），"
                f"实际是 {value!r}"
            )
        return value


class SshTarget(SshTargetDraft):
    """一台已保存的目标机器。``target_id`` 由仓储生成，创建后不变。"""

    target_id: str
    created_at: datetime
    updated_at: datetime


class SshTargetRepository:
    """``ssh_targets`` 表的读写。每次调用用一个短会话，提交后不复用。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, draft: SshTargetDraft) -> SshTarget:
        """写入一台新机器，返回带 id 与时间戳的记录。"""
        row = SshTargetRow(
            target_id=uuid4().hex,
            host=draft.host,
            port=draft.port,
            username=draft.username,
            password_env_var=draft.password_env_var,
        )
        async with create_session_factory(self._engine)() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_model(row)

    async def update(self, target_id: str, draft: SshTargetDraft) -> SshTarget:
        """整体替换一台机器的可改字段。记录不存在时抛 ``TargetNotFound``。"""
        async with create_session_factory(self._engine)() as session:
            result = await session.execute(
                update(SshTargetRow)
                .where(SshTargetRow.target_id == target_id)
                .values(
                    host=draft.host,
                    port=draft.port,
                    username=draft.username,
                    password_env_var=draft.password_env_var,
                    updated_at=func.now(),
                )
                .returning(SshTargetRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise TargetNotFound(f"目标机器不存在：{target_id}")
            await session.commit()
            return _to_model(row)

    async def delete(self, target_id: str) -> None:
        """删除一台机器。记录不存在时抛 ``TargetNotFound``，不静默成功。"""
        async with create_session_factory(self._engine)() as session:
            result = await session.execute(
                delete(SshTargetRow)
                .where(SshTargetRow.target_id == target_id)
                .returning(SshTargetRow.target_id)
            )
            if result.scalar_one_or_none() is None:
                raise TargetNotFound(f"目标机器不存在：{target_id}")
            await session.commit()

    async def get(self, target_id: str) -> SshTarget | None:
        """按 id 取一台机器，没有时返回 ``None``。"""
        async with create_session_factory(self._engine)() as session:
            row = await session.get(SshTargetRow, target_id)
            return None if row is None else _to_model(row)

    async def list(self) -> list[SshTarget]:
        """按创建时间列出全部机器。"""
        async with create_session_factory(self._engine)() as session:
            rows = (
                await session.execute(
                    select(SshTargetRow).order_by(SshTargetRow.created_at, SshTargetRow.target_id)
                )
            ).scalars()
            return [_to_model(row) for row in rows]


def _to_model(row: SshTargetRow) -> SshTarget:
    """把表行转成对外模型。逐字段显式映射，避免将来加列时把敏感字段顺手带出去。"""
    return SshTarget(
        target_id=row.target_id,
        host=row.host,
        port=row.port,
        username=row.username,
        password_env_var=row.password_env_var,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
