"""按 manifest 的 ``config_schema`` 校验插件配置。

Core 是配置解析与校验的唯一责任方（见 ``docs/plugin-contracts.md`` 第 1 节「职责划分」）：
插件不读环境变量、不读配置文件，它拿到的 ``PluginContext.config`` 已经过这里的校验。

校验用标准 JSON Schema 实现，不自造子集：manifest 里写了什么关键字，配置就按什么检查，
不会出现「schema 写了但配置没被检查」这种静默失效。``config_schema = {}`` 是唯一特例，
契约规定它表示「不接受任何配置」，因此这时传入非空配置也算不合法。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from system_analyzer.plugins.contracts import PluginError, PluginSpec


class PluginConfigError(PluginError):
    """插件配置不符合自身 manifest 的 ``config_schema``。

    这是 Core 在把配置交给插件之前做出的拒绝：错误信息指出插件、配置位置与不合格的原因，
    调用方据此修正配置，不需要去读插件源码。
    """


def validate_plugin_config(
    spec: PluginSpec,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按 ``spec.config_schema`` 校验配置，返回可以交给插件的副本。

    不合法时抛 ``PluginConfigError``。返回的是新字典，调用方之后再改原对象不会影响
    已经交给插件的 ``PluginContext.config``。
    """
    resolved = dict(config or {})
    if not spec.config_schema:
        if resolved:
            raise PluginConfigError(
                f"插件 {spec.id} 的 config_schema 为空，不接受任何配置；"
                f"实际传入：{', '.join(sorted(resolved))}"
            )
        return resolved

    errors = sorted(
        Draft202012Validator(spec.config_schema).iter_errors(resolved),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        raise PluginConfigError(f"插件 {spec.id} 的配置不合法：{_describe(errors[0])}")
    return resolved


def _describe(error: ValidationError) -> str:
    """把 jsonschema 的错误说成一句带配置位置的话。

    例如 ``strict：'yes' is not of type 'boolean'``。
    """
    where = ".".join(str(part) for part in error.absolute_path) or "顶层"
    return f"{where}：{error.message}"
