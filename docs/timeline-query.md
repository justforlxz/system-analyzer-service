# 时间线查询与回放接口

本文档由任务 1.5（MKACG-770）定稿，说明时间线引擎对 Web（4.2 时间线界面、4.3 回放）提供的
轨道数据与时间游标。规则本身来自 `docs/time-base.md` 与 `docs/data-model.md`，本文档只描述
**接口形状与读取口径**，不重新定义时间语义。

- 可执行版本：`backend/src/system_analyzer/timeline/`（`engine.py` 查询、`models.py` 数据形状）。
- 文档与代码冲突时先改文档，再改代码。
- 本任务不提供 HTTP 接口（1.6）、不生成分析结论（2.x）、不做界面（4.x）。

目录：

1. 边界与位置
2. 轨道是怎么分出来的
3. 条目：Event、Span、Metric
4. 三种轨道状态与读取口径
5. 开放区间的收尾
6. 时间范围的语义
7. 指定时刻的状态
8. 统一时间游标
9. 只有墙钟的记录与漂移
10. Web 怎么用
11. 已知限制与偏差

## 1. 边界与位置

| 项 | 内容 |
| --- | --- |
| 入口 | `TimelineEngine`，构造参数是只读接口 `TimelineReader` |
| 读数据 | 只调 `DiagnosticStore` 已有的四个方法：`get_boot`、`list_collections`、`list_records_in_boot`、`list_metric_streams` |
| 不读 | 不写 SQL、不改存储层、不读 `importers/`、不碰前端 |
| 时间基准 | 一律用 boot 内位置 `monotonic_ns`；墙钟只用于显示时间文字 |
| 启动范围 | 每次查询固定一次启动 `(machine_id, boot_id)`；跨启动排序在引擎里直接拒绝 |

读到的记录若不属于查询的那次启动，抛 `TimelineScopeError`。这是 `docs/time-base.md` 第 3.2 节
「跨启动排序在 1.5 的查询里直接拒绝」的落点：宁可报错，也不返回一个看起来合理但没有物理意义的顺序。

## 2. 轨道是怎么分出来的

一条轨道是界面上的一条水平线，由下面四个字段唯一确定：

| 字段 | 含义 |
| --- | --- |
| `kind` | 记录种类：`event`、`span`、`metric` |
| `source_plugin` | 产生这条数据的插件 |
| `stream_id` | 逻辑流，例如 `systemd.units`、`cpu` |
| `entity` | 记录关联的实体，例如 `service:nginx.service` |

`track_id` 是这四者的拼接，对调用方是**不透明字符串**：只用来区分轨道，不要解析它，也不要自己拼。

两类特殊情形：

- **指标轨道以 `metric_stream` 实体为准**，因此「被声明过、但一个采样点都没有」的流也会作为一条
  轨道出现，并带上不可用原因；`source_plugin` 与 `stream_id` 从采样记录取，没有记录时为 `null`。
- **一个实体同时有事件与区间**时会出现两条轨道（`kind` 不同）。它们画在不同的线上，状态也分别查询。

轨道按 `track_id` 升序返回，顺序稳定：同一份数据任何时候查询都得到同一个轨道顺序。

本任务只产出 Event、Span、Metric 三类轨道。Profile、Annotation、Artifact 的展示属于 2.x 与 4.x，
在 `kinds` 里传入它们会抛 `TimelineKindError`，不会静默返回空结果。拼错的值（例如 `anntation`）
同样抛 `TimelineKindError`，不会漏出 `ValueError`。

`kinds` 只决定**返回哪些轨道与条目**：轴的起止、开放区间收尾用的采集窗口、启动锚点与漂移、
资源声明都按整次启动的全部记录算，不随筛选变化。否则同一条开放区间在不同调用里会得到不同长度，
漂移也会随筛选翻转，回放就不可重复了（`docs/time-base.md` 第 6、4.4 节）。

## 3. 条目：Event、Span、Metric

三种条目共用定位字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `record_id` | string | 记录内容摘要，`(collection_id, record_id)` 是唯一键 |
| `collection_id` | string | 这条记录属于哪次采集 |
| `monotonic_ns` | int | boot 内位置 |
| `realtime_ns` | int 或 `null` | 记录的墙钟读数，原样透出，不做换算 |

