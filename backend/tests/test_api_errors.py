"""统一错误结构的单元测试：每一种失败都收敛成同一个信封，且能区分四类原因。

不连数据库：存储与时间线都用 ``tests/api_fakes.py`` 里的替身。
"""

from __future__ import annotations

import httpx
import pytest

from api_fakes import FakeStore, boot, collection, machine, make_app, make_client

ERROR_TYPES = {"invalid_parameter", "invalid_format", "not_found", "internal_error"}


def _error(response: httpx.Response) -> dict:
    payload = response.json()
    assert set(payload) == {"error"}, payload
    body = payload["error"]
    assert set(body) == {"type", "message", "details"}, body
    assert body["type"] in ERROR_TYPES
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["details"], list)
    return body


async def test_health_response_is_unchanged() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "version"}


async def test_unknown_path_is_not_found() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/不存在")

    assert response.status_code == 404
    assert _error(response)["type"] == "not_found"


async def test_unknown_machine_is_not_found() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/machines/machine:missing")

    assert response.status_code == 404
    body = _error(response)
    assert body["type"] == "not_found"
    assert "machine:missing" in body["message"]


async def test_unknown_boot_is_not_found() -> None:
    store = FakeStore(machines={machine().machine_id: machine()})
    async with make_client(make_app(store=store)) as client:
        response = await client.get("/api/machines/machine:uos-PC/boots/boot:missing")

    assert response.status_code == 404
    assert _error(response)["type"] == "not_found"


async def test_unknown_collection_is_not_found() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/collections/collection:missing")

    assert response.status_code == 404
    assert _error(response)["type"] == "not_found"


@pytest.mark.parametrize(
    ("query", "expected_fragment"),
    [
        ("limit=0", "limit"),
        ("limit=201", "limit"),
        ("offset=-1", "offset"),
    ],
)
async def test_invalid_pagination_is_invalid_parameter(query: str, expected_fragment: str) -> None:
    async with make_client(make_app()) as client:
        response = await client.get(f"/api/collections?{query}")

    assert response.status_code == 422
    body = _error(response)
    assert body["type"] == "invalid_parameter"
    assert any(expected_fragment in detail for detail in body["details"]), body


async def test_invalid_uuid_path_is_invalid_parameter() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/imports/not-a-uuid")

    assert response.status_code == 422
    assert _error(response)["type"] == "invalid_parameter"


async def test_invalid_time_range_is_invalid_parameter() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/collections/collection:a1/records?start_ns=10&end_ns=5")

    assert response.status_code == 422
    body = _error(response)
    assert body["type"] == "invalid_parameter"
    assert "晚于" in body["message"]


async def test_unknown_record_kind_is_invalid_parameter() -> None:
    store = FakeStore(collections={"collection:a1": collection()})
    async with make_client(make_app(store=store)) as client:
        response = await client.get("/api/collections/collection:a1/records?kinds=unknown")

    assert response.status_code == 422
    body = _error(response)
    assert body["type"] == "invalid_parameter"
    assert body["details"]


async def test_timeline_kind_out_of_range_is_invalid_parameter() -> None:
    store = FakeStore(boots={("machine:uos-PC", "boot:a"): boot()})
    async with make_client(make_app(store=store)) as client:
        response = await client.get(
            "/api/machines/machine:uos-PC/boots/boot:a/timeline/tracks?kinds=profile"
        )

    assert response.status_code == 422
    body = _error(response)
    assert body["type"] == "invalid_parameter"
    assert "profile" in body["message"]


async def test_timeline_boot_not_found_maps_to_not_found() -> None:
    async with make_client(make_app()) as client:
        response = await client.get("/api/machines/machine:uos-PC/boots/boot:a/timeline/tracks")

    assert response.status_code == 404
    body = _error(response)
    assert body["type"] == "not_found"
    assert "boot:a" in body["message"]


async def test_unexpected_exception_becomes_internal_error_without_details() -> None:
    """500 只给固定文案加请求 id：异常类型与原文可能带路径、SQL 片段，不能回给调用方。"""

    class BrokenStore(FakeStore):
        async def get_machine(self, machine_id: str):
            raise RuntimeError("数据库炸了：SELECT * FROM secrets")

    async with make_client(make_app(store=BrokenStore())) as client:
        response = await client.get("/api/machines/machine:uos-PC")

    assert response.status_code == 500
    body = _error(response)
    assert body["type"] == "internal_error"
    assert "请求 id" in body["message"]
    assert "RuntimeError" not in response.text
    assert "数据库炸了" not in response.text
    assert "SELECT" not in response.text
