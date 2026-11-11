"""目标机器模型、仓储接口与服务层（不连数据库、不连网络）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ssh_fakes import FakeSshClient
from system_analyzer.core.config import Settings
from system_analyzer.remote.client import CommandResult, SshConnectionConfig
from system_analyzer.remote.credentials import PASSWORD_ENV_VAR
from system_analyzer.remote.errors import PasswordNotConfigured, TargetNotFound
from system_analyzer.remote.probe import SYSTEM_INFO_COMMAND
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.targets import SshTarget, SshTargetDraft, SshTargetRepository

DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"
PASSWORD = "".join(("unit", "test", "-pw"))


def make_settings(password: str | None = PASSWORD) -> Settings:
    return Settings(database_url=DATABASE_URL, ssh_password=password)


def make_target(**overrides: object) -> SshTarget:
    now = datetime(2026, 11, 11, tzinfo=UTC)
    values: dict[str, object] = {
        "target_id": "t-1",
        "host": "10.20.50.152",
        "port": 22,
        "username": "uos",
        "password_env_var": PASSWORD_ENV_VAR,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return SshTarget(**values)


class FakeRepository:
    """``SshTargetRepository`` 的内存替身：只实现服务层用到的方法。"""

    def __init__(self) -> None:
        self.rows: dict[str, SshTarget] = {}
        self._next = 0

    async def create(self, draft: SshTargetDraft) -> SshTarget:
        self._next += 1
        target = SshTarget(
            target_id=f"t-{self._next}",
            created_at=datetime(2026, 11, 11, tzinfo=UTC),
            updated_at=datetime(2026, 11, 11, tzinfo=UTC),
            **draft.model_dump(),
        )
        self.rows[target.target_id] = target
        return target

    async def update(self, target_id: str, draft: SshTargetDraft) -> SshTarget:
        existing = self.rows.get(target_id)
        if existing is None:
            raise TargetNotFound(f"目标机器不存在：{target_id}")
        updated = existing.model_copy(update=draft.model_dump())
        self.rows[target_id] = updated
        return updated

    async def delete(self, target_id: str) -> None:
        if target_id not in self.rows:
            raise TargetNotFound(f"目标机器不存在：{target_id}")
        del self.rows[target_id]

    async def get(self, target_id: str) -> SshTarget | None:
        return self.rows.get(target_id)

    async def list(self) -> list[SshTarget]:
        return list(self.rows.values())


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_draft_defaults() -> None:
    draft = SshTargetDraft(host="10.20.50.152", username="uos")

    assert draft.port == 22
    assert draft.password_env_var == PASSWORD_ENV_VAR


def test_draft_strips_surrounding_spaces() -> None:
    draft = SshTargetDraft(host="  10.20.50.152 ", username=" uos ")

    assert draft.host == "10.20.50.152"
    assert draft.username == "uos"


@pytest.mark.parametrize("field", ["host", "username"])
def test_blank_host_or_username_is_rejected(field: str) -> None:
    values = {"host": "h", "username": "u", field: "   "}

    with pytest.raises(ValidationError):
        SshTargetDraft(**values)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_port_out_of_range_is_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        SshTargetDraft(host="h", username="u", port=port)


@pytest.mark.parametrize(
    "name", ["lower_case", "1STARTS_WITH_DIGIT", "WITH-DASH", "WITH SPACE", ""]
)
def test_password_env_var_must_be_an_env_var_name(name: str) -> None:
    with pytest.raises(ValidationError):
        SshTargetDraft(host="h", username="u", password_env_var=name)


def test_unknown_fields_are_rejected() -> None:
    """写错字段名（例如把口令直接塞进 password）必须报错，而不是被静默忽略。"""
    with pytest.raises(ValidationError):
        SshTargetDraft(host="h", username="u", password="plain-text")


# ---------------------------------------------------------------------------
# 输出里没有口令
# ---------------------------------------------------------------------------


def test_target_model_has_no_field_for_a_password() -> None:
    assert "password" not in SshTarget.model_fields
    assert not any("password" == name for name in SshTarget.model_fields)


def test_target_serialization_contains_only_the_reference() -> None:
    target = make_target()

    dumped = target.model_dump_json()

    assert PASSWORD not in dumped
    assert PASSWORD_ENV_VAR in dumped
    assert PASSWORD not in repr(target)


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------


async def test_create_list_get_update_delete() -> None:
    repository = FakeRepository()
    service = SshTargetService(repository, settings=make_settings())

    created = await service.create(SshTargetDraft(host="10.20.50.152", username="uos"))
    assert [target.target_id for target in await service.list_targets()] == [created.target_id]
    assert (await service.get(created.target_id)).host == "10.20.50.152"

    updated = await service.update(
        created.target_id,
        SshTargetDraft(host="10.20.50.153", username="root", port=2222),
    )
    assert updated.host == "10.20.50.153"
    assert updated.port == 2222

    await service.delete(created.target_id)
    assert await service.list_targets() == []


async def test_get_missing_target_is_an_error() -> None:
    service = SshTargetService(FakeRepository(), settings=make_settings())

    with pytest.raises(TargetNotFound):
        await service.get("nope")


async def test_delete_missing_target_is_an_error() -> None:
    service = SshTargetService(FakeRepository(), settings=make_settings())

    with pytest.raises(TargetNotFound):
        await service.delete("nope")


def test_connection_config_resolves_the_password() -> None:
    service = SshTargetService(FakeRepository(), settings=make_settings())

    config = service.connection_config(make_target())

    assert isinstance(config, SshConnectionConfig)
    assert config.host == "10.20.50.152"
    assert config.password == PASSWORD
    assert PASSWORD not in repr(config)


def test_connection_config_without_a_configured_password_fails_early() -> None:
    """口令没配好时在连接之前就报清楚，不要等 SSH 握手失败再猜原因。"""
    service = SshTargetService(FakeRepository(), settings=make_settings(None))

    with pytest.raises(PasswordNotConfigured):
        service.connection_config(make_target())


async def test_connect_yields_the_client_and_closes_it() -> None:
    client = FakeSshClient()
    requested: list[SshConnectionConfig] = []

    async def factory(config: SshConnectionConfig) -> FakeSshClient:
        requested.append(config)
        return client

    service = SshTargetService(FakeRepository(), settings=make_settings(), client_factory=factory)
    target = await service.create(SshTargetDraft(host="10.20.50.152", username="uos"))

    async with service.connect(target.target_id) as opened:
        assert opened is client

    assert requested[0].host == "10.20.50.152"
    assert client.closed is True


async def test_test_connection_returns_system_info() -> None:
    stdout = "hostname=uos-PC\ncpu_count=16\nos_release=UOS Desktop 25\n"
    client = FakeSshClient(
        run_result=CommandResult(
            command=SYSTEM_INFO_COMMAND, stdout=stdout, stderr="", exit_status=0
        )
    )

    async def factory(config: SshConnectionConfig) -> FakeSshClient:
        return client

    service = SshTargetService(FakeRepository(), settings=make_settings(), client_factory=factory)
    target = await service.create(SshTargetDraft(host="10.20.50.152", username="uos"))

    info = await service.test_connection(target.target_id)

    assert info.hostname == "uos-PC"
    assert info.cpu_count == 16
    assert client.commands == [SYSTEM_INFO_COMMAND]
    assert client.closed is True


async def test_test_connection_propagates_ssh_failures() -> None:
    from system_analyzer.remote.errors import SshAuthError

    client = FakeSshClient(run_error=SshAuthError("认证失败"))

    async def factory(config: SshConnectionConfig) -> FakeSshClient:
        return client

    service = SshTargetService(FakeRepository(), settings=make_settings(), client_factory=factory)
    target = await service.create(SshTargetDraft(host="10.20.50.152", username="uos"))

    with pytest.raises(SshAuthError):
        await service.test_connection(target.target_id)

    assert client.closed is True


def test_repository_type_is_documented() -> None:
    """服务层用的就是仓储类型本身，测试替身只是形状一致。"""
    assert hasattr(SshTargetRepository, "create")
