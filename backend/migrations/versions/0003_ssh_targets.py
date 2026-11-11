"""建立 SSH 目标机器表：ssh_targets

表结构声明在 ``system_analyzer/remote/models.py``，两边由
``tests/integration/test_ssh_targets_store.py::test_migration_matches_models`` 对照。
本表只存「连哪台机器、用哪个账号、口令从哪个环境变量取」，不存口令本身。

Revision ID: 0003_ssh_targets
Revises: 0002_diagnostic_data
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_ssh_targets"
down_revision: str | None = "0002_diagnostic_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ssh_targets",
        sa.Column("target_id", sa.Text(), primary_key=True),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.BigInteger(), nullable=False, server_default=sa.text("22")),
        sa.Column("username", sa.Text(), nullable=False),
        # 只存环境变量名（引用），口令值永远不入库
        sa.Column("password_env_var", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ssh_targets_port_check"),
        sa.CheckConstraint("host <> ''", name="ssh_targets_host_check"),
        sa.CheckConstraint("username <> ''", name="ssh_targets_username_check"),
        sa.CheckConstraint("password_env_var <> ''", name="ssh_targets_password_env_var_check"),
    )


def downgrade() -> None:
    op.drop_table("ssh_targets")
