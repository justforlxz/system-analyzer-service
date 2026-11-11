"""测试公共准备。

单元测试不需要真实数据库：这里给一个占位的连接串，让配置能构造成功。
需要真实 PostgreSQL 的测试用 ``integration`` 标记，单独运行。
"""

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from system_analyzer.core.config import get_settings

UNIT_TEST_DATABASE_URL = "postgresql+asyncpg://system_analyzer@127.0.0.1:55432/system_analyzer"

MISSING_TEST_DATABASE_URL = (
    "跑集成测试前必须先提供测试库地址：设置环境变量 SAS_TEST_DATABASE_URL，"
    "或在 backend/.env 里配置同名项（见 backend/.env.example）。"
)


class TestDatabaseSettings(BaseSettings):
    """集成测试使用的库地址。

    与 ``Settings`` 共用前缀 ``SAS_`` 与同一个 ``backend/.env``，所以「导出环境变量」与
    「写进 .env」两种方式都生效。这里只声明这一个字段、不要求 ``SAS_DATABASE_URL``，
    因此缺配置时能直接给出「缺哪一项」的提示，而不是先被别的必填项挡住。
    """

    model_config = SettingsConfigDict(
        env_prefix="SAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    test_database_url: str | None = None


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """集成测试使用的数据库连接串，未配置时让用例直接失败。"""
    url = TestDatabaseSettings().test_database_url
    if not url:
        pytest.fail(MISSING_TEST_DATABASE_URL)
    return url


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch):
    """每个测试都从同一份环境变量开始，避免配置缓存互相影响。"""
    monkeypatch.setenv("SAS_DATABASE_URL", UNIT_TEST_DATABASE_URL)
    monkeypatch.delenv("SAS_WORKER_ID", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_configure(config: pytest.Config) -> None:
    """显式要求跑集成测试却没有测试库地址时直接失败。

    pytest 对被整体跳过的用例仍返回退出码 0，会让人误以为集成测试跑过了；
    这里换成明确的报错，退出码非 0。
    """
    expression = (getattr(config.option, "markexpr", "") or "").replace(" ", "")
    wants_integration = "integration" in expression and "notintegration" not in expression
    if wants_integration and not TestDatabaseSettings().test_database_url:
        raise pytest.UsageError(MISSING_TEST_DATABASE_URL)
