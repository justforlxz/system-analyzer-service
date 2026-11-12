"""服务诊断视图接口（任务 4.3）。

只读接口：按游标给出一个服务当时的状态、身份信息、关联 Journal、资源采样与分析结论。
数据来自 ``ServiceDiagnosticsEngine``；这里只校验参数并把诊断错误翻译成统一错误结构。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from system_analyzer.api.deps import get_store
from system_analyzer.api.diagnostic_schemas import (
    ServiceDiagnosticsResponse,
    ServiceListResponse,
    service_diagnostics_response,
    service_list_response,
)
from system_analyzer.api.errors import not_found
from system_analyzer.diagnostics import (
    DiagnosticsBootNotFoundError,
    DiagnosticsServiceNotFoundError,
    ServiceDiagnosticsEngine,
)
from system_analyzer.diagnostics.engine import DEFAULT_JOURNAL_WINDOW_NS
from system_analyzer.storage import DiagnosticStore

router = APIRouter(
    prefix="/machines/{machine_id}/boots/{boot_id}/diagnostics", tags=["diagnostics"]
)

MAX_JOURNAL_WINDOW_NS = 3_600_000_000_000


@router.get("/services", response_model=ServiceListResponse, summary="这次启动里的服务列表")
async def list_services(
    machine_id: str,
    boot_id: str,
    store: DiagnosticStore = Depends(get_store),
) -> ServiceListResponse:
    try:
        services = await ServiceDiagnosticsEngine(store).services(machine_id, boot_id)
    except DiagnosticsBootNotFoundError as exc:
        raise not_found(str(exc)) from exc
    return service_list_response(services)


@router.get(
    "/services/{entity_id}",
    response_model=ServiceDiagnosticsResponse,
    summary="指定时刻的服务状态与诊断证据",
)
async def read_service_diagnostics(
    machine_id: str,
    boot_id: str,
    entity_id: str,
    at_ns: int | None = Query(None, ge=0, description="时间游标（boot 内纳秒）；不传用最早时刻"),
    journal_window_ns: int = Query(
        DEFAULT_JOURNAL_WINDOW_NS,
        ge=1,
        le=MAX_JOURNAL_WINDOW_NS,
        description="关联 Journal 在游标前后各取多长时间，单位纳秒",
    ),
    store: DiagnosticStore = Depends(get_store),
) -> ServiceDiagnosticsResponse:
    try:
        value = await ServiceDiagnosticsEngine(store).service_at(
            machine_id,
            boot_id,
            entity_id,
            at_ns=at_ns,
            journal_window_ns=journal_window_ns,
        )
    except (DiagnosticsBootNotFoundError, DiagnosticsServiceNotFoundError) as exc:
        raise not_found(str(exc)) from exc
    return service_diagnostics_response(value)
