// E2E 用的假后端：按真实 FastAPI 的响应形状提供时间线、机器管理、采集任务与导入接口。
//
// 时间线部分的响应来自 `fixtures/` 里从真实后端抓下来的快照（三份样例各自单独导入后抓取，
// 见 `docs/timeline-ui.md` 的「复跑验证」一节）。这里只补真实后端本来会做的事：按时间窗口
// 过滤条目、派生 cursor 与 state、给 `boot-resources` 追加一条 `no_data` 轨道。
//
// 机器、采集任务与导入是状态化的内存假实现（任务 4.1 的页面要用到「状态变化」与「取消」）：
//   - 目标机器的连接测试结果由 host 里的关键字决定，四类失败各有一个 host；
//   - 采集任务每被读取一次就往前走一步：排队中 → 执行中 → 成功 / 失败；取消后下一次读取
//     给出 `cancelled` 的尝试记录；
//   - 导入任务同样按读取次数推进，文件名里带 `bad` 的会在最后失败，用来覆盖失败原因文案。
// `POST /api/_reset` 把这三份内存状态清空，测试之间互不影响。
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.MOCK_API_PORT ?? 3110);

function load(name) {
  return JSON.parse(readFileSync(join(here, "fixtures", `${name}.json`), "utf8"));
}

const machine = load("machine");

const NO_DATA_TRACK = {
  track_id: "metric:resource-sampler:load:metric:load.1min",
  kind: "metric",
  entity: "metric:load.1min",
  stream_id: "load",
  source_plugin: "resource-sampler",
  status: "no_data",
  unavailable_reason: null,
  name: "load.1min",
  unit: "count",
  items: [],
};

const boots = new Map(
  [
    ["boot-full", "full", []],
    ["boot-journal-only", "journal-only", []],
    ["boot-resources", "resources-missing", [NO_DATA_TRACK]],
  ].map(([bootId, fixture, extraTracks]) => {
    const tracks = load(`${fixture}-tracks`);
    const collections = load(`${fixture}-collections`);
    return [
      bootId,
      {
        bootId,
        tracks: { ...tracks, boot_id: bootId, tracks: [...tracks.tracks, ...extraTracks] },
        collections: collections.items,
        resources: tracks.resources,
        clock: tracks.clock,
        axisStart: tracks.axis_start_ns,
        axisEnd: tracks.axis_end_ns,
        machineId: (collections.items[0] ?? machine).machine_id,
      },
    ];
  }),
);

// ----------------------------------------------------------------------
// 机器管理 / 采集任务 / 导入：内存状态
// ----------------------------------------------------------------------

const SYSTEM_INFO = {
  hostname: "uos-PC",
  os_release: "UOS Desktop 25 Professional",
  kernel_version: "6.6.0-amd64-desktop",
  architecture: "x86_64",
  cpu_count: 16,
  memory_total_bytes: 16397991936,
  boot_id: "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9",
};

let targets = [];
let tasks = [];
const imports = new Map();
let collectionsMode = "fixtures";
let targetSeq = 0;
let taskSeq = 0;
let importSeq = 0;
let clockTick = 0;

/** 固定形状的时间戳：每次调用往后走一秒，输出因此可预测。 */
function stamp() {
  clockTick += 1;
  const seconds = String(clockTick % 60).padStart(2, "0");
  return `2026-01-01T00:00:${seconds}.000000+00:00`;
}

function resetState() {
  targets = [];
  tasks = [];
  imports.clear();
  collectionsMode = "fixtures";
  targetSeq = 0;
  taskSeq = 0;
  importSeq = 0;
  clockTick = 0;
}

const ENV_VAR_PATTERN = /^[A-Z][A-Z0-9_]*$/;

function invalid(message, details) {
  return { status: 422, body: { error: { type: "invalid_parameter", message, details } } };
}

