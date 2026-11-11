"""API 骨架的冒烟测试：应用能构造，健康检查能用。"""

import httpx

from system_analyzer import __version__
from system_analyzer.api.app import create_app


async def test_health_returns_ok() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_openapi_contains_health_path() -> None:
    schema = create_app().openapi()
    assert "/api/health" in schema["paths"]
