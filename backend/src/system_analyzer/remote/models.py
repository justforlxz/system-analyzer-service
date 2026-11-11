"""SSH 目标机器记录的表结构（任务 3.1）。

这是「要连哪台机器、用哪个账号、口令从哪个环境变量取」的配置记录，与诊断数据里的
``machines`` 表（``docs/data-model.md`` 第 5.1 节，被诊断对象的身份，由采集数据写入）不是
一回事：``machines`` 是采集结果，本表是运维配置。因此单独建表 ``ssh_targets``，不复用
``machines``，也不在 ``storage/models.py`` 里加列。

口令不在这张表里：``password_env_var`` 只存**环境变量名**（引用），值是运行环境的事，
见 ``remote/credentials.py``。表结构因此没有任何能装下明文口令的列，写错代码也不会把口令落库。

``Base`` 与诊断数据共用一份 ``MetaData``，这样 ``alembic revision --autogenerate`` 与
``tests/integration/test_diagnostic_store.py`` 的「迁移与模型一致」检查都能覆盖到本表；
``migrations/env.py`` 为此显式导入本模块。

迁移文件：``backend/migrations/versions/0003_ssh_targets.py``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from system_analyzer.storage.models import Base


class SshTargetRow(Base):
    """一台可被远程采集的目标机器。"""

    __tablename__ = "ssh_targets"

    target_id: Mapped[str] = mapped_column(Text, primary_key=True)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("22"))
    username: Mapped[str] = mapped_column(Text, nullable=False)
    # 只存环境变量名，不存口令；见 remote/credentials.py
    password_env_var: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("port BETWEEN 1 AND 65535", name="ssh_targets_port_check"),
        CheckConstraint("host <> ''", name="ssh_targets_host_check"),
        CheckConstraint("username <> ''", name="ssh_targets_username_check"),
        CheckConstraint("password_env_var <> ''", name="ssh_targets_password_env_var_check"),
    )
