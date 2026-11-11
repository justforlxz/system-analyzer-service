"""查询接口的分页与时间范围边界测试，以及时间线接口的接线测试。

不连数据库：边界规则（起止相等、空区间、范围完全在数据之外、分页越界）都在接口层与引擎里，
用 ``tests/api_fakes.py`` 的替身就能断言。真实 PostgreSQL 上的同族断言在
``tests/integration/test_api_import_timeline.py``。
"""

from __future__ import annotations

from api_fakes import (
    FakeStore,
    boot,
    collection,
    event,
    machine,
    make_app,
    make_client,
    metric,
    stream,
)
from system_analyzer.storage import ImportStatus

S = 1_000_000_000
"""一秒的纳秒数，用例里的时刻都用它做单位。"""


def _store() -> FakeStore:
    """一台机器、三次启动、两次采集、四个记录（三个事件 + 一个指标点）。"""
    store = FakeStore(machines={"machine:uos-PC": machine()})
    for index in (1, 2, 3):
        item = boot(f"boot:{index}", boot_index=index)
        store.boots[("machine:uos-PC", item.boot_id)] = item
    store.collections["collection:a1"] = collection("collection:a1", boot_id="boot:1")
    store.collections["collection:a2"] = collection(
        "collection:a2", boot_id="boot:1", status=ImportStatus.PENDING
    )
    store.records = [
        event(1, 1 * S, collection_id="collection:a1", boot_id="boot:1"),
        event(2, 2 * S, collection_id="collection:a1", boot_id="boot:1"),
        event(3, 3 * S, collection_id="collection:a1", boot_id="boot:1"),
        metric(4, 2 * S, collection_id="collection:a1", boot_id="boot:1"),
    ]
    store.streams[("machine:uos-PC", "boot:1")] = [stream(boot_id="boot:1")]
    return store


async def test_machine_and_boot_detail() -> None:
    async with make_client(make_app(store=_store())) as client:
        machine_response = await client.get("/api/machines/machine:uos-PC")
        boot_response = await client.get("/api/machines/machine:uos-PC/boots/boot:2")

    assert machine_response.status_code == 200
    assert machine_response.json()["hostname"] == "uos-PC"
    assert boot_response.status_code == 200
    assert boot_response.json()["boot_id"] == "boot:2"
    assert boot_response.json()["boot_index"] == 2


async def test_boots_pagination_boundaries() -> None:
    async with make_client(make_app(store=_store())) as client:
        first_page = await client.get("/api/machines/machine:uos-PC/boots?limit=2&offset=0")
        last_page = await client.get("/api/machines/machine:uos-PC/boots?limit=2&offset=2")
        beyond = await client.get("/api/machines/machine:uos-PC/boots?offset=100")

    assert first_page.status_code == 200
    assert [item["boot_id"] for item in first_page.json()["items"]] == ["boot:1", "boot:2"]
    assert first_page.json()["total"] == 3
    assert len(last_page.json()["items"]) == 1
    assert last_page.json()["items"][0]["boot_id"] == "boot:3"
    assert beyond.json()["items"] == []
    assert beyond.json()["total"] == 3


async def test_collection_filters_and_pagination() -> None:
    async with make_client(make_app(store=_store())) as client:
        all_collections = await client.get("/api/collections")
        pending = await client.get("/api/collections?status=pending")
        other_machine = await client.get("/api/collections?machine_id=machine:other")

    assert all_collections.json()["total"] == 2
    assert [item["collection_id"] for item in all_collections.json()["items"]] == [
        "collection:a1",
        "collection:a2",
    ]
    assert [item["collection_id"] for item in pending.json()["items"]] == ["collection:a2"]
    assert other_machine.json()["items"] == []
    assert other_machine.json()["total"] == 0


async def test_records_default_window_returns_everything() -> None:
    async with make_client(make_app(store=_store())) as client:
        response = await client.get("/api/collections/collection:a1/records")

    assert response.status_code == 200
    body = response.json()
    assert [item["record_id"] for item in body["items"]] == [
        f"{number:032x}" for number in (1, 2, 4, 3)
    ]
    assert body["collection_total"] == 4


async def test_records_start_equals_end_returns_single_instant() -> None:
    async with make_client(make_app(store=_store())) as client:
        response = await client.get(
            f"/api/collections/collection:a1/records?start_ns={2 * S}&end_ns={2 * S}"
        )

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["kind"] for item in items} == {"event", "metric"}
    assert {item["monotonic_ns"] for item in items} == {2 * S}


