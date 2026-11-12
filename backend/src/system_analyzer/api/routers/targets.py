"""目标机器的管理接口：增删改查与连接测试（任务 4.1）。

机器管理的能力在 ``remote/`` 的服务层已经齐全（``SshTargetService`` 有增删改查与
``test_connection``），这一层只做三件事：

1. 直接把服务层的对象当请求与响应模型——``SshTargetDraft`` / ``SshTarget`` 本来就是对外形状
   （见 ``remote/targets.py`` 的模块说明），在这里再抄一份只会让两处的校验慢慢跑偏；
2. 把「目标机器不存在」翻译成 404；
3. 把连接测试的失败分成四类可读原因。

连接测试失败用 200 加 ``ok: false`` 返回，而不是 4xx：连不上是连接测试的正常结果，不是这次
请求本身出错。四类失败与 ``remote/errors.py`` 里的异常一一对应，``message`` 直接取异常消息
（那条链路上的消息已经不含口令）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from system_analyzer.api.deps import get_target_service
from system_analyzer.api.errors import not_found
from system_analyzer.api.schemas import (
    SystemInfoResponse,
    TestConnectionFailure,
    TestConnectionFailureKind,
    TestConnectionResponse,
)
from system_analyzer.remote.errors import (
    PasswordNotConfigured,
    SshAuthError,
    SshConnectError,
    SshTimeoutError,
    TargetNotFound,
)
from system_analyzer.remote.service import SshTargetService
from system_analyzer.remote.targets import SshTarget, SshTargetDraft

router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("", response_model=list[SshTarget], summary="目标机器列表")
async def list_targets(
    service: SshTargetService = Depends(get_target_service),
) -> list[SshTarget]:
    """全部已保存的目标机器。按创建时间排序，数量小，不分页。

    只返回配置（host / port / username / 口令变量名），不返回口令本身——表里也没有能装口令
    的列。连接状态不在这个响应里：本轮不落库，见界面的偏差说明。
    """
    return await service.list_targets()


@router.post("", response_model=SshTarget, status_code=201, summary="新增目标机器")
async def create_target(
    draft: SshTargetDraft,
    service: SshTargetService = Depends(get_target_service),
) -> SshTarget:
    """新增一台目标机器。字段校验与拒绝理由由 ``SshTargetDraft`` 给出，不在这层重写。"""
    return await service.create(draft)


@router.get("/{target_id}", response_model=SshTarget, summary="目标机器详情")
async def read_target(
    target_id: str,
    service: SshTargetService = Depends(get_target_service),
) -> SshTarget:
    return await _target_or_404(service, target_id)


@router.put("/{target_id}", response_model=SshTarget, summary="整体替换目标机器")
async def replace_target(
    target_id: str,
    draft: SshTargetDraft,
    service: SshTargetService = Depends(get_target_service),
) -> SshTarget:
    """整体替换：提交什么就是什么。

    不做「只改传入字段」的部分更新——那样调用方会遇到「没传的字段被保留成旧值」的意外，
    而编辑表单本来就该把四个字段全部提交上来。
    """
    try:
        return await service.update(target_id, draft)
    except TargetNotFound as exc:
        raise not_found(f"目标机器 {target_id} 不存在") from exc


@router.delete("/{target_id}", status_code=204, summary="删除目标机器")
async def delete_target(
    target_id: str,
    service: SshTargetService = Depends(get_target_service),
) -> Response:
    """只删 ``ssh_targets`` 这一行，不级联。

    已经创建的采集任务（``jobs`` 行）与已经导入的诊断记录都保留：删掉一台机器的配置不该让
    历史数据跟着消失。
    """
    try:
        await service.delete(target_id)
    except TargetNotFound as exc:
        raise not_found(f"目标机器 {target_id} 不存在") from exc
    return Response(status_code=204)


@router.post(
    "/{target_id}/test-connection",
    response_model=TestConnectionResponse,
    summary="对目标机器做一次只读连接测试",
)
async def test_target_connection(
    target_id: str,
    service: SshTargetService = Depends(get_target_service),
) -> TestConnectionResponse:
    """只读测试：读 ``/etc/os-release``、``uname``、``/proc``，不改目标机器的任何配置。"""
    await _target_or_404(service, target_id)
    try:
        info = await service.test_connection(target_id)
    except SshAuthError as exc:
        return _failed("auth", exc)
    except SshTimeoutError as exc:
        return _failed("timeout", exc)
    except SshConnectError as exc:
        return _failed("connect", exc)
    except PasswordNotConfigured as exc:
        return _failed("credential", exc)
    return TestConnectionResponse(ok=True, system=SystemInfoResponse(**info.as_dict()))


async def _target_or_404(service: SshTargetService, target_id: str) -> SshTarget:
    try:
        return await service.get(target_id)
    except TargetNotFound as exc:
        raise not_found(f"目标机器 {target_id} 不存在") from exc


def _failed(kind: TestConnectionFailureKind, exc: Exception) -> TestConnectionResponse:
    return TestConnectionResponse(
        ok=False,
        failure=TestConnectionFailure(kind=kind, message=str(exc)),
    )
