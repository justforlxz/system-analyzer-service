# 启动过程时间线回放界面

本文说明任务 4.2（MKACG-782）交付的时间线回放界面：多条轨道共用一个时间游标，能缩放、拖动、
定位关键事件，并把「有数据」「不可用」「无数据」如实分开显示。

- 入口页面：`frontend/src/app/timeline/page.tsx`（Server Component）。
- 交互组件：`frontend/src/components/timeline/`（Client Component）。
- 时间语义与接口口径以 `docs/timeline-query.md`、`docs/time-base.md`、`docs/data-model.md` 为准，
  本文只说明界面怎么用这些数据，不重新定义时间规则。
- 界面不重算时间：窗口值、游标位置、条目时刻全部使用后端返回的 `axis_*`、`window_*`、
  `monotonic_ns`、`position_ns` 等字段，前端只做像素映射与文字格式化。

目录：

1. 页面入口与选择启动
2. 数据来源
3. 统一时间游标
4. 轨道与绘制方式
5. 三种轨道状态与资源声明
6. 缩放、拖动与定位关键事件
7. 时间值的显示口径
8. 同源转发与部署配置
9. 测试与复跑
10. 已知限制与偏差

## 1. 页面入口与选择启动

| 地址                                     | 行为                                                                |
| ---------------------------------------- | ------------------------------------------------------------------- |
| `/timeline`                              | 列出已经导入完成的采集，按「机器 + 启动」分组；点机器名进入该次启动 |
| `/timeline?machine_id=<id>&boot_id=<id>` | 直接打开这次启动的时间线回放                                        |

首页 `/` 有一个指向 `/timeline` 的链接。页面本身是 Server Component：目标选择、首屏的轨道数据
与初始游标都在服务端取好再交给 Client Component；后续的缩放、拖动、换帧在浏览器里直接请求
FastAPI。

## 2. 数据来源

三个接口全部来自任务 1.6 的实现（见 `docs/import-and-query-api.md` 第 5 节）：

| 用途               | 接口                                                       | 界面怎么用                                               |
| ------------------ | ---------------------------------------------------------- | -------------------------------------------------------- |
| 首屏与每次窗口变化 | `GET /api/machines/{machine}/boots/{boot}/timeline/tracks` | 取轨道、条目、`axis_*`、`window_*`、`resources`、`clock` |
| 游标定位与换帧     | `GET .../timeline/cursor`                                  | `at_ns` 定位；带 `forward=true/false` 前进或后退一帧     |
| 游标所在时刻的读数 | `GET .../timeline/state?monotonic_ns=`                     | 每条轨道「最近采样点 / 进行中的区间 / 最近事件」         |

数据请求封装在 `frontend/src/lib/api/client.ts`，类型直接来自 `src/lib/api/schema.d.ts`（由
`npm run api:generate` 生成，未手改）。

首屏不取整条轴：页面先读 `cursor` 拿到轴范围，再以轴末尾 60 秒作为初始窗口请求 `tracks`，
并传 `include_attributes=false`。真实采集的 Journal 可能覆盖数天，整条轴会有数万条事件；
默认窗口让首屏先落在采集窗口附近，轴范围仍完整显示，用户可以自行缩放或点「全览」。
初始游标也放在这个窗口的起点，避免打开页面就看到「游标在窗口之外」。

## 3. 统一时间游标

- 界面上只有**一条**游标线（`data-testid="shared-cursor"`），横跨所有轨道；位置由后端
  `cursor.position_ns` 决定，换算方式是 `(position_ns - window_start_ns) / (window_end_ns - window_start_ns)`。
- 换帧按钮调用 `cursor(at_ns=当前位置, forward=...)`，前进/后退到下一个/上一个可停靠时刻；
  可停靠时刻由后端给出（事件与采样点时刻、区间起点与结束点），界面不自己找相邻记录。
- 游标变化后立刻请求 `state`，把每条轨道在该时刻的读数写进轨道标签（指标值、进行中的区间状态、
  最近事件类型）。所有轨道读的是同一个 `position_ns`，因此拖动时同步刷新。