async def test_records_window_outside_data_is_empty_but_total_is_intact() -> None:
    async with make_client(make_app(store=_store())) as client:
        response = await client.get(
            f"/api/collections/collection:a1/records?start_ns={10 * S}&end_ns={11 * S}"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["collection_total"] == 4


async def test_records_kinds_and_pagination() -> None:
    async with make_client(make_app(store=_store())) as client:
        only_events = await client.get("/api/collections/collection:a1/records?kinds=event")
        second = await client.get("/api/collections/collection:a1/records?limit=1&offset=1")

    assert [item["kind"] for item in only_events.json()["items"]] == ["event"] * 3
    assert [item["record_id"] for item in second.json()["items"]] == [f"{2:032x}"]


async def test_records_invalid_clock_domain_is_invalid_parameter() -> None:
    async with make_client(make_app(store=_store())) as client:
        response = await client.get("/api/collections/collection:a1/records?clock_domain=wall")

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_parameter"


async def test_records_negative_time_is_invalid_parameter() -> None:
    async with make_client(make_app(store=_store())) as client:
        response = await client.get("/api/collections/collection:a1/records?start_ns=-1")

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_parameter"


async def test_timeline_tracks_window_boundaries() -> None:
    async with make_client(make_app(store=_store())) as client:
        everything = await client.get("/api/machines/machine:uos-PC/boots/boot:1/timeline/tracks")
        instant = await client.get(
            "/api/machines/machine:uos-PC/boots/boot:1/timeline/tracks"
            f"?start_ns={2 * S}&end_ns={2 * S}"
        )
        outside = await client.get(
            "/api/machines/machine:uos-PC/boots/boot:1/timeline/tracks"
            f"?start_ns={10 * S}&end_ns={11 * S}"
        )
        inverted = await client.get(
            "/api/machines/machine:uos-PC/boots/boot:1/timeline/tracks"
            f"?start_ns={3 * S}&end_ns={1 * S}"
        )

    assert everything.status_code == 200
    body = everything.json()
    assert body["axis_start_ns"] == 1 * S
    assert body["axis_end_ns"] == 3 * S
    assert {track["kind"] for track in body["tracks"]} == {"event", "metric"}

    assert instant.status_code == 200
    assert len(instant.json()["items"]) == 2
    # 轴的范围与查询窗口无关，滑块的量不随缩放变化
    assert instant.json()["axis_start_ns"] == 1 * S

    assert outside.status_code == 200
    assert outside.json()["items"] == []
    assert outside.json()["tracks"], "范围之外仍然列出轨道"
    assert outside.json()["axis_start_ns"] == 1 * S

    assert inverted.status_code == 422
    assert inverted.json()["error"]["type"] == "invalid_parameter"


async def test_timeline_state_and_cursor_round_trip() -> None:
    async with make_client(make_app(store=_store())) as client:
        state = await client.get(
            f"/api/machines/machine:uos-PC/boots/boot:1/timeline/state?monotonic_ns={2 * S}"
        )
        start = await client.get("/api/machines/machine:uos-PC/boots/boot:1/timeline/cursor")
        forward = await client.get(
            "/api/machines/machine:uos-PC/boots/boot:1/timeline/cursor?forward=true"
        )
        backward = await client.get(
            f"/api/machines/machine:uos-PC/boots/boot:1/timeline/cursor?at_ns={3 * S}&forward=false"
        )

    assert state.status_code == 200
    body = state.json()
    assert body["monotonic_ns"] == 2 * S
    assert any(track["metric"] is not None for track in body["tracks"])
    assert any(track["last_event"] is not None for track in body["tracks"])

    assert start.status_code == 200
    assert start.json()["position_ns"] == 1 * S
    assert start.json()["next_ns"] == 2 * S
    assert forward.json()["position_ns"] == 2 * S
    assert backward.json()["position_ns"] == 2 * S


async def test_timeline_resources_report_no_resource_data() -> None:
    """Journal-only 采集：整次启动没有资源数据，用 resources 字段表达，不伪造轨道。"""
    async with make_client(make_app(store=_store())) as client:
        response = await client.get("/api/machines/machine:uos-PC/boots/boot:1/timeline/tracks")

    assert response.status_code == 200
    resources = response.json()["resources"]
    assert resources["available"] is False
    assert resources["reasons"] == ["只含 Journal"]


async def test_offset_above_upper_bound_is_invalid_parameter() -> None:
    async with make_client(make_app(store=_store())) as client:
        response = await client.get("/api/collections?offset=100001")

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_parameter"


async def test_records_offset_beyond_total_returns_empty_page_without_querying() -> None:
    """越界页码不发出 ``LIMIT offset+limit`` 查询：存储层没有 OFFSET，页码越界就该直接给空页。"""

    class CountingStore(FakeStore):
        def __init__(self, source: FakeStore) -> None:
            super().__init__(
                machines=source.machines,
                boots=source.boots,
                collections=source.collections,
                records=source.records,
                streams=source.streams,
            )
            self.record_queries = 0

        async def list_records_in_collection(self, *args, **kwargs):
            self.record_queries += 1
            return await super().list_records_in_collection(*args, **kwargs)

    store = CountingStore(_store())
    async with make_client(make_app(store=store)) as client:
        response = await client.get("/api/collections/collection:a1/records?limit=2&offset=10")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["collection_total"] == 4
    assert store.record_queries == 0, "越界页码不该向存储层取记录"
