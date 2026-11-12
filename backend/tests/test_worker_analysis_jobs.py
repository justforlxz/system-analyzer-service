"""分析任务处理器的单元测试：分析器选择、请求参数与结果写回。

不连数据库：数据入口与分析器都用替身。真实链路（导入 → 分析 → 入库）在集成测试里。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from system_analyzer.jobs.models import ANALYSIS_JOB_KIND
from system_analyzer.jobs.queue import Job
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    AnalysisRequest,
    AnalysisResult,
    PluginBase,
    PluginContext,
    PluginInputError,
    PluginKind,
    PluginSpec,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.worker.analysis_jobs import (
    SyncAnalysisAccess,
    analysis_handlers,
    run_analysis_job,
)

MACHINE = "machine:uos-PC"
BOOT = "boot-a"


class RecordingAnalyzer(PluginBase):
    def __init__(self, plugin_id: str, written: int = 2) -> None:
        self.spec = PluginSpec(
            id=plugin_id,
            kind=PluginKind.ANALYZER,
            name="记录请求的分析器",
            version="0.1.0",
            api_version=PLUGIN_API_VERSION,
            capabilities=("sample.analyze",),
        )
        self.written = written
        self.requests: list[AnalysisRequest] = []

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        self.requests.append(request)
        return AnalysisResult(annotations_written=self.written)


class RejectingAnalyzer(PluginBase):
    spec = PluginSpec(
        id="sample.rejecting",
        kind=PluginKind.ANALYZER,
        name="拒绝输入的分析器",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.analyze",),
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        raise PluginInputError("缺 boot_id")


class RecordingQueue:
    def __init__(self) -> None:
        self.results: list[tuple[Any, dict[str, Any]]] = []

    async def record_result(self, job_id: Any, result: dict[str, Any]) -> None:
        self.results.append((job_id, dict(result)))


class UnusedAccess:
    """分析器不访问数据入口时用它占位。"""


def make_job(payload: dict[str, Any]) -> Job:
    return Job(id=uuid4(), kind=ANALYSIS_JOB_KIND, payload=payload, attempts=1)


async def test_analysis_job_runs_selected_analyzer_with_machine_and_boot() -> None:
    analyzer = RecordingAnalyzer("sample.recording", written=3)
    registry = PluginRegistry()
    registry.register(analyzer)
    queue = RecordingQueue()
    job = make_job({"machine_id": MACHINE, "boot_id": BOOT, "analyzers": [analyzer.spec.id]})

    await run_analysis_job(
        job,
        registry=registry,
        queue=queue,
        access_factory=UnusedAccess,
    )

    assert analyzer.requests == [AnalysisRequest(boot_id=BOOT, options={"machine_id": MACHINE})]
    assert queue.results == [(job.id, {"analyzers": {analyzer.spec.id: 3}})]


async def test_analysis_job_records_invalid_input_classification() -> None:
    registry = PluginRegistry()
    registry.register(RejectingAnalyzer())
    queue = RecordingQueue()
    job = make_job({"machine_id": MACHINE, "boot_id": BOOT, "analyzers": ["sample.rejecting"]})

    with pytest.raises(PluginInputError):
        await run_analysis_job(
            job,
            registry=registry,
            queue=queue,
            access_factory=UnusedAccess,
        )

    assert queue.results == [
        (job.id, {"failure": {"kind": "invalid_input", "message": "缺 boot_id"}})
    ]


def test_analysis_handlers_registers_the_analysis_job_kind() -> None:
    handlers = analysis_handlers()
    assert set(handlers) == {ANALYSIS_JOB_KIND}
    assert callable(handlers[ANALYSIS_JOB_KIND])


def test_sync_analysis_access_batches_records_by_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PluginDataAccess`` 的批量写入按记录自带的 collection_id 分组。"""
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    class Outcome:
        records_written = 1

    class Store:
        async def append_records(self, collection_id: str, records: Any) -> Outcome:
            calls.append((collection_id, list(records)))
            return Outcome()

    access = SyncAnalysisAccess("postgresql+asyncpg://unused", MACHINE, BOOT)
    monkeypatch.setattr(access, "_run", lambda call: asyncio.run(call(Store())))

    written = access.append_records(
        [
            {"collection_id": "c1", "record_id": "r1"},
            {"collection_id": "c2", "record_id": "r2"},
        ],
        plugin_id="sample",
        source="run-1",
    )

    assert written == 2
    assert calls == [
        ("c1", [{"collection_id": "c1", "record_id": "r1"}]),
        ("c2", [{"collection_id": "c2", "record_id": "r2"}]),
    ]
