"""健康检查路由。

骨架阶段只需要一个能证明 API 进程可用的端点；业务路由由后续任务加入。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from system_analyzer import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def read_health() -> HealthResponse:
    """返回进程存活状态与版本。"""
    return HealthResponse(status="ok", version=__version__)
