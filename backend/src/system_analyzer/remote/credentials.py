"""目标机器凭据的引用与解析。

1.0 的约束（任务 MKACG-774 的 handoff）：口令只从环境变量（或未提交的 ``backend/.env``）读取，
数据库里只存**引用**，不存口令原文。

做法：

- ``ssh_targets`` 表的 ``password_env_var`` 列存环境变量名，例如 ``SAS_SSH_PASSWORD``。
  它只是名字，不是口令；值永远不落库。
- 名字统一由 ``Settings`` 解析——``Settings`` 是 ``.env`` 的唯一读取入口，所以「导出环境变量」
  与「写进 ``backend/.env``」两种配置方式都生效，不需要在别处再读一次环境变量。
- 1.0 只支持一个凭据：``SAS_SSH_PASSWORD``。多台目标机器共用同一组账号口令时够用，
  也是 handoff 里「Settings 里加一个字段」的要求。

后续演进（不引入新的表结构）：``password_env_var`` 已经是每台机器独立的引用，
把 ``EnvironmentPasswordResolver`` 换成「按名字从密钥管理服务取」的实现即可，表与调用方不动。
"""

from __future__ import annotations

from system_analyzer.core.config import Settings
from system_analyzer.remote.errors import PasswordNotConfigured

PASSWORD_ENV_VAR = "SAS_SSH_PASSWORD"
"""1.0 唯一支持的口令环境变量名，也是 ``ssh_targets.password_env_var`` 的默认值。"""


class EnvironmentPasswordResolver:
    """按引用取口令。库里只有引用，值只存在于运行环境。

    ``resolve`` 返回的是明文 ``str``，只用于交给 SSH 客户端；调用方不得把它写进日志、
    异常消息或数据库。``Settings.ssh_password`` 本身是 ``SecretStr``，打印与 ``repr`` 都会被掩码。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, password_env_var: str) -> str:
        """取 ``password_env_var`` 指向的口令；未配置或引用不受支持时抛异常。

        失败信息只说「哪个变量没配」或「只支持哪个变量」，不含任何口令内容。
        """
        if password_env_var != PASSWORD_ENV_VAR:
            raise PasswordNotConfigured(
                f"目标机器引用了口令变量 {password_env_var}，但 1.0 只支持从 "
                f"{PASSWORD_ENV_VAR} 读取口令；请把目标机器的 password_env_var 改成 "
                f"{PASSWORD_ENV_VAR}"
            )
        secret = self._settings.ssh_password
        password = secret.get_secret_value() if secret is not None else ""
        if not password:
            raise PasswordNotConfigured(
                f"运行环境没有配置 {PASSWORD_ENV_VAR}："
                f"请在环境变量或 backend/.env 里设置目标机器的 SSH 口令"
            )
        return password
