"""机器、启动、采集与记录的查询接口。

全部是只读接口：不写库、不改导入状态、不做时间线聚合（时间线在 ``routers/timeline.py``）。
分页在 API 层切片，因为存储层（任务 1.1）没有提供分页参数；这一点写在
``docs/import-and-query-api.md`` 的已知限制里。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from system_analyzer.api.deps import (
    PageParams,
    WindowParams,
    get_store,
    page_params,
    paginate,
    parse_kinds,
    window_params,
)
from system_analyzer.api.errors import not_found
from system_analyzer.api.schemas import (
    BootResponse,
    CollectionResponse,
    MachineResponse,
    Page,
    RecordsPage,
    boot_response,
    collection_response,
    machine_response,
    record_response,
)
from system_analyzer.domain.bootrec import ClockDomain, RecordKind
from system_analyzer.storage import DiagnosticStore, ImportStatus

router = APIRouter(tags=["query"])

KIND_NAMES = tuple(kind.value for kind in RecordKind)


@router.get("/machines/{machine_id}", response_model=MachineResponse, summary="机器详情")
async def read_machine(
    machine_id: str,
    store: DiagnosticStore = Depends(get_store),
) -> MachineResponse:
    machine = await store.get_machine(machine_id)
    if machine is None:
        raise not_found(f"机器 {machine_id} 不在库里")
    return machine_response(machine)


@router.get(
    "/machines/{machine_id}/boots",
    response_model=Page[BootResponse],
    summary="一台机器的启动记录",
)
async def list_machine_boots(
    machine_id: str,
    page: PageParams = Depends(page_params),
    store: DiagnosticStore = Depends(get_store),
) -> Page[BootResponse]:
    if await store.get_machine(machine_id) is None:
        raise not_found(f"机器 {machine_id} 不在库里")
    boots = [boot_response(boot) for boot in await store.list_boots(machine_id)]
    items, total = paginate(boots, page)
    return Page[BootResponse](items=items, total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/machines/{machine_id}/boots/{boot_id}",
    response_model=BootResponse,
    summary="启动详情",
)
async def read_boot(
    machine_id: str,
    boot_id: str,
    store: DiagnosticStore = Depends(get_store),
) -> BootResponse:
    boot = await store.get_boot(machine_id, boot_id)
    if boot is None:
        raise not_found(f"启动 {machine_id}/{boot_id} 不在库里")
    return boot_response(boot)


@router.get("/collections", response_model=Page[CollectionResponse], summary="采集记录列表")
async def list_collections(
    machine_id: str | None = Query(None, description="只看这台机器"),
    boot_id: str | None = Query(None, description="只看这次启动"),
    status: ImportStatus | None = Query(None, description="只看这个导入状态"),
    page: PageParams = Depends(page_params),
    store: DiagnosticStore = Depends(get_store),
) -> Page[CollectionResponse]:
    states = await store.list_collections(machine_id=machine_id, boot_id=boot_id, status=status)
    collections = [collection_response(state) for state in states]
    items, total = paginate(collections, page)
    return Page[CollectionResponse](items=items, total=total, limit=page.limit, offset=page.offset)


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
    summary="采集详情",
)
async def read_collection(
    collection_id: str,
    store: DiagnosticStore = Depends(get_store),
) -> CollectionResponse:
    state = await store.get_collection(collection_id)
    if state is None:
        raise not_found(f"采集 {collection_id} 不在库里")
    return collection_response(state)


@router.get(
    "/collections/{collection_id}/records",
    response_model=RecordsPage,
    summary="一次采集里的记录",
)
async def list_collection_records(
    collection_id: str,
    clock_domain: ClockDomain = Query(
        ClockDomain.BOOT_MONOTONIC,
        description="按哪个时钟域排序与筛选；两种时钟域的零点不同，不能混排",
    ),
    kinds: str | None = Query(
        None,
        description="逗号分隔的记录种类，例如 event,metric；不传表示全部",
    ),
    window: WindowParams = Depends(window_params),
    page: PageParams = Depends(page_params),
    store: DiagnosticStore = Depends(get_store),
) -> RecordsPage:
    state = await store.get_collection(collection_id)
    if state is None:
        raise not_found(f"采集 {collection_id} 不在库里")
    wanted = parse_kinds(kinds, KIND_NAMES)
    # 先取总数再决定要不要查记录：存储层没有 OFFSET 参数，页码要靠 ``LIMIT offset+limit`` 模拟，
    # 越界的页码（offset 超过这次采集的总条数）直接给空页，不发出那条大 LIMIT 查询。
    total = await store.count_records(collection_id)
    if page.offset >= total:
        return RecordsPage(items=[], limit=page.limit, offset=page.offset, collection_total=total)
    records = await store.list_records_in_collection(
        collection_id,
        clock_domain=clock_domain,
        start_ns=window.start_ns,
        end_ns=window.end_ns,
        kinds=None if wanted is None else [RecordKind(name) for name in wanted],
        limit=page.offset + page.limit,
    )
    items = [record_response(record) for record in records[page.offset :]]
    return RecordsPage(
        items=items,
        limit=page.limit,
        offset=page.offset,
        collection_total=total,
    )
