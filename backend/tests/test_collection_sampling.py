"""In Progress 阶段调用资源与 Journal 采集插件的测试。

采集器是同步插件，编排是异步的。这里用真的插件注册表与假插件验证：两个采集器被调用时
拿到同一套 machine / boot / collection 标识，产物落在本次采集的临时目录里，并且数据库
连接串只经插件配置传入，不写进采集记录。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from collection_fakes import (
    LIFECYCLE_BOOT_ID,
    FakeConnector,
    FakeJournalCollector,
    FakeResourceCollector,
)
from system_analyzer.collection import sampling
from system_analyzer.collection.context import PhaseContext
from system_analyzer.core.config import get_settings
from system_analyzer.plugins.registry import PluginNotRegisteredError, PluginRegistry

TARGET = "uos-152"
MACHINE_ID = f"machine:{TARGET}"
COLLECTION_ID = "collection:run-sampling"
RUN_ID = "run-sampling"


def make_context(
    tmp_path: Path,
    *,
    options: dict[str, Any] | None = None,
    system_info: dict[str, Any] | None = None,
) -> PhaseContext:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return PhaseContext(
        target_id=TARGET,
        run_id=RUN_ID,
        attempt=1,
        options=options if options is not None else {"delivery": {}},
        scratch_dir=scratch,
        data={
            "system_info": system_info
            if system_info is not None
            else {"hostname": TARGET, "boot_id": LIFECYCLE_BOOT_ID}
        },
        connector=FakeConnector({TARGET: {}}),
    )


@pytest.fixture
def fake_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PluginRegistry, FakeResourceCollector, FakeJournalCollector]:
    registry = PluginRegistry()
    resources = FakeResourceCollector()
    journal = FakeJournalCollector()
    registry.register(resources)
    registry.register(journal)
    monkeypatch.setattr(sampling, "get_registry", lambda: registry)
    return registry, resources, journal


async def test_collect_continuously_records_resources_and_journal(
    tmp_path: Path, fake_plugins: tuple[PluginRegistry, FakeResourceCollector, FakeJournalCollector]
) -> None:
    _registry, resources, journal = fake_plugins
    ctx = make_context(tmp_path)

    await sampling.start_sampling(ctx)
    await sampling.collect_continuously(ctx)

    state = ctx.data["sampling"]
    assert state["status"] == "completed"
    assert state["collection_id"] == COLLECTION_ID
    assert state["machine_id"] == MACHINE_ID
    assert state["boot_id"] == LIFECYCLE_BOOT_ID

    records_file = Path(state["records_file"])
    rows = [
        json.loads(line)
        for line in records_file.read_text(encoding="utf-8").splitlines()  # noqa: ASYNC240
        if line
    ]
    assert rows
    assert {row["collection_id"] for row in rows} == {COLLECTION_ID}
    assert {row["machine_id"] for row in rows} == {MACHINE_ID}
    assert {row["boot_id"] for row in rows} == {LIFECYCLE_BOOT_ID}

    assert resources.requests[0].target_id == TARGET
    assert resources.requests[0].options["collection_id"] == COLLECTION_ID
    assert resources.requests[0].options["machine_id"] == MACHINE_ID
    assert resources.requests[0].options["boot_id"] == LIFECYCLE_BOOT_ID
    database_url = str(get_settings().database_url)
    assert resources.configs[0] == {"database_url": database_url}
    assert journal.requests[0].options["collection_id"] == COLLECTION_ID
    assert journal.requests[0].options["database_url"] == database_url

    export = Path(ctx.data["journal"]["export_file"])
    assert export.is_file()  # noqa: ASYNC240  测试断言
    assert ctx.data["journal"]["work_dir"] == str(ctx.scratch_dir / "journal")
    assert "database_url" not in json.dumps(ctx.data, ensure_ascii=False)
    assert "database_url" not in ctx.options


async def test_collect_continuously_is_skipped_without_delivery(
    tmp_path: Path, fake_plugins: tuple[PluginRegistry, FakeResourceCollector, FakeJournalCollector]
) -> None:
    _registry, resources, journal = fake_plugins
    ctx = make_context(tmp_path, options={"backup_paths": []})

    await sampling.start_sampling(ctx)
    await sampling.collect_continuously(ctx)

    assert ctx.data["sampling"]["status"] == "skipped"
    assert resources.requests == []
    assert journal.requests == []


async def test_collect_continuously_accepts_a_relative_scratch_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_plugins: tuple[PluginRegistry, FakeResourceCollector, FakeJournalCollector],
) -> None:
    """Worker 的默认临时目录是相对路径，采集器返回的相对产物也必须解析一次。"""
    monkeypatch.chdir(tmp_path)
    scratch = Path("scratch")
    scratch.mkdir()  # noqa: ASYNC240  测试准备
    ctx = PhaseContext(
        target_id=TARGET,
        run_id=RUN_ID,
        attempt=1,
        options={"delivery": {}},
        scratch_dir=scratch,
        data={"system_info": {"hostname": TARGET, "boot_id": LIFECYCLE_BOOT_ID}},
        connector=FakeConnector({TARGET: {}}),
    )

    await sampling.start_sampling(ctx)
    await sampling.collect_continuously(ctx)

    export = Path(ctx.data["journal"]["export_file"])
    assert export.is_absolute()
    assert export.is_file()  # noqa: ASYNC240  测试断言
    assert scratch.resolve() / "journal" in export.parents  # noqa: ASYNC240  测试断言
    assert Path(ctx.data["sampling"]["records_file"]).is_file()  # noqa: ASYNC240  测试断言


async def test_collect_continuously_reports_missing_boot_id(tmp_path: Path) -> None:
    ctx = make_context(tmp_path, system_info={"hostname": TARGET})

    with pytest.raises(sampling.SamplingError, match="boot_id"):
        await sampling.start_sampling(ctx)


async def test_collect_continuously_reports_missing_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sampling, "get_registry", PluginRegistry)
    ctx = make_context(tmp_path)

    await sampling.start_sampling(ctx)
    with pytest.raises(PluginNotRegisteredError, match="resource-sampler"):
        await sampling.collect_continuously(ctx)


async def test_phase_timeout_waits_for_blocking_collector_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阶段超时后必须等工作线程结束，不能让旧采样与 Post 或重试同时写临时目录。"""

    class SlowResourceCollector(FakeResourceCollector):
        def __init__(self, delay: float) -> None:
            super().__init__()
            self.delay = delay
            self.finished = threading.Event()

        def collect(self, request: Any, context: Any) -> Any:
            time.sleep(self.delay)
            self.finished.set()
            return super().collect(request, context)

    registry = PluginRegistry()
    resources = SlowResourceCollector(delay=0.3)
    registry.register(resources)
    registry.register(FakeJournalCollector())
    monkeypatch.setattr(sampling, "get_registry", lambda: registry)
    ctx = make_context(tmp_path)
    await sampling.start_sampling(ctx)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sampling.collect_continuously(ctx), timeout=0.05)
    elapsed = time.monotonic() - started

    assert resources.finished.is_set()
    assert elapsed >= 0.25
    assert not (ctx.scratch_dir / sampling.RECORDS_FILE_NAME).exists()
