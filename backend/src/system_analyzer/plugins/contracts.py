"""插件契约的骨架定义。

完整契约（生命周期、输入输出、错误语义）由任务 0.4 定稿；这里只固定
"一个插件由唯一的 id 与一个类别标识"这一最小事实，供注册表和骨架测试使用。
"""

from dataclasses import dataclass
from enum import StrEnum


class PluginKind(StrEnum):
    """插件类别，与项目描述的四类插件一致。"""

    COLLECTOR = "collector"
    IMPORTER = "importer"
    ANALYZER = "analyzer"
    VISUALIZATION = "visualization"


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """一个插件的注册信息。"""

    id: str
    kind: PluginKind