- 游标被拖到当前窗口之外时，界面上不画游标线并提示「游标在窗口之外」，同时提供「把窗口移到游标」
  按钮；不把游标悄悄夹到窗口边缘，避免显示一个和真实位置不同的时间。

## 4. 轨道与绘制方式

轨道顺序、轨道之间的分组只是显示组织，内容与顺序完全按后端返回（`track_id` 升序）：

| 分组                  | 记录种类 | 绘制方式                                                 |
| --------------------- | -------- | -------------------------------------------------------- |
| Systemd Unit 生命周期 | `span`   | 自绘 SVG 区间条（`span-track.tsx`）                      |
| Journal 事件          | `event`  | 自绘 SVG 竖线标记（`event-track.tsx`），严重级别决定颜色 |
| 资源采样              | `metric` | Apache ECharts 折线（`metric-track-chart.tsx`）          |

- 生命周期轨道：一条区间一个色块；`state` 为 `active` / `failed` / 其它分别用青、红、灰。
  `end_monotonic_ns` 为空的区间右端画虚线并标注「未闭合 N 段」，悬浮提示写明是「采集结束时仍
  在进行」还是「只有墙钟收尾」——两种情况都由后端字段区分，前端不改写。
- 事件轨道：按 `monotonic_ns` 落在轴上，悬浮提示给出 boot 内偏移、墙钟时间、`event_type` 与消息。
- 资源轨道：ECharts 折线的横轴范围直接用后端给的窗口，隐藏坐标轴文字；量纲与当前读数写在左侧
  轨道标签里。这样三条资源轨道与生命周期、事件轨道的绘图区宽度完全一致，共用的游标线才能一条
  直线贯穿到底。
- 资源轨道与其它轨道共用同一个左边界（标签列固定宽度 `TRACK_LABEL_WIDTH = 240`）。
- 每个分组默认只渲染前 20 条在当前窗口里有数据的轨道；其余轨道用「显示更多轨道（还有 N 条）」
  展开。没有数据的可用轨道不占一行，不可用与无数据的指标流仍然显示，因为那本身就是需要如实
  展示的状态。
- 资源曲线的悬浮提示先把文字转义再交给 ECharts（`escapeHtml`）：单位与指标名来自导入的
  `.bootrec`，导入校验不限制字符集，而 ECharts 把 `tooltip.formatter` 的返回值按 HTML 插入。

## 5. 三种轨道状态与资源声明

轨道状态由后端给出（`status`），界面只负责呈现，不重算：

| `status`      | 轨道标签 | 绘图区                                                      |
| ------------- | -------- | ----------------------------------------------------------- |
| `available`   | 有数据   | 正常画；本窗口内没有点时留空，不画 0                        |
| `unavailable` | 不可用   | 虚线占位块，文字为 `不可用：<后端给出的原因>`               |
| `no_data`     | 无数据   | 虚线占位块，文字为 `无数据：这次启动里没有这条流的任何记录` |

「不可用」与「无数据」在文案、底色（琥珀 / 灰）和 `data-status` 上都不同，任何情况下都不把缺失
画成 0。

整次启动没有资源数据（例如 Journal-only 导入）不是轨道状态，而是 `resources.available = false`：
界面在轨道上方显示「资源数据不可用」面板并逐条列出 `resources.reasons`，此时根本不会出现资源
轨道，也不会画空图。

## 6. 缩放、拖动与定位关键事件

