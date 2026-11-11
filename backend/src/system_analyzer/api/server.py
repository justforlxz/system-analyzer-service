"""API 进程入口，命令 ``uv run sas-api``。"""

import uvicorn

from system_analyzer.api.app import create_app
from system_analyzer.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(),
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