各自的额外字段：

| 条目 | 额外字段 |
| --- | --- |
| `TimelineEvent` | `event_type`、`severity`、`message`、`attributes`、`entity`、`source_plugin`、`stream_id` |
| `TimelineSpan` | `span_type`、`state`、`end_monotonic_ns`、`end_realtime_ns`、`end_is_declared`、`closed_by`、`attributes` |
| `MetricPoint` | `name`、`value`、`unit` |

`TimelineSpan` 的结束字段：

- `end_is_declared` 为真：记录自己声明了结束（`end_monotonic_ns` 或 `end_realtime_ns` 至少一个非空）。
- `end_is_declared` 为假：开放区间，引擎按所属采集的窗口终点收尾（第 5 节），`closed_by` 写这次的
  `collection_id`。
- 只有墙钟声明了结束时，`end_monotonic_ns` 保持 `null`：不把墙钟收尾换算成单调时间
  （`docs/time-base.md` 第 6 节最后一行）。

条目按 `(monotonic_ns, collection_id, record_id)` 排序（`docs/time-base.md` 第 7.1 节）。
结果里的 `items` 是全部轨道合在一起的这一条轴，轨道内的 `items` 是它按轨道切出来的子序列，
两者顺序一致，可以直接按同一个游标画。

两次采集观测到同一状态时不合并：两条都是独立事实，都保留。去重只发生在导入的幂等键上
（`docs/data-model.md` 第 8 节），时间线不做内容去重。

## 4. 三种轨道状态与读取口径

`status` 只有三个取值，互斥：

| 状态 | 什么时候出现 | 界面怎么画 |
| --- | --- | --- |
| `available` | 这条轨道有记录 | 正常画；本窗口内没有点时按缺口断开 |
| `unavailable` | 没有记录，且指标流实体标了 `available = false` | 整条轨道显示「不可用：<原因>」，**不画 0** |
| `no_data` | 没有记录，也没有任何采集声明它不可用 | 按缺口处理，不画 0，也不写成不可用 |

`unavailable` 时 `unavailable_reason` 一定有值，取自 `metric_stream` 实体的
`unavailable_reason`（导入时校验过：不可用必须写原因）。

### 多次采集对 `available` 判断不一致时怎么读

0.2 没有规定同一条指标流被多次采集给出不同 `available` 时以哪次为准，1.0 先按下述读侧口径处理：

- **有记录的窗口就显示数据。** 不因为另一次采集把这条流标成不可用而隐藏已有的采样点。
- **没有记录、且这条流被标成不可用**：标记 `unavailable` 并带原因。
- **没有记录、也没有任何采集声明过这条流**：标记 `no_data`，不写成 0。

### 这条口径的已知限制（留给后续版本定稿）

- 存储层每个 `(machine_id, boot_id, entity_id)` 只保留**先写入**的那一份 `metric_stream` 描述
  （1.1 的实现，见 `storage/store.py` 的 `_insert_entities`）。因此读侧只能按这份最早的声明判断，
  「某个具体时间窗口所属的那次采集怎么说」在库里查不到，上面第二条只能落到
  「整个 boot 的这个流」这一粒度，不能按采集或按窗口细分。
- 实体 `available = true` 却一个采样点都没有时，读侧只能报 `no_data`，无法区分「采集声明可用但
  采集失败」与「声明可用且确实没有采样」。
- 整次启动没有资源数据时（Journal-only 导入），`metric_stream` 实体根本不存在，因此**不会**出现
  资源轨道。这件事通过结果里的 `resources` 字段表达：`available = false`、`reasons` 里是各次采集
  给出的原因（例如「本次导入不含资源数据」）。这与「某条指标流不可用」是两件事
  （`docs/time-base.md` 第 8.1 节）。

## 5. 开放区间的收尾

`end_*_ns` 为 `null` 的记录表示采集结束时该过程仍未结束。时间线上按
`docs/time-base.md` 第 6 节收尾：

