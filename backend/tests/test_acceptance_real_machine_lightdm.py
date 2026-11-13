"""验收：在真实 UOS 桌面上验证 LightDM 自动登录与配置恢复。

默认跳过。设置以下环境变量后运行（凭据只从环境变量读取，不写进仓库）：

```bash
SAS_REAL_MACHINE_HOST=10.20.50.152 \
SAS_REAL_MACHINE_USER=uos \
SAS_REAL_MACHINE_PASSWORD='...' \
uv run pytest -m real_machine -s
```

用例做的事与生产采集完全一致：``inspect_target`` → ``backup_configuration`` →
``apply_temporary_configuration`` → ``trigger_desktop_login`` → ``restore_configuration``。
断言三件事：确实建立了新的 seat0 活动图形会话；恢复后 ``/etc/lightdm`` 下的文件与改动前
逐字节一致；远端暂存目录被删除。
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from system_analyzer.collection.context import PhaseContext
from system_analyzer.collection.lightdm import (
    AUTOLOGIN_FRAGMENT,
    LIGHTDM_CONFIG,
    LightDmState,
    read_lightdm_state,
    read_sessions,
)
from system_analyzer.collection.plan import (
    apply_temporary_configuration,
    backup_configuration,
    inspect_target,
    restore_configuration,
    trigger_desktop_login,
)
from system_analyzer.collection.staging import workspace_dir
from system_analyzer.remote.client import AsyncSshClient, SshConnectionConfig

pytestmark = pytest.mark.real_machine

DIRECTORY_FINGERPRINT = (
    "cd /etc/lightdm/lightdm.conf.d 2>/dev/null && "
    'for f in *; do printf \'%s %s\\n\' "$f" "$(sha256sum "$f" 2>/dev/null | '
    "awk '{print $1}')\"; done; true"
)


def real_machine_config() -> SshConnectionConfig:
    password = os.environ.get("SAS_REAL_MACHINE_PASSWORD")
    if not password:
        pytest.skip("没有设置 SAS_REAL_MACHINE_PASSWORD，跳过真机验收")
    return SshConnectionConfig(
        host=os.environ.get("SAS_REAL_MACHINE_HOST", "10.20.50.152"),
        username=os.environ.get("SAS_REAL_MACHINE_USER", "uos"),
        password=password,
        connect_timeout=15,
        command_timeout=120,
    )


class SingleTargetConnector:
    def __init__(self, config: SshConnectionConfig) -> None:
        self._config = config

    @asynccontextmanager
    async def connect(self, target_id: str):
        client = await AsyncSshClient.connect(self._config)
        try:
            yield client
        finally:
            await client.aclose()


async def snapshot(client: AsyncSshClient) -> dict[str, Any]:
    state: LightDmState = await read_lightdm_state(client)
    sessions = await read_sessions(client)
    directory = await client.run(DIRECTORY_FINGERPRINT)
    service = await client.run("systemctl is-active lightdm")
    return {
        "lightdm_active": state.active,
        "service_state": service.stdout.strip(),
        "config_sha256": state.config_sha256,
        "config_mode": state.config_mode,
        "fragments": list(state.fragments),
        "directory": sorted(directory.stdout.splitlines()),
        "sessions": [session.as_mapping() for session in sessions],
    }


async def test_lightdm_autologin_and_restore_on_real_machine(tmp_path: Path) -> None:
    config = real_machine_config()
    connector = SingleTargetConnector(config)
    run_id = f"acceptance-{uuid.uuid4().hex[:12]}"
    ctx = PhaseContext(
        target_id="uos-152",
        run_id=run_id,
        attempt=1,
        options={
            "desktop_user": config.username,
            "desktop_login_timeout": 120,
            "backup_paths": [AUTOLOGIN_FRAGMENT, LIGHTDM_CONFIG],
        },
        scratch_dir=tmp_path,
        data={},
        connector=connector,
    )

    async with connector.connect(ctx.target_id) as client:
        before = await snapshot(client)

    try:
        await inspect_target(ctx)
        await backup_configuration(ctx)
        await apply_temporary_configuration(ctx)
        await trigger_desktop_login(ctx)
        async with connector.connect(ctx.target_id) as client:
            during = await snapshot(client)
    finally:
        await restore_configuration(ctx)
        async with connector.connect(ctx.target_id) as client:
            after = await snapshot(client)
            staging = await client.run(f"test -d {workspace_dir(run_id)}; echo staging_exists=$?")

    desktop = ctx.data["desktop_login"]
    print("real-machine desktop_login:", json.dumps(desktop, ensure_ascii=False, default=str))
    print("before:", json.dumps(before, ensure_ascii=False, default=str))
    print("during:", json.dumps(during, ensure_ascii=False, default=str))
    print("after:", json.dumps(after, ensure_ascii=False, default=str))
    print("staging:", staging.stdout.strip())

    assert desktop["established"] is True, desktop.get("detail")
    assert desktop["session"]["seat"] == "seat0"
    assert desktop["session"]["active"] is True
    assert desktop["session"]["type"] in {"x11", "wayland", "mir"}

    assert before["config_sha256"] == after["config_sha256"]
    assert before["config_mode"] == after["config_mode"]
    assert before["fragments"] == after["fragments"]
    assert before["directory"] == after["directory"]
    assert AUTOLOGIN_FRAGMENT not in after["fragments"]
    assert "staging_exists=1" in staging.stdout