/** 目标机器的新增/替换校验：与后端 `SshTargetDraft` 的规则一致。 */
function validateDraft(body) {
  const details = [];
  const host = typeof body.host === "string" ? body.host.trim() : "";
  const username = typeof body.username === "string" ? body.username.trim() : "";
  const port = body.port === undefined ? 22 : Number(body.port);
  const passwordEnvVar =
    body.password_env_var === undefined ? "SAS_SSH_PASSWORD" : String(body.password_env_var).trim();
  if (!host) details.push("body.host: 不能为空或只包含空白字符");
  if (!username) details.push("body.username: 不能为空或只包含空白字符");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    details.push("body.port: 端口必须在 1 到 65535 之间");
  }
  if (!ENV_VAR_PATTERN.test(passwordEnvVar)) {
    details.push("body.password_env_var: 必须是环境变量名（大写字母、数字、下划线，且以字母开头）");
  }
  if (details.length > 0) return { error: invalid("请求参数不合法", details) };
  return { draft: { host, username, port, password_env_var: passwordEnvVar } };
}

function targetJson(target) {
  return { ...target };
}

function testConnection(host) {
  if (host.includes("auth-fail")) {
    return {
      ok: false,
      system: null,
      failure: { kind: "auth", message: `建立 SSH 连接认证失败：${host}:22 拒绝了用户名或口令` },
    };
  }
  if (host.includes("timeout")) {
    return {
      ok: false,
      system: null,
      failure: { kind: "timeout", message: `建立 SSH 连接超时：${host}:22 1 秒内没有完成` },
    };
  }
  if (host.includes("connect-fail")) {
    return {
      ok: false,
      system: null,
      failure: {
        kind: "connect",
        message: `建立 SSH 连接失败：${host}:22 不可达（Connection refused）`,
      },
    };
  }
  if (host.includes("no-credential")) {
    return {
      ok: false,
      system: null,
      failure: {
        kind: "credential",
        message: "运行环境里没有配置 SAS_SSH_PASSWORD，无法解析目标机器口令",
      },
    };
  }
  return { ok: true, system: SYSTEM_INFO, failure: null };
}

function attemptOf(status, phaseStatus, detail) {
  const finished = status !== "running";
  return {
    attempt: 1,
    status,
    started_at: "2026-01-01T00:00:00.000000+00:00",
    finished_at: finished ? "2026-01-01T00:00:10.000000+00:00" : null,
    phases: [
      {
        phase: "pre",
        status: status === "running" ? "running" : phaseStatus,
        started_at: "2026-01-01T00:00:00.000000+00:00",
        finished_at: null,
        detail: null,
      },
      {
        phase: "in_progress",
        status: status === "running" ? "pending" : phaseStatus,
        started_at: null,
        finished_at: null,
        detail,
      },
      {
        phase: "post",
        status: status === "running" ? "pending" : "succeeded",
        started_at: null,
        finished_at: null,
        detail: null,
      },
    ],
    events: [
      {
        at: "2026-01-01T00:00:05.000000+00:00",
        kind: status === "failed" ? "phase_failed" : "phase_started",
        phase: "in_progress",
        step: "collect.cpu",
        detail,
      },
    ],
  };
}

function taskJson(task) {
  return {
    task_id: task.task_id,
    kind: "collect.target",
    status: task.status,
    attempts: task.attempts,
    max_attempts: 3,
    created_at: task.created_at,
    updated_at: task.updated_at,
    target_id: task.target_id,
    run_id: task.run_id,
    cancel_requested: task.cancel_requested,
    latest_attempt: task.latest_attempt,
    failure: task.failure,
  };
}

/** 任务每被读取一次往前走一步：排队中 → 执行中 → 成功 / 失败（取消则直接结束）。 */
function advanceTasks() {
  for (const task of tasks) {
    if (task.finished) continue;
    if (task.cancel_requested) {
      task.status = "succeeded";
      task.latest_attempt = attemptOf("cancelled", "cancelled", null);
      task.failure = null;
      task.finished = true;
      continue;
    }
    task.reads = (task.reads ?? 0) + 1;
    if (task.reads === 1) {
      task.status = "running";
      task.attempts = 1;
      task.latest_attempt = attemptOf("running", "running", "步骤 collect.cpu 正在执行");
      continue;
    }
    task.attempts = 1;
    if (task.fail) {
      task.status = "failed";
      task.latest_attempt = attemptOf(
        "failed",
        "failed",
        "步骤 collect.cpu 失败：远端命令以退出码 1 结束",
      );
      task.failure = {
        phase: "in_progress",
        message: "步骤 collect.cpu 失败：远端命令以退出码 1 结束",
      };
    } else {
      task.status = "succeeded";
      task.latest_attempt = attemptOf("succeeded", "succeeded", null);
      task.failure = null;
    }
    task.finished = true;
  }
}

