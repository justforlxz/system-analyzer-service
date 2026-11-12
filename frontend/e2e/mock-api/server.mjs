// E2E 用的假后端：按真实 FastAPI 的响应形状提供时间线三个接口。
//
// 响应来自 `fixtures/` 里从真实后端抓下来的快照（三份样例各自单独导入后抓取，见
// `docs/timeline-ui.md` 的「复跑验证」一节）。这里只补两件真实后端本来会做的事：
//   - 按 `start_ns` / `end_ns` 过滤条目并回显窗口；
//   - 从条目算出可停靠时刻，派生 `cursor` 与 `state` 的响应。
// 另外给 `boot-resources` 追加一条 `no_data` 轨道：样例里没有「声明可用但没有任何采样」
// 的数据，这个组合只能由导入方造出来，这里显式补上并标注，方便 E2E 覆盖「无数据」文案。
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

const server = createServer((request, response) => {
  const url = new URL(request.url ?? "/", `http://127.0.0.1:${port}`);
  const path = url.pathname;

  if (path === "/api/health") {
    sendJson(response, 200, { status: "ok", version: "mock" });
    return;
  }
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
});

server.listen(port, "127.0.0.1", () => {
  console.log(`mock timeline api listening on http://127.0.0.1:${port}`);
});
