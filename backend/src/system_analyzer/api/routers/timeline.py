"""时间线查询与回放接口。

数据全部来自任务 1.5 的 ``TimelineEngine``：本接口层只负责参数校验、调用与序列化，
不在这一层重算时间、合并轨道或补默认值。三种轨道状态（``available`` / ``unavailable`` /
``no_data``）与开放区间的收尾规则都由引擎决定，序列化时原样透出。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from system_analyzer.api.deps import WindowParams, get_timeline, parse_kinds, window_params
from system_analyzer.api.schemas import (
    CursorResponse,
    TimelineStateResponse,
    TracksResponse,
    cursor_response,
    state_response,
    tracks_response,
)
from system_analyzer.timeline import TIMELINE_KINDS, TimelineEngine

router = APIRouter(prefix="/machines/{machine_id}/boots/{boot_id}/timeline", tags=["timeline"])

TIMELINE_KIND_NAMES = tuple(kind.value for kind in TIMELINE_KINDS)


@router.get("/tracks", response_model=TracksResponse, summary="时间范围里的轨道与条目")
async def read_tracks(
    machine_id: str,
    boot_id: str,
    kinds: str | None = Query(
        None,
        description="逗号分隔的记录种类，只能取 event、span、metric；不传表示全部",
    ),
    window: WindowParams = Depends(window_params),
    engine: TimelineEngine = Depends(get_timeline),
) -> TracksResponse:
    wanted = parse_kinds(kinds, TIMELINE_KIND_NAMES)
    tracks = await engine.tracks(
        machine_id,
        boot_id,
        start_ns=window.start_ns,
        end_ns=window.end_ns,
        kinds=wanted,
    )
    return tracks_response(tracks)


@router.get("/state", response_model=TimelineStateResponse, summary="指定时刻的轨道状态")
async def read_state(
    machine_id: str,
    boot_id: str,
    monotonic_ns: int = Query(..., ge=0, description="boot 内时刻，单位纳秒"),
    engine: TimelineEngine = Depends(get_timeline),
) -> TimelineStateResponse:
    state = await engine.state_at(machine_id, boot_id, monotonic_ns)
    return state_response(state)


@router.get("/cursor", response_model=CursorResponse, summary="统一时间游标")
async def read_cursor(
    machine_id: str,
    boot_id: str,
    at_ns: int | None = Query(None, ge=0, description="游标位置；不传停在轴起点"),
    forward: bool | None = Query(
        None,
        description="给出布尔值时先按 at_ns 定位，再挪到前（false）或后（true）一个可停靠时刻",
    ),
    engine: TimelineEngine = Depends(get_timeline),
) -> CursorResponse:
    cursor = await engine.cursor(machine_id, boot_id, at_ns=at_ns)
    if forward is not None:
        cursor = await engine.step_cursor(cursor, forward=forward)
    return cursor_response(cursor)