function importJson(job) {
  return {
    job: {
      id: job.id,
      kind: "import.source",
      status: job.status,
      attempts: job.attempts,
      max_attempts: 3,
      failure: job.failure,
      plugin_id: job.plugin_id,
      upload_id: job.upload_id,
      filename: job.filename,
      result: job.result,
    },
    collection: job.collection,
  };
}

/** 导入任务同样按读取次数推进：排队中 → 执行中 → 成功 / 失败。 */
function advanceImport(job) {
  if (job.status === "succeeded" || job.status === "failed") return job;
  job.reads = (job.reads ?? 0) + 1;
  if (job.reads === 1) return job;
  if (job.reads === 2) {
    job.status = "running";
    job.attempts = 1;
    return job;
  }
  job.attempts = 1;
  if (job.bad) {
    job.status = "failed";
    job.failure = {
      kind: "invalid_input",
      message: "归档里没有 _HOSTNAME，无法确定 machine_id",
    };
    return job;
  }
  job.status = "succeeded";
  job.result = {
    collection_id: "collection:mock-import",
    import_status: "imported",
    records_written: 128,
    records_skipped: 0,
    skipped_reason: null,
  };
  job.collection = {
    collection_id: "collection:mock-import",
    machine_id: "machine:uos-PC",
    boot_id: "boot-mock-import",
    mode: "journal_import",
    created_realtime_ns: 1767225600000000000,
    import_status: "imported",
    import_error: null,
    source_sha256: "0".repeat(64),
    resources_available: false,
    resources_reason: "Journal-only 数据不包含资源指标",
  };
  return job;
}

// ----------------------------------------------------------------------
// 时间线假实现（4.2 交付）
// ----------------------------------------------------------------------

function stopTimesOf(boot) {
  const times = new Set();
  for (const item of boot.tracks.items) {
    if (item.monotonic_ns !== null) times.add(item.monotonic_ns);
    if (item.type === "span" && item.end_monotonic_ns !== null) times.add(item.end_monotonic_ns);
  }
  return [...times].sort((a, b) => a - b);
}

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

function intersects(item, startNs, endNs) {
  const start = item.monotonic_ns;
  if (start === null) return false;
  if (item.type !== "span") return start >= startNs && start <= endNs;
  const end = item.end_monotonic_ns ?? endNs;
  return end >= startNs && start <= endNs;
}

function tracksResponse(boot, url) {
  const startParam = url.searchParams.get("start_ns");
  const endParam = url.searchParams.get("end_ns");
  const startNs = startParam === null ? null : Number(startParam);
  const endNs = endParam === null ? null : Number(endParam);
  const inWindow = (item) => {
    if (startNs === null && endNs === null) return true;
    const low = startNs ?? Number.NEGATIVE_INFINITY;
    const high = endNs ?? Number.POSITIVE_INFINITY;
    return intersects(item, low, high);
  };
  const tracks = boot.tracks.tracks.map((track) => ({
    ...track,
    items: track.items.filter(inWindow),
  }));
  const items = boot.tracks.items.filter(inWindow);
  return {
    ...boot.tracks,
    window_start_ns: startNs,
    window_end_ns: endNs,
    tracks,
    items: items.sort(
      (a, b) =>
        (a.monotonic_ns ?? 0) - (b.monotonic_ns ?? 0) || a.record_id.localeCompare(b.record_id),
    ),
  };
}

