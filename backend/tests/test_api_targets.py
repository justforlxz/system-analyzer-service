"""目标机器管理接口的单元测试：增删改查与连接测试的四类失败。

不连库、不连真机：``SshTargetService`` 换成内存替身，连接测试的结果由用例指定抛哪种异常。
持久化的真实链路在 ``tests/integration/test_ssh_targets_store.py``；四类失败在真机上的取证
写在交付评论里。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from api_fakes import make_app, make_client
from system_analyzer.api.deps import get_target_service
from system_analyzer.remote.errors import (
    PasswordNotConfigured,
    SshAuthError,
    SshConnectError,
    SshTimeoutError,
    TargetNotFound,
)
from system_analyzer.remote.probe import SystemInfo
from system_analyzer.remote.targets import SshTarget, SshTargetDraft

_WHEN = datetime(2026, 1, 1, tzinfo=UTC)
_TARGET_FIELDS = {
    "target_id",
    "host",
    "username",
    "port",
    "password_env_var",
    "created_at",
    "updated_at",
}
"""响应里允许出现的字段。表里没有能装口令的列，所以这里也不该出现任何口令字段。"""

SYSTEM_INFO = SystemInfo(
    hostname="uos-PC",
    os_release="UOS Desktop 25 Professional",
    kernel_version="6.6.0-amd64-desktop",
    architecture="x86_64",
    cpu_count=16,
    memory_total_bytes=16_397_991_936,
    boot_id="abc7451f-65c8-4be8-ab01-4b9a1e49a8b9",
)


class FakeTargetService:
    """``SshTargetService`` 的替身：内存字典 + 可指定结果的连接测试。"""

    def __init__(self) -> None:
        self.targets: dict[str, SshTarget] = {}
        self.test_outcome: SystemInfo | Exception = SYSTEM_INFO
        self.counter = 0

    async def create(self, draft: SshTargetDraft) -> SshTarget:
        self.counter += 1
        return await self._save(f"target:{self.counter}", draft)

    async def update(self, target_id: str, draft: SshTargetDraft) -> SshTarget:
        await self.get(target_id)
        return await self._save(target_id, draft)

    async def delete(self, target_id: str) -> None:
        await self.get(target_id)
        del self.targets[target_id]

    async def list_targets(self) -> list[SshTarget]:
        return list(self.targets.values())

    async def get(self, target_id: str) -> SshTarget:
        target = self.targets.get(target_id)
        if target is None:
            raise TargetNotFound(f"目标机器不存在：{target_id}")
        return target

    async def test_connection(self, target_id: str) -> SystemInfo:
        await self.get(target_id)
        if isinstance(self.test_outcome, Exception):
            raise self.test_outcome
        return self.test_outcome

    async def _save(self, target_id: str, draft: SshTargetDraft) -> SshTarget:
        target = SshTarget(
            **draft.model_dump(),
            target_id=target_id,
            created_at=_WHEN,
            updated_at=_WHEN,
        )
        self.targets[target_id] = target
        return target


def target_client(service: FakeTargetService) -> httpx.AsyncClient:
    """把目标机器服务换成替身的测试客户端。"""
    app = make_app()
    app.dependency_overrides[get_target_service] = lambda: service
    return make_client(app)


async def test_list_targets_is_empty_before_anything_is_saved() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.get("/api/targets")

    assert response.status_code == 200
    assert response.json() == []


async def test_create_target_returns_the_saved_target() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.post(
            "/api/targets",
            json={"host": "10.20.50.152", "username": "uos", "port": 22},
        )
        listed = await client.get("/api/targets")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == _TARGET_FIELDS
    assert body["host"] == "10.20.50.152"
    assert body["port"] == 22
    assert body["password_env_var"] == "SAS_SSH_PASSWORD"
    assert listed.json() == [body]


@pytest.mark.parametrize(
    ("body", "mentioned"),
    [
        ({"host": "", "username": "uos"}, "host"),
        ({"host": "10.20.50.152", "username": "  "}, "username"),
        ({"host": "10.20.50.152", "username": "uos", "port": 70000}, "port"),
        (
            {"host": "10.20.50.152", "username": "uos", "password_env_var": "sas_password"},
            "password_env_var",
        ),
    ],
)
async def test_create_target_rejects_invalid_input(body: dict, mentioned: str) -> None:
    """四类非法输入都在请求模型里被拒，接口返回统一错误信封。"""
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.post("/api/targets", json=body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "invalid_parameter"
    assert any(mentioned in item for item in error["details"])
    assert service.targets == {}


async def test_read_unknown_target_is_not_found() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.get("/api/targets/target:missing")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


async def test_replace_target_replaces_every_field() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        created = (
            await client.post("/api/targets", json=_draft(password_env_var="SAS_OTHER_PASSWORD"))
        ).json()
        response = await client.put(
            f"/api/targets/{created['target_id']}",
            json={"host": "10.20.50.160", "username": "uos", "port": 2222},
        )
        detail = await client.get(f"/api/targets/{created['target_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "10.20.50.160"
    assert body["port"] == 2222
    # 整体替换：没有提交的口令变量名回到默认值，而不是保留成创建时的 SAS_OTHER_PASSWORD
    assert body["password_env_var"] == "SAS_SSH_PASSWORD"
    assert detail.json() == body


async def test_replace_unknown_target_is_not_found() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.put("/api/targets/target:missing", json=_draft())

    assert response.status_code == 404


async def test_delete_target_removes_only_that_row() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        first = (await client.post("/api/targets", json=_draft())).json()
        second = (await client.post("/api/targets", json=_draft(host="10.20.50.160"))).json()
        deleted = await client.delete(f"/api/targets/{first['target_id']}")
        remaining = await client.get("/api/targets")
        gone = await client.get(f"/api/targets/{first['target_id']}")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert [item["target_id"] for item in remaining.json()] == [second["target_id"]]
    assert gone.status_code == 404


async def test_delete_unknown_target_is_not_found() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.delete("/api/targets/target:missing")

    assert response.status_code == 404


async def test_test_connection_returns_system_info() -> None:
    service = FakeTargetService()
    async with target_client(service) as client:
        target = (await client.post("/api/targets", json=_draft())).json()
        response = await client.post(f"/api/targets/{target['target_id']}/test-connection")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "system": {
            "hostname": "uos-PC",
            "os_release": "UOS Desktop 25 Professional",
            "kernel_version": "6.6.0-amd64-desktop",
            "architecture": "x86_64",
            "cpu_count": 16,
            "memory_total_bytes": 16_397_991_936,
            "boot_id": "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9",
        },
        "failure": None,
    }


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (SshConnectError("建立 SSH 连接失败：10.20.50.152:22 不可达"), "connect"),
        (SshAuthError("建立 SSH 连接认证失败：10.20.50.152:22 拒绝了用户名或口令"), "auth"),
        (SshTimeoutError("建立 SSH 连接超时：10.20.50.152:22 1 秒内没有完成"), "timeout"),
        (
            PasswordNotConfigured("运行环境里没有配置 SAS_SSH_PASSWORD，无法解析目标机器口令"),
            "credential",
        ),
    ],
)
async def test_test_connection_reports_each_failure_kind(error: Exception, kind: str) -> None:
    """四类失败都是 200 加 ``ok: false``：连不上是连接测试的结果，不是请求出错。"""
    service = FakeTargetService()
    service.test_outcome = error
    async with target_client(service) as client:
        target = (await client.post("/api/targets", json=_draft())).json()
        response = await client.post(f"/api/targets/{target['target_id']}/test-connection")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["system"] is None
    assert body["failure"] == {"kind": kind, "message": str(error)}


async def test_test_connection_on_unknown_target_is_not_found() -> None:
    """目标机器不存在走 404，而不是 200 加一个「连不上」的失败分类。"""
    service = FakeTargetService()
    async with target_client(service) as client:
        response = await client.post("/api/targets/target:missing/test-connection")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


def _draft(
    *,
    host: str = "10.20.50.152",
    port: int = 22,
    password_env_var: str = "SAS_SSH_PASSWORD",
) -> dict:
    return {
        "host": host,
        "username": "uos",
        "port": port,
        "password_env_var": password_env_var,
    }
