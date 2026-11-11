"""把异步的 ``DiagnosticStore`` 包成导入插件要求的同步入口。

插件契约（``docs/plugin-contracts.md`` 第 3.3 节）把 ``import_source`` 定成同步方法，
``importers/bootrec_importer.py`` 的 ``CollectionImportAccess`` 因此也要求同步的
``get_collection`` 与 ``import_collection``；而 ``DiagnosticStore`` 的方法全是协程。

桥接方式是：Worker 把 ``registry.invoke(...)`` 放到线程里跑，线程里的每次存储层调用都开一条
短命事件循环；连接池用 ``NullPool``，连接用完就关，不会跨事件循环复用 asyncpg 连接
（asyncpg 的连接与创建它的事件循环绑定，跨循环复用会出错）。

一次导入任务里这样的调用只有一两次（先查采集号再整份写入），开销可以接受；换来的是
「插件永远只看到同步入口」，不会出现「调用成功、实际什么都没导入」的静默错配。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from system_analyzer.domain.bootrec import CollectionRef, ResourceAvailability
from system_analyzer.storage import CollectionState, DiagnosticStore, ImportOutcome


class SyncCollectionAccess(Protocol):
    """导入任务看到的同步数据入口。

    形状与 ``importers.bootrec_importer.CollectionImportAccess`` 一致，另外多一个
    ``last_outcome``：任务处理器要从这里取「写进了哪次采集」。测试可以用一个满足它的替身，
    不必连库。
    """

    last_outcome: ImportOutcome | None

    def get_collection(self, collection_id: str) -> CollectionState | None: ...

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Sequence[BaseModel],
        records: Sequence[Any],
        *,
        source_sha256: str | None = None,
        resources: ResourceAvailability | None = None,
    ) -> ImportOutcome: ...


class SyncStoreAccess:
    """导入插件的数据入口：同步方法，内部驱动异步存储层。

    ``last_outcome`` 记最近一次 ``import_collection`` 的结果。插件入口只返回
    ``ImportResult``（``docs/plugin-contracts.md`` 第 3.3 节没有采集号这一项），
    而任务结果要写出「写进了哪次采集」，所以在这里把存储层的返回留一份。
    """

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
        """在一条独立的事件循环里执行一次存储层调用。

        在事件循环线程里调用本方法会直接报错：那里的 ``asyncio.run`` 无法执行，
        硬跑会得到一个含义不明的运行时异常。这条路径只该由 Worker 的工作线程走。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("同步数据入口不能在事件循环线程里调用；请在 Worker 的工作线程里使用")

        async def body() -> Any:
            engine = create_async_engine(self._database_url, poolclass=NullPool)
            try:
                return await call(DiagnosticStore(engine))
            finally:
                await engine.dispose()

        return asyncio.run(body())