| 项 | 做法 |
| --- | --- |
| 收尾时刻 | 所属 collection 的采集窗口终点，即该 collection 内所有记录时间字段（含 `end_*_ns`）的最大值 |
| 是否改记录 | 不改。`end_*_ns` 永远是 `null`，收尾结果只出现在查询结果里 |
| 怎么标注 | `end_is_declared = false`，`closed_by` 指向用来收尾的 collection |
| 多次采集 | 每段区间只用自己所属 collection 的窗口收尾，不跨空档接续 |
| 禁止 | 不用「查询时刻」「当前时间」「导入时间」「启动结束时刻」收尾 |
| 与筛选无关 | 只取部分种类（`kinds`）时，窗口终点仍按该 collection 的全部记录算，区间不会被截短 |

由此得到一个可验证的性质：**同一份数据在不同查询范围里回放，同一条区间的起止完全一样**。
收尾时刻不受 `start_ns` / `end_ns` 影响，也不随查询发生的时刻变化。
收尾时刻也同样不受 `kinds` 影响。

## 6. 时间范围的语义

| 写法 | 含义 |
| --- | --- |
| 都不给 | 整条轴 |
| 只给 `start_ns` | `[start_ns, +∞)` |
| 只给 `end_ns` | `(-∞, end_ns]` |
| 都给 | 闭区间 `[start_ns, end_ns]`，正好落在两端的记录包含在内 |
| `start_ns == end_ns` | 单个瞬间：返回这个时刻的点，以及覆盖这个时刻的区间 |
| `start_ns > end_ns` | 抛 `TimelineRangeError`，不做静默交换 |
| 范围完全在数据之外 | `items` 为空、`tracks` 仍然列出、`axis_*` 仍是整次启动的范围 |

区间（Span）只要与窗口**有交集**就返回，不是只看起点：一条从窗口之前开始、在窗口之中结束的区间
必须出现在结果里，否则界面会把「当时一直活着的服务」画成不存在。

`axis_start_ns` / `axis_end_ns` 是整次启动的数据范围，不受查询窗口影响，供界面画总览与夹紧滑块；
`window_*_ns` 回显本次查询实际使用的范围。

## 7. 指定时刻的状态

`state_at(machine_id, boot_id, monotonic_ns)` 返回该时刻每条轨道的状态：

| 字段 | 含义 |
| --- | --- |
| `status` / `unavailable_reason` | 同第 4 节，描述整条轨道的可用性 |
| `metric` | 该时刻或之前最近的一个采样点；没有则为 `null` |
| `spans` | 该时刻正在进行的区间（可能有多条） |
| `last_event` | 该时刻或之前最近的一个事件；没有则为 `null` |

采样的取法遵守 `docs/time-base.md` 第 7.2 节：两次采集时间重叠时，只取最靠近游标的那一条，不把两条
都返回。时刻早于全部数据时，三个字段都是空，但轨道仍然列出。

区间结束时刻未知（只有墙钟收尾、或根本没收尾）时，按「从开始起一直有效」处理。

## 8. 统一时间游标

`cursor(machine_id, boot_id, at_ns=None)` 返回 `TimeCursor`：

| 字段 | 含义 |
| --- | --- |
| `start_ns` / `end_ns` | 这条轴覆盖的范围 |
| `position_ns` | 当前位置；`at_ns` 缺省为 `start_ns`，超出范围时夹到起止点 |
| `previous_ns` | 位置之前最近的一个数据时刻；已在起点时为 `null` |
| `next_ns` | 位置之后最近的一个数据时刻；已在终点时为 `null` |

可停靠的时刻是数据发生变化的时刻：事件与采样点的时刻、区间的起点与**结束点**。区间结束也是一次
状态变化，所以也能停靠。

`step_cursor(cursor, forward=True)` 把游标挪到前后最近的可停靠时刻，已经在两端时原样返回。界面的
「上一帧 / 下一帧」用它实现，不需要自己在条目里找相邻时刻。

没有任何数据时（启动登记了但还没有导入），五个时刻字段都是 `null`，界面据此显示「没有可回放的数据」。

## 9. 只有墙钟的记录与漂移

- `clock_domain = wall_realtime` 的记录没有 boot 内位置，**不进入** `items` 与任何轨道，单独放在
  `wall_clock_items`，按 `realtime_ns` 排序（`docs/time-base.md` 第 2、10 节）。界面把它画成
  「位置未知」，不能挤进同一条轴。
- 时间范围是单调时间，与墙钟读数没有可比性，所以 `wall_clock_items` 不受 `start_ns` / `end_ns`
  过滤，整次启动一起返回。