function cursorResponse(boot, url) {
  const times = stopTimesOf(boot);
  const atParam = url.searchParams.get("at_ns");
  const forwardParam = url.searchParams.get("forward");
  const base = {
    machine_id: boot.machineId,
    boot_id: boot.bootId,
    start_ns: times.length > 0 ? times[0] : null,
    end_ns: times.length > 0 ? times.at(-1) : null,
    position_ns: null,
    previous_ns: null,
    next_ns: null,
  };
  if (times.length === 0) return base;
  const requested = atParam === null ? times[0] : clamp(Number(atParam), times[0], times.at(-1));
  const neighbour = (from, forward) => {
    const candidates = forward
      ? times.filter((time) => time > from)
      : times.filter((time) => time < from).reverse();
    return candidates.length > 0 ? candidates[0] : null;
  };
  let position = requested;
  if (forwardParam !== null) {
    const target = neighbour(requested, forwardParam === "true");
    if (target !== null) position = target;
  }
  return {
    ...base,
    position_ns: position,
    previous_ns: neighbour(position, false),
    next_ns: neighbour(position, true),
  };
}

function stateResponse(boot, url) {
  const moment = Number(url.searchParams.get("monotonic_ns"));
  return {
    machine_id: boot.machineId,
    boot_id: boot.bootId,
    monotonic_ns: moment,
    resources: boot.resources,
    clock: boot.clock,
    tracks: boot.tracks.tracks.map((track) => {
      let metric = null;
      let lastEvent = null;
      const spans = [];
      for (const item of track.items) {
        if (item.type === "metric" && item.monotonic_ns !== null && item.monotonic_ns <= moment) {
          metric = item;
        }
        if (item.type === "event" && item.monotonic_ns !== null && item.monotonic_ns <= moment) {
          lastEvent = item;
        }
        if (item.type === "span" && item.monotonic_ns !== null && item.monotonic_ns <= moment) {
          if (item.end_monotonic_ns === null || moment <= item.end_monotonic_ns) spans.push(item);
        }
      }
      return {
        track_id: track.track_id,
        kind: track.kind,
        entity: track.entity,
        status: track.status,
        unavailable_reason: track.unavailable_reason,
        metric,
        spans,
        last_event: lastEvent,
      };
    }),
  };
}

function collectionsResponse(url) {
  const limit = Number(url.searchParams.get("limit") ?? "50");
  if (collectionsMode === "empty") {
    return { items: [], total: 0, limit, offset: 0 };
  }
  const items = [...boots.values()].map((boot) => {
    const first = boot.collections[0] ?? {};
    return {
      ...first,
      collection_id: `collection:${boot.bootId}`,
      machine_id: boot.machineId,
      boot_id: boot.bootId,
      mode: first.mode ?? "bootrec_import",
      import_status: "imported",
      resources_available: boot.resources.available,
      resources_reason: boot.resources.reasons[0] ?? null,
    };
  });
  return { items: items.slice(0, limit), total: items.length, limit, offset: 0 };
}

// ----------------------------------------------------------------------
// 路由
// ----------------------------------------------------------------------

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function notFound(response, message) {
  sendJson(response, 404, { error: { type: "not_found", message, details: [] } });
}

function conflict(response, message) {
  sendJson(response, 409, {
    error: { type: "invalid_parameter", message: message, details: [] },
  });
}

