"""PostgreSQL 读写与仓储实现。

表结构由 ``backend/migrations`` 下的 Alembic 修订维护，本包对应任务 1.1（诊断数据存储与核心
领域模型）。三个模块的分工：

- ``models``：五张表的声明式模型，以及导入状态的取值与允许的迁移。
- ``validation``：写入前校验与完整性检查，纯函数、不访问数据库。
- ``store``：``DiagnosticStore`` 的写入与查询接口。

字段、类型、缺失语义以 ``docs/data-model.md`` 为准；时间字段与排序键以 ``docs/time-base.md``
为准。本包不解析 ``.bootrec``（1.3）、不做时间线聚合与游标（1.5）、不提供 HTTP 接口（1.6）。
"""

from system_analyzer.storage.models import Base, ImportStatus
from system_analyzer.storage.store import (
    INTERNAL_IMPORT_FAILURE_MESSAGE,
    CollectionState,
    DiagnosticStore,
    ImportOutcome,
    StorageError,
    StorageStateError,
    StorageValidationError,
)

__all__ = [
    "INTERNAL_IMPORT_FAILURE_MESSAGE",
    "Base",
    "CollectionState",
    "DiagnosticStore",
    "ImportOutcome",
    "ImportStatus",
    "StorageError",
    "StorageStateError",
    "StorageValidationError",
]
