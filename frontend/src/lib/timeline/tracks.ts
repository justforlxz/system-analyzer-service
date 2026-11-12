import type { SpanItem, TrackResponse, TrackStateResponse, TracksResponse } from "@/lib/api/client";
import { formatMetricValue } from "@/lib/timeline/format";
import type { WindowRange } from "@/lib/timeline/scale";
import { nsToFraction } from "@/lib/timeline/scale";

/** 轨道在界面上的分组。分组只决定排列顺序与标题，不改变后端给的轨道内容与顺序。 */
export type TrackGroupKey = "span" | "event" | "metric";

export type TrackGroup = {
  key: TrackGroupKey;
  title: string;
  description: string;
  tracks: TrackResponse[];
};

const GROUP_TITLES: Record<TrackGroupKey, { title: string; description: string }> = {
  span: { title: "Systemd Unit 生命周期", description: "每条区间是一段持续状态" },
  event: { title: "Journal 事件", description: "日志与 systemd 事件按时刻排列" },
  metric: { title: "资源采样", description: "CPU、内存、磁盘 I/O 曲线" },
};

const GROUP_ORDER: TrackGroupKey[] = ["span", "event", "metric"];

/** 按记录种类分组；组内保留后端返回的顺序（`track_id` 升序）。 */
export function groupTracks(tracks: readonly TrackResponse[]): TrackGroup[] {
  return GROUP_ORDER.map((key) => ({
    key,
    ...GROUP_TITLES[key],
    tracks: tracks.filter((track) => track.kind === key),
  })).filter((group) => group.tracks.length > 0);
}

/** 轨道的显示名：指标用后端给的 `name`，其它用实体名去掉前缀。 */
export function trackLabel(track: TrackResponse): string {
  if (track.name) return track.name;
  const entity = track.entity;
  if (entity) {
    const separator = entity.indexOf(":");
    return separator >= 0 ? entity.slice(separator + 1) : entity;
  }
  return track.stream_id ?? track.track_id;
}

/** 轨道来源的次要说明，界面上作为小字。 */
export function trackSourceLabel(track: TrackResponse): string {
  const stream = track.stream_id ?? "未标流";
  const plugin = track.source_plugin ?? "未标来源";
  return `${stream} · ${plugin}`;
}

export type TrackStatusView = {
  key: TrackResponse["status"];
  badge: string;
  text: string;
  tone: "ok" | "muted" | "warn";
};

/** 三种轨道状态的界面文案。`unavailable` 必须带原因，`no_data` 单独成句，都不写成 0。 */
export function trackStatusView(track: TrackResponse): TrackStatusView {
  if (track.status === "unavailable") {
    const reason = track.unavailable_reason ?? "后端没有给出原因";
    return { key: "unavailable", badge: "不可用", text: `不可用：${reason}`, tone: "warn" };
  }
  if (track.status === "no_data") {
    return {
      key: "no_data",
      badge: "无数据",
      text: "无数据：这次启动里没有这条流的任何记录",
      tone: "muted",
    };
  }
  return { key: "available", badge: "有数据", text: "有数据", tone: "ok" };
}

export type ResourcesNotice = {
  title: string;
  reasons: string[];
  tone: "warn" | "info";
};

/** 整次启动的资源声明（`resources`）。它描述「有没有资源数据」，与单条轨道状态是两件事。 */
export function resourcesNotice(resources: TracksResponse["resources"]): ResourcesNotice | null {
  const reasons = resources.reasons ?? [];
  if (resources.available === false) {
    return { title: "资源数据不可用", reasons, tone: "warn" };
  }
  if (reasons.length > 0) {
    return { title: "资源数据说明", reasons, tone: "info" };
  }
  return null;
}

/** 区间是否没有确定的结束时刻（没声明结束，或只有墙钟声明了结束）。 */
export function isOpenSpan(span: SpanItem): boolean {
  return span.end_monotonic_ns === null;
}

/** 区间的结束说明文字，供轨道标签与悬浮提示使用。 */
export function spanEndLabel(span: SpanItem): string {
  if (!span.end_is_declared) return "未闭合：采集结束时仍在进行";
  if (span.end_monotonic_ns === null) return "结束时刻未知：只有墙钟收尾";
  return "";
}

export type SpanClip = {
  startFraction: number;
  endFraction: number;
  /** 结束时刻是由后端收尾/缺失推出来的，不是记录自己声明的。 */
  openEnd: boolean;
  /** 区间的起点在窗口之前或结束在窗口之后，被窗口裁掉了。 */
  clippedStart: boolean;
  clippedEnd: boolean;
};

/** 把区间裁剪到当前窗口，得到画布上的起止比例；与窗口无交集时返回 null。 */
export function clipSpan(span: SpanItem, range: WindowRange): SpanClip | null {
  if (span.monotonic_ns === null) return null;
  const startNs = span.monotonic_ns;
  const endNs = span.end_monotonic_ns ?? range.endNs;
  if (endNs < range.startNs || startNs > range.endNs) return null;
  const startFraction = Math.min(Math.max(nsToFraction(startNs, range), 0), 1);
  const endFraction = Math.min(Math.max(nsToFraction(endNs, range), 0), 1);
  return {
    startFraction,
    endFraction,
    openEnd: span.end_monotonic_ns === null,
    clippedStart: startNs < range.startNs,
    clippedEnd: endNs > range.endNs,
  };
}

/** 游标停在某一时刻时，这条轨道在该时刻的读数。
 *
 * 数值全部取自 `state_at` 的返回：指标是「该时刻或之前最近的采样点」，区间是「该时刻正在
 * 进行的区间」，事件是「该时刻或之前最近的一个事件」。前端不自己找相邻记录。
 */
export function trackCursorReadout(
  track: TrackResponse,
  stateTrack: TrackStateResponse | undefined,
): string {
  if (track.status === "unavailable") return "不可用：没有采样点";
  if (track.status === "no_data") return "无数据：这次启动里没有这条流的记录";
  if (!stateTrack) return "读取中…";
  if (track.kind === "metric") {
    return stateTrack.metric
      ? formatMetricValue(stateTrack.metric.value, stateTrack.metric.unit)
      : "此时刻之前没有采样点";
  }
  if (track.kind === "span") {
    if (stateTrack.spans.length === 0) return "此时刻没有进行中的区间";
    return `进行中：${stateTrack.spans.map((span) => span.state ?? span.span_type).join("、")}`;
  }
  return stateTrack.last_event ? stateTrack.last_event.event_type : "此时刻之前没有事件";
}