function readBody(request) {
  return new Promise((resolve) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

async function handle(request, response) {
  const url = new URL(request.url ?? "/", `http://127.0.0.1:${port}`);
  const path = url.pathname;
  const method = request.method ?? "GET";

  if (path === "/api/health") {
    sendJson(response, 200, { status: "ok", version: "mock" });
    return;
  }

  if (path === "/api/_reset" && method === "POST") {
    resetState();
    if (url.searchParams.get("collections") === "empty") collectionsMode = "empty";
    sendJson(response, 200, { status: "reset" });
    return;
  }

  // ---- 目标机器 ----
  if (path === "/api/targets" && method === "GET") {
    sendJson(response, 200, targets.map(targetJson));
    return;
  }
  if (path === "/api/targets" && method === "POST") {
    const parsed = validateDraft(JSON.parse((await readBody(request)).toString("utf8") || "{}"));
    if (parsed.error) {
      sendJson(response, parsed.error.status, parsed.error.body);
      return;
    }
    targetSeq += 1;
    const now = stamp();
    const target = {
      ...parsed.draft,
      target_id: `target-e2e-${targetSeq}`,
      created_at: now,
      updated_at: now,
    };
    targets = [...targets, target];
    sendJson(response, 201, targetJson(target));
    return;
  }

  const targetMatch = path.match(/^\/api\/targets\/([^/]+)$/);
  if (targetMatch) {
    const targetId = decodeURIComponent(targetMatch[1]);
    const index = targets.findIndex((item) => item.target_id === targetId);
    if (method === "GET") {
      if (index < 0) {
        notFound(response, `目标机器 ${targetId} 不存在`);
        return;
      }
      sendJson(response, 200, targetJson(targets[index]));
      return;
    }
    if (method === "PUT") {
      if (index < 0) {
        notFound(response, `目标机器 ${targetId} 不存在`);
        return;
      }
      const parsed = validateDraft(JSON.parse((await readBody(request)).toString("utf8") || "{}"));
      if (parsed.error) {
        sendJson(response, parsed.error.status, parsed.error.body);
        return;
      }
      const updated = {
        ...parsed.draft,
        target_id: targetId,
        created_at: targets[index].created_at,
        updated_at: stamp(),
      };
      targets = targets.map((item) => (item.target_id === targetId ? updated : item));
      sendJson(response, 200, targetJson(updated));
      return;
    }
    if (method === "DELETE") {
      if (index < 0) {
        notFound(response, `目标机器 ${targetId} 不存在`);
        return;
      }
      targets = targets.filter((item) => item.target_id !== targetId);
      response.writeHead(204);
      response.end();
      return;
    }
  }

  const testMatch = path.match(/^\/api\/targets\/([^/]+)\/test-connection$/);
  if (testMatch && method === "POST") {
    const targetId = decodeURIComponent(testMatch[1]);
    const target = targets.find((item) => item.target_id === targetId);
    if (!target) {
      notFound(response, `目标机器 ${targetId} 不存在`);
      return;
    }
    sendJson(response, 200, testConnection(target.host));
    return;
  }

  // ---- 采集任务 ----
  if (path === "/api/collection-tasks" && method === "POST") {
    const body = JSON.parse((await readBody(request)).toString("utf8") || "{}");
    const target = targets.find((item) => item.target_id === body.target_id);
    if (!target) {
      notFound(response, `目标机器 ${body.target_id} 不存在，无法创建采集任务`);
      return;
    }
    advanceTasks();
    taskSeq += 1;
    const task = {
      task_id: `task-e2e-${taskSeq}`,
      target_id: target.target_id,
      run_id: `run-e2e-${taskSeq}`,
      status: "queued",
      attempts: 0,
      created_at: stamp(),
      updated_at: stamp(),
      cancel_requested: false,
      latest_attempt: null,
      failure: null,
      fail: target.host.includes("collect-fail"),
    };
    tasks = [task, ...tasks];
    sendJson(response, 202, taskJson(task));
    return;
  }
  if (path === "/api/collection-tasks" && method === "GET") {
    advanceTasks();
    const targetId = url.searchParams.get("target_id");
    const status = url.searchParams.get("status");
    let items = tasks;
    if (targetId) items = items.filter((task) => task.target_id === targetId);
    if (status) items = items.filter((task) => task.status === status);
    sendJson(response, 200, {
      items: items.map(taskJson),
      total: items.length,
      limit: Number(url.searchParams.get("limit") ?? "50"),
      offset: Number(url.searchParams.get("offset") ?? "0"),
    });
    return;
  }

  const taskMatch = path.match(/^\/api\/collection-tasks\/([^/]+)$/);
  if (taskMatch && method === "GET") {
    advanceTasks();
    const taskId = decodeURIComponent(taskMatch[1]);
    const task = tasks.find((item) => item.task_id === taskId);
    if (!task) {
      notFound(response, `采集任务 ${taskId} 不存在`);
      return;
    }
    sendJson(response, 200, taskJson(task));
    return;
  }

  const cancelMatch = path.match(/^\/api\/collection-tasks\/([^/]+)\/cancel$/);
  if (cancelMatch && method === "POST") {
    const taskId = decodeURIComponent(cancelMatch[1]);
    const task = tasks.find((item) => item.task_id === taskId);
    if (!task) {
      notFound(response, `采集任务 ${taskId} 不存在`);
      return;
    }
    if (task.finished) {
      conflict(
        response,
        `采集任务 ${taskId} 当前状态是 ${task.status}，只有排队中或执行中的任务可以取消`,
      );
      return;
    }
    task.cancel_requested = true;
    task.updated_at = stamp();
    sendJson(response, 200, taskJson(task));
    return;
  }

  // ---- 导入 ----
  if (path === "/api/imports" && method === "POST") {
    const body = await readBody(request);
    const nameMatch = /filename="([^"]*)"/.exec(body.toString("latin1"));
    const filename = nameMatch ? nameMatch[1] : "upload.bin";
    importSeq += 1;
    const job = {
      id: `import-e2e-${importSeq}`,
      status: "queued",
      attempts: 0,
      failure: null,
      result: null,
      collection: null,
      plugin_id: filename.endsWith(".bootrec") ? "bootrec.importer" : "journal.archive",
      upload_id: `upload-e2e-${importSeq}`,
      filename,
      reads: 0,
      bad: filename.includes("bad"),
    };
    imports.set(job.id, job);
    sendJson(response, 202, {
      job_id: job.id,
      upload_id: job.upload_id,
      plugin_id: job.plugin_id,
      filename,
      source_sha256: "a".repeat(64),
      size_bytes: body.length,
    });
    return;
  }

  const importMatch = path.match(/^\/api\/imports\/([^/]+)$/);
  if (importMatch && method === "GET") {
    const jobId = decodeURIComponent(importMatch[1]);
    const job = imports.get(jobId);
    if (!job) {
      notFound(response, `任务 ${jobId} 不存在`);
      return;
    }
    sendJson(response, 200, importJson(advanceImport(job)));
    return;
  }

  // ---- 诊断记录与时间线 ----
  if (path === "/api/collections") {
    sendJson(response, 200, collectionsResponse(url));
    return;
  }
  const machineMatch = path.match(/^\/api\/machines\/([^/]+)$/);
  if (machineMatch) {
    const machineId = decodeURIComponent(machineMatch[1]);
    if (machineId !== machine.machine_id) {
      notFound(response, `机器 ${machineId} 不在库里`);
      return;
    }
    sendJson(response, 200, machine);
    return;
  }
  const timelineMatch = path.match(
    /^\/api\/machines\/([^/]+)\/boots\/([^/]+)\/timeline\/(tracks|cursor|state)$/,
  );
  if (timelineMatch) {
    const machineId = decodeURIComponent(timelineMatch[1]);
    const bootId = decodeURIComponent(timelineMatch[2]);
    const endpoint = timelineMatch[3];
    const boot = boots.get(bootId);
    if (!boot || boot.machineId !== machineId) {
      notFound(response, `启动 ${machineId}/${bootId} 不在库里，没有可回放的时间轴`);
      return;
    }
    if (endpoint === "tracks") {
      sendJson(response, 200, tracksResponse(boot, url));
      return;
    }
    if (endpoint === "cursor") {
      sendJson(response, 200, cursorResponse(boot, url));
      return;
    }
    if (url.searchParams.get("monotonic_ns") === null) {
      sendJson(response, 422, {
        error: { type: "invalid_parameter", message: "缺少 monotonic_ns", details: [] },
      });
      return;
    }
    sendJson(response, 200, stateResponse(boot, url));
    return;
  }

  notFound(response, `没有这个接口：${path}`);
}

const server = createServer((request, response) => {
  handle(request, response).catch((error) => {
    sendJson(response, 500, {
      error: { type: "internal_error", message: String(error), details: [] },
    });
  });
});

server.listen(port, "127.0.0.1", () => {
  console.log(`mock api listening on http://127.0.0.1:${port}`);
});
