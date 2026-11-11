"""建立诊断数据表：machines、boots、collections、entities、records

字段与判定规则见 ``docs/data-model.md``（第 3、4、5 节）与 ``docs/time-base.md``
（第 2 节时钟域、第 7 节排序键）。表结构声明在 ``system_analyzer/storage/models.py``，
两边由 ``tests/integration/test_diagnostic_store.py::test_migration_matches_models`` 对照。

Revision ID: 0002_diagnostic_data
Revises: 0001_jobs
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_diagnostic_data"
down_revision: str | None = "0001_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "machines",
        sa.Column("machine_id", sa.Text(), primary_key=True),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("os_release", sa.Text(), nullable=True),
        sa.Column("kernel_version", sa.Text(), nullable=True),
        sa.Column("architecture", sa.Text(), nullable=True),
    )

    op.create_table(
        "boots",
        sa.Column(
            "machine_id",
            sa.Text(),
            sa.ForeignKey("machines.machine_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("boot_id", sa.Text(), primary_key=True),
        sa.Column("started_realtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("kernel_version", sa.Text(), nullable=True),
        sa.Column("boot_index", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "collections",
        sa.Column("collection_id", sa.Text(), primary_key=True),
        sa.Column("machine_id", sa.Text(), nullable=False),
        sa.Column("boot_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("created_realtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("import_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("import_error", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.Text(), nullable=True),
        sa.Column("resources_available", sa.Boolean(), nullable=True),
        sa.Column("resources_reason", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["machine_id", "boot_id"],
            ["boots.machine_id", "boots.boot_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "mode IN ('ssh_collection', 'journal_import', 'bootrec_import')",
            name="collections_mode_check",
        ),
        sa.CheckConstraint(
            "import_status IN ('pending', 'importing', 'imported', 'failed')",
            name="collections_import_status_check",
        ),
        sa.CheckConstraint(
            "resources_available IS NULL"
            " OR (resources_available AND resources_reason IS NULL)"
            " OR (NOT resources_available AND resources_reason IS NOT NULL)",
            name="collections_resources_reason_check",
        ),
    )
    op.create_index("collections_boot_idx", "collections", ["machine_id", "boot_id"])
    op.create_index("collections_digest_idx", "collections", ["machine_id", "source_sha256"])

    op.create_table(
        "entities",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column(
            "machine_id",
            sa.Text(),
            sa.ForeignKey("machines.machine_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("boot_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("available", sa.Boolean(), nullable=True),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "machine_id",
            "boot_id",
            "entity_id",
            name="entities_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "entity_type IN ('service', 'process', 'user_session', 'cgroup', 'metric_stream')",
            name="entities_type_check",
        ),
        sa.CheckConstraint(
            "(entity_type = 'metric_stream' AND available IS NOT NULL"
            " AND name IS NOT NULL AND unit IS NOT NULL)"
            " OR (entity_type <> 'metric_stream'"
            " AND available IS NULL AND unavailable_reason IS NULL)",
            name="entities_metric_stream_columns_check",
        ),
        sa.CheckConstraint(
            "available IS NULL"
            " OR (available AND unavailable_reason IS NULL)"
            " OR (NOT available AND unavailable_reason IS NOT NULL)",
            name="entities_metric_stream_reason_check",
        ),
    )

    op.create_table(
        "records",
        sa.Column(
            "collection_id",
            sa.Text(),
            sa.ForeignKey("collections.collection_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("record_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("machine_id", sa.Text(), nullable=False),
        sa.Column("boot_id", sa.Text(), nullable=False),
        sa.Column("source_plugin", sa.Text(), nullable=False),
        sa.Column("stream_id", sa.Text(), nullable=False),
        sa.Column("monotonic_ns", sa.BigInteger(), nullable=True),
        sa.Column("realtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("clock_domain", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "extra", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.ForeignKeyConstraint(
            ["machine_id", "boot_id"],
            ["boots.machine_id", "boots.boot_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('event', 'span', 'metric', 'profile', 'annotation', 'artifact')",
            name="records_kind_check",
        ),
        sa.CheckConstraint(
            "clock_domain IN ('boot_monotonic', 'wall_realtime')",
            name="records_clock_domain_check",
        ),
        sa.CheckConstraint(
            "monotonic_ns IS NOT NULL OR realtime_ns IS NOT NULL",
            name="records_timestamp_check",
        ),
        sa.CheckConstraint(
            "(clock_domain = 'boot_monotonic') = (monotonic_ns IS NOT NULL)",
            name="records_clock_domain_matches_monotonic_check",
        ),
    )
    op.create_index(
        "records_boot_order_idx",
        "records",
        ["machine_id", "boot_id", "monotonic_ns", "collection_id", "record_id"],
    )
    op.create_index(
        "records_boot_wall_idx",
        "records",
        ["machine_id", "boot_id", "realtime_ns", "collection_id", "record_id"],
    )
    op.create_index(
        "records_collection_order_idx", "records", ["collection_id", "monotonic_ns", "record_id"]
    )
    op.create_index(
        "records_entity_idx", "records", ["machine_id", "boot_id", "entity", "monotonic_ns"]
    )


def downgrade() -> None:
    op.drop_index("records_entity_idx", table_name="records")
    op.drop_index("records_collection_order_idx", table_name="records")
    op.drop_index("records_boot_wall_idx", table_name="records")
    op.drop_index("records_boot_order_idx", table_name="records")
    op.drop_table("records")
    op.drop_table("entities")
    op.drop_index("collections_digest_idx", table_name="collections")
    op.drop_index("collections_boot_idx", table_name="collections")
    op.drop_table("collections")
    op.drop_table("boots")
    op.drop_table("machines")
