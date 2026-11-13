"""采集回传后的同步导入入口。

1.3 的 ``BootrecImporter`` 是同步插件，需要同步的 ``get_collection`` / ``import_collection``；
1.1 的 ``DiagnosticStore`` 是协程。Worker 里的导入任务用 ``api/sync_store.py`` 做这份桥接，
本模块给采集回传流程提供同样的桥接，但放在采集层，避免 ``collection`` 依赖 ``api``：

- 每次调用开一条短命事件循环与一条短命连接（``NullPool``），用完即关；
- 不能在事件循环线程里调用，实际由 ``asyncio.to_thread`` 放进工作线程执行。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.domain.bootrec import CollectionRef, ResourceAvailability
from system_analyzer.importers.bootrec_importer import BootrecImportOutcome, import_bootrec
from system_analyzer.storage import CollectionState, DiagnosticStore, ImportOutcome


class SynchronousCollectionAccess:
    """把异步存储层包成导入插件要求的同步入口，并留下最近一次写入结果。"""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self.last_outcome: ImportOutcome | None = None

    def get_collection(self, collection_id: str) -> CollectionState | None:
        return self._run(lambda store: store.get_collection(collection_id))

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Sequence[BaseModel],
        records: Sequence[Any],
        *,
        source_sha256: str | None = None,
        resources: ResourceAvailability | None = None,
    ) -> ImportOutcome:
        outcome = self._run(
            lambda store: store.import_collection(
                collection,
                entities,
                records,
                source_sha256=source_sha256,
                resources=resources,
            )
        )
        self.last_outcome = outcome
        return outcome

    def _run(self, call: Callable[[DiagnosticStore], Coroutine[Any, Any, Any]]) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("同步导入入口不能在事件循环线程里调用；请用 asyncio.to_thread")

        async def body() -> Any:
            engine = create_async_engine(self._database_url, poolclass=NullPool)
            try:
                return await call(DiagnosticStore(engine))
            finally:
                await engine.dispose()

        return asyncio.run(body())


def import_bootrec_file(path: str, database_url: str) -> BootrecImportOutcome:
    """同步导入一份 ``.bootrec``，返回导入结果。"""
    return import_bootrec(SynchronousCollectionAccess(database_url), path)


__all__ = ["SynchronousCollectionAccess", "import_bootrec_file"]
