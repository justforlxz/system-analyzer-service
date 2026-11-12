"""远程采集任务的三阶段编排与状态机（任务 3.2）。

```
Worker 领取 jobs.kind = "collect.target"
              │
              ▼
        ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
        │  Pre 阶段   │──▶│ In Progress │──▶│  Post 阶段  │
        │ 检查/备份/  │   │ 采样/登录/  │   │ 停止/收集/  │
        │ 临时配置    │   │ 持续采集    │   │ 恢复环境    │
        └─────────────┘   └─────────────┘   └─────────────┘
                                  │ 失败、取消、超时都仍然执行 Post
```

模块分工：

| 模块 | 内容 |
| --- | --- |
| ``states.py`` | 阶段状态、任务状态与转换表（唯一真值） |
| ``record.py`` | 每次尝试的执行记录：阶段状态、操作事件、跨阶段数据 |
| ``context.py`` | 步骤能拿到的东西（目标机器、临时目录、中间数据、SSH 连接） |
| ``plan.py`` | 三个阶段各有哪些步骤；3.4–3.7 在这里填真实动作 |
| ``connector.py`` | 按目标机器 id 建立 SSH 连接的接缝（生产实现复用 ``remote/``） |
| ``orchestrator.py`` | 顺序、超时、取消、失败清理与重试复用 |
| ``jobs.py`` | Worker 任务处理器、任务锁续租、取消标记读取 |

状态转换图与「每条转换对应哪个测试」见 ``docs/collection-orchestration.md``。

本包只做编排：不采集具体数据（3.4、3.5）、不实现 LightDM 登录（3.6）、不做数据回传（3.7），
也不改 ``remote/`` 的任何文件。
"""

from system_analyzer.collection.jobs import (
    COLLECTION_JOB_KIND,
    CollectionJobFailed,
    CollectionJobPayload,
    collection_handlers,
    run_collection_job,
)
from system_analyzer.collection.orchestrator import CollectionOutcome, run_collection
from system_analyzer.collection.states import (
    AttemptStatus,
    CollectionPhase,
    InvalidTransition,
    PhaseStatus,
)

__all__ = [
    "COLLECTION_JOB_KIND",
    "AttemptStatus",
    "CollectionJobFailed",
    "CollectionJobPayload",
    "CollectionOutcome",
    "CollectionPhase",
    "InvalidTransition",
    "PhaseStatus",
    "collection_handlers",
    "run_collection",
    "run_collection_job",
]