- `clock` 字段给出 boot 级的启动锚点与漂移（`docs/time-base.md` 第 4.4 节）：锚点候选是 Boot 实体的
  `started_realtime_ns` 与每条同时带两个时钟的记录的 `realtime_ns - monotonic_ns`；`anchor_ns` 取
  中位数，`drift_ns` 取极差，`anchor_count` 是候选个数。
- 只取部分种类（`kinds`）时，锚点候选与漂移不跟着筛选变化：它们始终按整次启动的全部记录计算。
- `clock.unreliable` 是建议阈值：`drift_ns` 超过 1 秒时为真。此时墙钟换算只用于显示时间文字，
  不用于跨启动比较。下游可以按自己的口径覆盖这个判断，本文档不强制。

本任务**不返回**换算后的墙钟时刻：`realtime_ns` 原样透出，换算与误差展示由 4.2 按记录所属
collection 的锚点自己算（`docs/time-base.md` 第 7.1 节：墙钟换算按 collection 各算各的）。

## 10. Web 怎么用

时间线界面（4.2）：

1. `tracks(machine_id, boot_id)` 拿到全部轨道与整条轴，画总览。
2. 拖动或缩放时用 `tracks(..., start_ns=..., end_ns=...)` 只取可见范围；`axis_*` 不变，滑块不用重算。
3. 每条轨道按 `status` 决定画数据、画「不可用：原因」还是画缺口；`unavailable` 与 `no_data` 都不能
   画成 0。
4. `items` 已经是排好的同一条轴，可以直接映射到画布横坐标；区间用 `end_monotonic_ns` 决定长度，
   `end_is_declared = false` 的区间末端画成开放端并标注「未闭合」。
5. `wall_clock_items` 单独画成一条「位置未知」轨道。

回放（4.3）：

1. `cursor(...)` 取游标，前后帧用 `step_cursor(...)`。
2. 每停一次用 `state_at(machine_id, boot_id, cursor.position_ns)` 取该时刻每条轨道的状态。
3. 两次采集时间重叠的区间只画最靠近游标的一条，接口已经这样做，界面不需要再去重。

游标是单调时间上的位置，与墙钟文字无关：显示时间文字时按记录所属 collection 的锚点换算，并参考
`clock.unreliable`。

## 11. 已知限制与偏差

1. **`available` 的多次采集优先级未定稿**：见第 4 节的读取口径，读侧按「有记录就显示数据」处理，库中只保留
   最早写入的那份流描述。这是 0.2 未定义的已知限制，留给后续版本。
2. **不返回换算后的墙钟时刻与量化误差**：`docs/time-base.md` 第 4.4 节把量化误差的字段名交给 1.5
   定，但 0.2 的记录里没有精度字段（Journal 微秒精度只体现在读数本身），读侧无法判断某条读数是否
   只有微秒精度。因此本任务只返回 boot 级锚点与漂移，不返回逐条记录的量化误差；换算留给 4.2，
   由它按 collection 的锚点计算。
3. **时间范围筛在引擎里做，不下推给 SQL**：开放区间的收尾要求看到整次采集的记录（第 5 节），而
   `DiagnosticStore` 的现有接口没有「按采集取窗口终点」的聚合方法，本任务又不允许改存储层。因此引擎
   读整次启动的记录在内存里筛窗口。`DiagnosticStore` 已经有范围筛选能力，后续若在存储侧补一个窗口
   聚合，可以改为下推而不影响本接口。
   同一原因导致每次调用都重新读整次启动的记录：回放每帧调一次 `state_at`，开销按「帧数 × 记录数」
   增长。按启动缓存留到 1.6 定接口时一起决定——缓存需要失效钩子，导入完成后数据会变，现在加一层
   进程内缓存没有可靠的失效点。
4. **本任务不产出 Profile、Annotation、Artifact 轨道**：它们的展示属于 2.x 与 4.x，`kinds` 传入这三类
   会报错，不会静默丢弃。
5. **只做了单启动视图**：跨启动对比按 `docs/time-base.md` 第 3.2 节应当「各分一组并排」，本任务只保证
   单次启动内正确，不做跨启动的组合查询。4.2 要为两次启动并排时，分别查两次再并排放。