| 操作                       | 行为                                                              | 请求                                                 |
| -------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------- |
| 「放大」/「缩小」          | 以游标为锚点把跨度减半 / 加倍（游标不在窗口内时以窗口中心为锚点） | `tracks(start_ns, end_ns)`                           |
| 「全览」                   | 窗口恢复为整条轴                                                  | `tracks(start_ns, end_ns)` 或直接用后端回显          |
| 按住轨道左右拖动           | 平移窗口，超出轴时贴边；移动超过 3 像素才算拖动                   | `tracks(start_ns, end_ns)`（连续拖动合并为一次请求） |
| 在轨道上单击               | 把游标定位到点击处对应的时刻                                      | `cursor(at_ns=...)`                                  |
| 「上一帧」/「下一帧」      | 游标移动到前后最近的可停靠时刻                                    | `cursor(at_ns=..., forward=...)`                     |
| 点「关键事件」列表中的一条 | 游标定位到该事件的 `monotonic_ns`                                 | `cursor(at_ns=...)`                                  |

缩放下限是 1 毫秒（`MIN_WINDOW_NS`），上限是整条轴；缩放与平移都夹在 `axis_*` 之内，界面上的
窗口值始终是后端回显的 `window_start_ns` / `window_end_ns`。

「关键事件」列表最多渲染当前窗口的前 200 条，超过时在列表末尾说明总数并提示缩小窗口查看更具体
的事件。事件轨道本身仍按当前窗口绘制全部事件，列表上限只限制可点击的按钮数量。

## 7. 时间值的显示口径

页头有时间对照区，逐项显示后端字段的原值，便于核对：

| 显示项          | 来源字段                                      | 写法                                                       |
| --------------- | --------------------------------------------- | ---------------------------------------------------------- |
| 轴范围          | `axis_start_ns` / `axis_end_ns`               | 整数原样（不加千位分隔符）+ 时长                           |
| 当前窗口        | `window_start_ns` / `window_end_ns`           | 整数原样 + 时长                                            |
| 游标            | `position_ns`                                 | 整数原样 + `T+1.200 s` 形式的 boot 内偏移 + 墙钟文字       |
| 上一帧 / 下一帧 | `previous_ns` / `next_ns`                     | 整数原样                                                   |
| 启动锚点与漂移  | `clock.anchor_ns` / `drift_ns` / `unreliable` | 整数原样 + 漂移时长；超过 1 秒时注明墙钟只用于显示时间文字 |

- `monotonic_ns` 是整数纳秒，界面原样显示，不做四舍五入，便于和后端响应逐项对照。
- 墙钟文字按 UTC 显示（`YYYY-MM-DD HH:MM:SS.mmm UTC`）：墙钟读数来自目标机器，换成浏览器本地
  时区会把「目标机器几点」和「我这里几点」混在一起。
- 墙钟文字使用 boot 级锚点 `clock.anchor_ns + monotonic_ns`（见第 10 节偏差 1）。

## 8. 同源转发与部署配置

后端没有开放跨域。浏览器端的所有请求都发到本域的 `/api/...`，由 `frontend/next.config.ts` 的
`rewrites` 转发到 `NEXT_PUBLIC_API_BASE_URL` 指向的 FastAPI：

- Server Component 直接用绝对地址请求 FastAPI（服务端没有「页面来源」）。
- Client Component 用同源路径，浏览器不跨域，因此不需要后端加 CORS 配置。
- Next.js 只转发字节，不实现业务逻辑，也没有新增 Route Handler。

注意转发目标写在 Next.js 的配置里：

- `next dev` 每次启动读取环境变量 `NEXT_PUBLIC_API_BASE_URL`。
- `next build` / `next start` 在构建时把它写进路由清单，因此生产环境要在**构建时**设置该变量。

`allowedDevOrigins: ["127.0.0.1", "localhost"]` 也在这个文件里：Next.js 16 默认只允许 `localhost`
访问开发期资源，用 `127.0.0.1:3000` 打开页面时客户端脚本会被拦下，界面只剩服务端渲染的静态
HTML、完全没有交互。

## 9. 测试与复跑

```bash
cd frontend && npm ci && npm run lint && npm run typecheck && npm run test && npm run build
```

```bash
cd frontend && npx playwright install chromium && npm run test:e2e
```

