"""同步数据入口（``api/sync_store.py``）的单元测试。

真实写库要 PostgreSQL，见 ``tests/integration/test_api_import_timeline.py``；这里只验证
「这条入口不能在事件循环线程里被调用」这条防线，它挡住的是「在协程里 ``asyncio.run``」这种
含义不明的失败。
"""

from __future__ import annotations

import pytest

from system_analyzer.api.sync_store import SyncStoreAccess

DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"


async def test_calling_from_event_loop_thread_is_refused() -> None:
    access = SyncStoreAccess(DATABASE_URL)

    with pytest.raises(RuntimeError, match="事件循环线程"):
        access.get_collection("collection:any")

    with pytest.raises(RuntimeError, match="事件循环线程"):
        access.import_collection(None, [], [])  # type: ignore[arg-type]


def test_last_outcome_starts_empty() -> None:
    assert SyncStoreAccess(DATABASE_URL).last_outcome is None
