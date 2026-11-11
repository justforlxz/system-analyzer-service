"""system-analyzer-service 后端包。

模块边界（与父 issue 的并发边界一致）：

- ``api``：FastAPI 应用、路由与依赖注入，唯一业务 API 层。
- ``domain``：统一诊断数据模型与时间基准（由 0.2 / 0.3 定稿后填充）。
- ``storage``：PostgreSQL 读写与仓储实现。
- ``plugins``：插件契约与进程内注册表，API 与 Worker 共用同一份注册。
- ``jobs``：PostgreSQL 支撑的后台任务队列与状态机，Worker 从队列取任务。
- ``worker``：后台任务进程入口，执行采集、导入与分析任务。
- ``collectors`` / ``importers`` / ``analyzers``：三类插件的实现目录。
"""

__version__ = "0.1.0"
