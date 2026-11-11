"""凭据引用解析：库里只有变量名，值只在运行环境里。"""

from __future__ import annotations

import pytest

from system_analyzer.core.config import Settings
from system_analyzer.remote.credentials import PASSWORD_ENV_VAR, EnvironmentPasswordResolver
from system_analyzer.remote.errors import PasswordNotConfigured

DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"
PASSWORD = "".join(("unit", "test", "-pw"))


def make_settings(password: str | None) -> Settings:
    return Settings(database_url=DATABASE_URL, ssh_password=password)


def test_resolves_the_password_from_settings() -> None:
    resolver = EnvironmentPasswordResolver(make_settings(PASSWORD))

    assert resolver.resolve(PASSWORD_ENV_VAR) == PASSWORD


def test_missing_password_is_reported_by_variable_name() -> None:
    resolver = EnvironmentPasswordResolver(make_settings(None))

    with pytest.raises(PasswordNotConfigured) as caught:
        resolver.resolve(PASSWORD_ENV_VAR)

    assert PASSWORD_ENV_VAR in str(caught.value)
    assert "没有配置" in str(caught.value)


def test_empty_password_counts_as_not_configured() -> None:
    """``.env.example`` 里留着空值，复制过来时不能当成「口令是空字符串」去连。"""
    resolver = EnvironmentPasswordResolver(make_settings(""))

    with pytest.raises(PasswordNotConfigured):
        resolver.resolve(PASSWORD_ENV_VAR)


def test_unsupported_reference_is_rejected_with_the_supported_name() -> None:
    resolver = EnvironmentPasswordResolver(make_settings(PASSWORD))

    with pytest.raises(PasswordNotConfigured) as caught:
        resolver.resolve("OTHER_SSH_PASSWORD")

    assert PASSWORD_ENV_VAR in str(caught.value)
    assert "1.0 只支持" in str(caught.value)


def test_settings_repr_masks_the_password() -> None:
    """``Settings`` 被打印或写进日志时不能泄口令。"""
    settings = make_settings(PASSWORD)

    assert PASSWORD not in repr(settings)
    assert PASSWORD not in str(settings)


def test_resolved_password_is_absent_from_error_messages() -> None:
    """无论成功还是失败，异常消息都不带口令。"""
    resolver = EnvironmentPasswordResolver(make_settings(None))

    with pytest.raises(PasswordNotConfigured) as caught:
        resolver.resolve(PASSWORD_ENV_VAR)

    assert PASSWORD not in str(caught.value)