- 单元测试（Vitest）：`src/lib/timeline/*.test.ts` 覆盖像素映射、缩放/平移夹紧、时间格式化、
  轨道分组与状态文案、ECharts 配置；`src/components/timeline/timeline-replay.test.tsx` 覆盖
  共用游标、换帧、缩放、拖动、事件定位、无数据/不可用/资源不可用、空数据与错误提示。
- 端到端测试（Playwright）：`frontend/e2e/timeline.spec.ts`。E2E 不依赖真实后端：
  `e2e/mock-api/server.mjs` 用 `e2e/mock-api/fixtures/` 里从真实后端抓下来的响应起一个假 API，
  Playwright 在两份 `webServer` 配置里先起假 API、再用它作为 `NEXT_PUBLIC_API_BASE_URL` 起 Next.js。
  fixtures 是全量、Journal-only、资源缺失三份样例各自**单独**导入后抓取的快照（三份样例共用
  同一个 `boot_id`，同时导入会合并）；第四份 `no-data.bootrec` 没有抓取 fixtures，只在导入
  验证里使用。

用真实后端复跑的步骤：

```bash
scripts/dev-db.sh start
cd backend && cp -n .env.example .env && uv run alembic upgrade head
SAS_API_PORT=8010 SAS_UPLOAD_DIR=var/uploads uv run sas-api      # 另一个终端
SAS_UPLOAD_DIR=var/uploads uv run sas-worker                     # 另一个终端

# 依次单独导入四份样例（每次导入前清空上一次的数据，四份样例的 boot_id 相同）
curl -s -F "file=@docs/samples/bootrec-v1/full.bootrec"             http://127.0.0.1:8010/api/imports
curl -s -F "file=@docs/samples/bootrec-v1/journal-only.bootrec"     http://127.0.0.1:8010/api/imports
curl -s -F "file=@docs/samples/bootrec-v1/resources-missing.bootrec" http://127.0.0.1:8010/api/imports
curl -s -F "file=@docs/samples/bootrec-v1/no-data.bootrec"          http://127.0.0.1:8010/api/imports
```

```bash
cd frontend && NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8010 npm run dev
# 打开 http://127.0.0.1:3000/timeline ，选择 machine:uos-PC 的那一次启动
```

## 10. 已知限制与偏差

1. **墙钟文字用 boot 级锚点**。`docs/timeline-query.md` 第 10 节把「按 collection 各算各的锚点」
   交给 4.2，但 1.5 只返回 boot 级 `clock.anchor_ns`；boot 内只有一个全局游标，它不属于任何一次
   采集，按 collection 分别换算没有确定答案。因此界面统一用 `clock.anchor_ns`，并在漂移超过
   1 秒时提示墙钟只用于显示时间文字。
2. **资源曲线不画坐标轴文字**。Y 轴量纲与游标处读数写在轨道标签里，绘图区与其它轨道同宽。这是
   显示选择，不改变任何数据。
3. **新增依赖 `echarts`**。父 issue 的技术栈已指定 Apache ECharts 用于 CPU/内存/磁盘 I/O 图表。
4. **新增 Next.js `rewrites` 同源转发**。后端没有开放跨域；改用同源转发后浏览器端不需要后端改
   CORS，也没有引入 Route Handler。代价是生产环境构建时必须给出
   `NEXT_PUBLIC_API_BASE_URL`（见第 8 节）。
5. **新增 `allowedDevOrigins`**。见第 8 节；只影响开发服务器，不影响生产行为。
6. **只做单启动视图**。`docs/timeline-query.md` 第 11 节第 5 条把「两次启动并排」留给后续版本，
   本任务不做。
7. **E2E 的 fixtures 是抓取快照**，不是实时后端。其中 `boot-resources` 额外追加了一条 `no_data`
   轨道：抓取 fixtures 时用的三份样例里没有「流声明可用但一个采样点都没有」的组合；现在第四份
   样例 `docs/samples/bootrec-v1/no-data.bootrec` 可以复现这个状态，假后端仍显式补上，仅用于
   稳定覆盖「无数据」的显示。
