"""配置读取的测试：缺项必须失败，环境变量必须生效。"""

import pytest
from pydantic import ValidationError

from system_analyzer.core.config import Settings, get_settings


def test_missing_database_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAS_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_read_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAS_DATABASE_URL", "postgresql+asyncpg://other@127.0.0.1:5432/other")
    monkeypatch.setenv("SAS_WORKER_ID", "worker-7")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.database_url == "postgresql+asyncpg://other@127.0.0.1:5432/other"
    assert settings.worker_id == "worker-7"
    assert settings.api_port == 8000
