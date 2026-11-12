"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { EventTrack } from "@/components/timeline/event-track";
import { MetricTrackChart } from "@/components/timeline/metric-track-chart";
import { SpanTrack } from "@/components/timeline/span-track";
import { Button } from "@/components/ui/button";
import {
  describeError,
  fetchCursor,
  fetchState,
  fetchTracks,
  type CursorResponse,
  type EventItem,
  type MachineResponse,
  type MetricItem,
  type SpanItem,
  type TimelineStateResponse,
  type TrackResponse,
  type TracksResponse,
} from "@/lib/api/client";
import {
  formatBootOffset,
  formatDuration,
  formatNsExact,
  formatWallClockUtc,
} from "@/lib/timeline/format";
import {
  fractionToNs,
  isInsideWindow,
  nsToFraction,
  panWindow,
  windowAround,
  windowSpan,
  zoomWindow,
  type WindowRange,
} from "@/lib/timeline/scale";
import {
  groupTracks,
  isOpenSpan,
  resourcesNotice,
  spanEndLabel,
  trackCursorReadout,
  trackLabel,
  trackSourceLabel,
  trackStatusView,
} from "@/lib/timeline/tracks";

/** 轨道标签列的固定宽度：所有轨道的绘图区因此左边界一致，游标才能一条直线画到底。 */
export const TRACK_LABEL_WIDTH = 240;

const ZOOM_IN_FACTOR = 0.5;
const ZOOM_OUT_FACTOR = 2;
const DRAG_THRESHOLD_PX = 3;
const WINDOW_FETCH_DELAY_MS = 80;

const TONE_CLASS: Record<"ok" | "muted" | "warn", string> = {
  ok: "bg-slate-100 text-slate-600",
  muted: "bg-slate-100 text-slate-500",
  warn: "bg-amber-100 text-amber-800",
};

function axisOf(tracks: TracksResponse): WindowRange | null {
  if (tracks.axis_start_ns === null || tracks.axis_end_ns === null) return null;
  return { startNs: tracks.axis_start_ns, endNs: tracks.axis_end_ns };
}

type TrackSlices = { spans: SpanItem[]; events: EventItem[]; metrics: MetricItem[] };

/** 按轨道把条目切成三类。轨道数据不变时只算一次，避免每次渲染都对每条轨道重跑三遍过滤。 */
function sliceTrackItems(tracks: readonly TrackResponse[]): Map<string, TrackSlices> {
  const slices = new Map<string, TrackSlices>();
  for (const track of tracks) {
    slices.set(track.track_id, {
      spans: track.items.filter((item): item is SpanItem => item.type === "span"),
      events: track.items.filter((item): item is EventItem => item.type === "event"),
      metrics: track.items.filter((item): item is MetricItem => item.type === "metric"),
    });
  }
  return slices;
}

export type TimelineReplayProps = {
  machine: MachineResponse;
  bootId: string;
  initialTracks: TracksResponse;
  initialCursor: CursorResponse;
};

/** 统一时间线回放：多轨道共用一个游标，缩放、拖动、定位关键事件都在这里。
 *
 * 数据全部来自 FastAPI 的时间线接口：窗口值与游标值原样显示后端返回的字段，组件只负责
 * 像素映射与文字格式。
 */
export function TimelineReplay({
  machine,
  bootId,
  initialTracks,
  initialCursor,
}: TimelineReplayProps) {
  const axis = useMemo(() => axisOf(initialTracks), [initialTracks]);
  const [tracksData, setTracksData] = useState<TracksResponse>(initialTracks);
  const [cursor, setCursor] = useState<CursorResponse>(initialCursor);
  const [state, setState] = useState<TimelineStateResponse | null>(null);
  const [range, setRange] = useState<WindowRange>(() =>
    axis ? { startNs: axis.startNs, endNs: axis.endNs } : { startNs: 0, endNs: 0 },
  );
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const tracksRequestRef = useRef(0);
  const draggingRef = useRef<{
    pointerId: number;
    startX: number;
    width: number;
    originRange: WindowRange;
    moved: boolean;
  } | null>(null);

  const displayRange = useMemo<WindowRange>(
    () => ({
      startNs: tracksData.window_start_ns ?? axis?.startNs ?? 0,
      endNs: tracksData.window_end_ns ?? axis?.endNs ?? 0,
    }),
    [tracksData, axis],
  );

  const cursorPosition = cursor.position_ns;
  const cursorInside = cursorPosition !== null && isInsideWindow(cursorPosition, displayRange);

  const loadTracks = useCallback(
    async (target: WindowRange) => {
      const requestId = tracksRequestRef.current + 1;
      tracksRequestRef.current = requestId;
      setPending(true);
      try {
        const next = await fetchTracks(machine.machine_id, bootId, {
          startNs: target.startNs,
          endNs: target.endNs,
        });
        if (requestId !== tracksRequestRef.current) return;
        setTracksData(next);
        setError(null);
      } catch (caught) {
        if (requestId !== tracksRequestRef.current) return;
        setError(describeError(caught));
      } finally {
        if (requestId === tracksRequestRef.current) setPending(false);
      }
    },
    [bootId, machine.machine_id],
  );

  // 窗口变化才重新取可见范围；后端回显的窗口与请求一致，因此数据回来后这个判断就成立，
  // 不会来回触发。拖动过程中连续变化，用一点延迟合并成一次请求。
  useEffect(() => {
    if (!axis) return;
    if (range.startNs === displayRange.startNs && range.endNs === displayRange.endNs) return;
    const timer = setTimeout(() => {
      void loadTracks(range);
    }, WINDOW_FETCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [axis, displayRange.endNs, displayRange.startNs, loadTracks, range]);

  // 游标位置变化就取该时刻每条轨道的状态；同一时刻的重复请求不影响显示。
  useEffect(() => {
    if (cursorPosition === null) return;
    let alive = true;
    fetchState(machine.machine_id, bootId, cursorPosition)
      .then((next) => {
        if (alive) setState(next);
      })
      .catch((caught) => {
        if (alive) setError(describeError(caught));
      });
    return () => {
      alive = false;
    };
  }, [bootId, cursorPosition, machine.machine_id]);

  const locate = useCallback(
    async (atNs: number | null, forward?: boolean) => {
      try {
        const next = await fetchCursor(machine.machine_id, bootId, { atNs, forward });
        setCursor(next);
        setError(null);
      } catch (caught) {
        setError(describeError(caught));
      }
    },
    [bootId, machine.machine_id],
  );

  const stepCursor = (forward: boolean) => {
    void locate(cursorPosition, forward);
  };

  const zoomBy = (factor: number) => {
    if (!axis) return;
    const anchor =
      cursorPosition !== null && isInsideWindow(cursorPosition, displayRange)
        ? cursorPosition
        : Math.round((displayRange.startNs + displayRange.endNs) / 2);
    setRange(zoomWindow(displayRange, factor, anchor, axis));
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!axis) return;
    const rect = event.currentTarget.getBoundingClientRect();
    draggingRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      width: rect.width,
      originRange: displayRange,
      moved: false,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = draggingRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !axis || drag.width <= 0) return;
    const dx = event.clientX - drag.startX;
    if (Math.abs(dx) > DRAG_THRESHOLD_PX) drag.moved = true;
    if (!drag.moved) return;
    const deltaNs = (-dx / drag.width) * windowSpan(drag.originRange);
    setRange(panWindow(drag.originRange, deltaNs, axis));
  };

  const handlePointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = draggingRef.current;
    draggingRef.current = null;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.moved) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const fraction = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1);
    void locate(fractionToNs(fraction, displayRange));
  };

  const groups = useMemo(() => groupTracks(tracksData.tracks), [tracksData.tracks]);
  const notice = resourcesNotice(tracksData.resources);
  // 游标没有位置时（这次启动没有任何可停靠的时刻）不显示上一轮的时刻状态，避免把旧读数
  // 当成当前读数。
  const activeState = cursorPosition === null ? null : state;
  const stateByTrack = useMemo(
    () => new Map((activeState?.tracks ?? []).map((track) => [track.track_id, track])),
    [activeState],
  );
  const events = useMemo(
    () => tracksData.items.filter((item): item is EventItem => item.type === "event"),
    [tracksData.items],
  );
  const trackItems = useMemo(() => sliceTrackItems(tracksData.tracks), [tracksData.tracks]);
  const cursorFraction =
    cursorInside && cursorPosition !== null ? nsToFraction(cursorPosition, displayRange) : null;
  const hasData = axis !== null && tracksData.tracks.length > 0;

  return (
    <section className="flex flex-col gap-4" data-testid="timeline-replay">
      <header className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-xl font-semibold text-slate-900">{machine.hostname}</h1>
          <span className="font-mono text-xs text-slate-500">{machine.machine_id}</span>
          <span className="font-mono text-xs text-slate-500">启动 {bootId}</span>
        </div>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-lg border border-slate-200 bg-white px-4 py-3 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-slate-500">轴范围（后端 axis_*）</dt>
            <dd className="mt-0.5 font-mono text-slate-800" data-testid="axis-range-exact">
              {axis ? `${formatNsExact(axis.startNs)} → ${formatNsExact(axis.endNs)}` : "没有数据"}
            </dd>
            <dd className="text-slate-500" data-testid="axis-range-duration">
              {axis ? formatDuration(axis.endNs - axis.startNs) : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">当前窗口（后端 window_*）</dt>
            <dd className="mt-0.5 font-mono text-slate-800" data-testid="window-range-exact">
              {`${formatNsExact(displayRange.startNs)} → ${formatNsExact(displayRange.endNs)}`}
            </dd>
            <dd className="text-slate-500">
              {formatDuration(displayRange.endNs - displayRange.startNs)}
              {pending ? " · 读取中…" : ""}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">游标（后端 position_ns）</dt>
            <dd className="mt-0.5 font-mono text-slate-800" data-testid="cursor-position-exact">
              {cursorPosition === null ? "无" : formatNsExact(cursorPosition)}
            </dd>
            <dd className="text-slate-500">
              {cursorPosition === null
                ? "这次启动没有可停靠的时刻"
                : `${formatBootOffset(cursorPosition)}${
                    tracksData.clock.anchor_ns !== null
                      ? ` · ${formatWallClockUtc(tracksData.clock.anchor_ns + cursorPosition)}`
                      : ""
                  }`}
              {cursorInside ? "" : " · 游标在窗口之外"}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">上一帧（previous_ns）</dt>
            <dd className="mt-0.5 font-mono text-slate-800" data-testid="cursor-previous-exact">
              {cursor.previous_ns === null ? "无" : formatNsExact(cursor.previous_ns)}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">下一帧（next_ns）</dt>
            <dd className="mt-0.5 font-mono text-slate-800" data-testid="cursor-next-exact">
              {cursor.next_ns === null ? "无" : formatNsExact(cursor.next_ns)}
            </dd>
          </div>
          <div>
            <dt className="text-slate-500">启动锚点与漂移（clock）</dt>
            <dd className="mt-0.5 text-slate-800" data-testid="clock-drift">
              {tracksData.clock.anchor_ns === null
                ? "没有锚点"
                : `${formatNsExact(tracksData.clock.anchor_ns)}（候选 ${tracksData.clock.anchor_count} 个）`}
            </dd>
            <dd className="text-slate-500">
              漂移{" "}
              {tracksData.clock.drift_ns === null
                ? "未知"
                : formatDuration(tracksData.clock.drift_ns)}
              {tracksData.clock.unreliable ? " · 大于 1 秒，墙钟只用于显示时间文字" : ""}
            </dd>
          </div>
        </dl>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => stepCursor(false)}
            data-testid="cursor-prev"
          >
            上一帧
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => stepCursor(true)}
            data-testid="cursor-next"
          >
            下一帧
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => zoomBy(ZOOM_IN_FACTOR)}
            data-testid="zoom-in"
          >
            放大
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => zoomBy(ZOOM_OUT_FACTOR)}
            data-testid="zoom-out"
          >
            缩小
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => axis && setRange(axis)}
            data-testid="zoom-reset"
          >
            全览
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={cursorPosition === null}
            onClick={() =>
              axis &&
              cursorPosition !== null &&
              setRange(windowAround(cursorPosition, windowSpan(displayRange), axis))
            }
            data-testid="follow-cursor"
          >
            把窗口移到游标
          </Button>
          <span className="text-xs text-slate-500">在轨道上单击定位游标，按住左右拖动平移窗口</span>
        </div>
      </header>

      {error ? (
        <p
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          data-testid="timeline-error"
        >
          {error}
        </p>
      ) : null}

      {notice ? (
        <div
          className={
            notice.tone === "warn"
              ? "rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
              : "rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700"
          }
          data-testid={notice.tone === "warn" ? "resources-unavailable" : "resources-note"}
        >
          <p className="font-medium">{notice.title}</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {notice.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {!hasData ? (
        <p
          className="rounded-md border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600"
          data-testid="timeline-empty"
        >
          这次启动没有可回放的轨道：后端返回的轨道列表为空。
        </p>
      ) : (
        <div
          className="relative overflow-hidden rounded-xl border border-slate-200 bg-white"
          data-testid="timeline-stack"
        >
          <div
            className="pointer-events-none absolute inset-y-0 right-0"
            style={{ left: TRACK_LABEL_WIDTH }}
            aria-hidden
          >
            {cursorFraction !== null ? (
              <div
                className="absolute inset-y-0 w-px bg-slate-900"
                style={{ left: `${cursorFraction * 100}%` }}
                data-testid="shared-cursor"
              />
            ) : null}
          </div>
          {groups.map((group) => (
            <div key={group.key}>
              <div className="flex items-baseline gap-2 border-y border-slate-100 bg-slate-50 px-3 py-1 text-xs">
                <span className="font-medium text-slate-700">{group.title}</span>
                <span className="text-slate-500">{group.description}</span>
              </div>
              {group.tracks.map((track) => {
                const status = trackStatusView(track);
                const stateTrack = stateByTrack.get(track.track_id);
                const {
                  spans,
                  events: trackEvents,
                  metrics: points,
                } = trackItems.get(track.track_id) ?? { spans: [], events: [], metrics: [] };
                const openSpans = spans.filter(isOpenSpan);
                return (
                  <div
                    key={track.track_id}
                    className="flex border-b border-slate-100 last:border-b-0"
                    data-testid="track-row"
                    data-track-id={track.track_id}
                  >
                    <div
                      className="shrink-0 border-r border-slate-100 px-3 py-2"
                      style={{ width: TRACK_LABEL_WIDTH }}
                    >
                      <div
                        className="truncate text-xs font-medium text-slate-800"
                        title={track.track_id}
                      >
                        {trackLabel(track)}
                      </div>
                      <div className="truncate text-[11px] text-slate-500">
                        {trackSourceLabel(track)}
                      </div>
                      <div
                        className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[11px] ${TONE_CLASS[status.tone]}`}
                        data-testid="track-status"
                        data-track-id={track.track_id}
                        data-status={status.key}
                        title={status.key === "unavailable" ? status.text : undefined}
                      >
                        {status.badge}
                      </div>
                      <div
                        className="mt-1 truncate text-[11px] text-slate-600"
                        data-testid="track-cursor-readout"
                        data-track-id={track.track_id}
                      >
                        {trackCursorReadout(track, stateTrack)}
                      </div>
                      {openSpans.length > 0 ? (
                        <div
                          className="mt-1 text-[11px] text-red-700"
                          data-testid="track-open-span"
                          data-track-id={track.track_id}
                          title={openSpans.map(spanEndLabel).join("；")}
                        >
                          未闭合 {openSpans.length} 段
                        </div>
                      ) : null}
                    </div>
                    <div
                      className="relative min-w-0 flex-1 touch-none select-none"
                      onPointerDown={handlePointerDown}
                      onPointerMove={handlePointerMove}
                      onPointerUp={handlePointerUp}
                      onPointerCancel={() => {
                        draggingRef.current = null;
                      }}
                      data-testid="track-plot"
                      data-track-id={track.track_id}
                    >
                      {status.key !== "available" ? (
                        <div
                          className={
                            status.key === "unavailable"
                              ? "m-2 rounded border border-dashed border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900"
                              : "m-2 rounded border border-dashed border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-600"
                          }
                          data-testid="track-placeholder"
                          data-track-id={track.track_id}
                          data-status={status.key}
                        >
                          {status.text}
                        </div>
                      ) : track.kind === "metric" ? (
                        <MetricTrackChart
                          points={points}
                          range={displayRange}
                          cursorNs={cursorInside ? cursorPosition : null}
                          unit={track.unit}
                          label={trackLabel(track)}
                        />
                      ) : track.kind === "span" ? (
                        spans.length > 0 ? (
                          <SpanTrack spans={spans} range={displayRange} />
                        ) : (
                          <p className="px-3 py-2 text-xs text-slate-500">本窗口没有区间</p>
                        )
                      ) : trackEvents.length > 0 ? (
                        <EventTrack events={trackEvents} range={displayRange} />
                      ) : (
                        <p className="px-3 py-2 text-xs text-slate-500">本窗口没有事件</p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
          {tracksData.wall_clock_items.length > 0 ? (
            <div className="flex border-t border-slate-100">
              <div
                className="shrink-0 border-r border-slate-100 px-3 py-2"
                style={{ width: TRACK_LABEL_WIDTH }}
              >
                <div className="text-xs font-medium text-slate-800">位置未知（墙钟）</div>
                <div className="text-[11px] text-slate-500">只有 realtime_ns，没有 boot 内位置</div>
              </div>
              <div className="min-w-0 flex-1 px-3 py-2" data-testid="wall-clock-track">
                <ul className="flex flex-wrap gap-2 text-[11px] text-slate-600">
                  {tracksData.wall_clock_items.map((item) => (
                    <li
                      key={`${item.collection_id}-${item.record_id}`}
                      className="rounded bg-slate-100 px-2 py-0.5"
                    >
                      {item.realtime_ns === null
                        ? "无墙钟读数"
                        : formatWallClockUtc(item.realtime_ns)}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          ) : null}
        </div>
      )}

      {events.length > 0 ? (
        <section className="rounded-lg border border-slate-200 bg-white" data-testid="event-list">
          <h2 className="border-b border-slate-100 px-3 py-2 text-xs font-medium text-slate-700">
            关键事件（点击定位游标）
          </h2>
          <ul className="divide-y divide-slate-100">
            {events.map((event) => (
              <li key={`${event.collection_id}-${event.record_id}`}>
                <button
                  type="button"
                  className="flex w-full items-baseline gap-3 px-3 py-1.5 text-left text-xs hover:bg-slate-50"
                  onClick={() => void locate(event.monotonic_ns)}
                  data-testid="event-jump"
                  data-monotonic-ns={event.monotonic_ns ?? ""}
                >
                  <span className="w-24 shrink-0 font-mono text-slate-500">
                    {event.monotonic_ns === null
                      ? "位置未知"
                      : formatBootOffset(event.monotonic_ns)}
                  </span>
                  <span className="w-44 shrink-0 truncate text-slate-700">{event.event_type}</span>
                  <span className="min-w-0 flex-1 truncate text-slate-600">
                    {event.message ?? ""}
                  </span>
                  <span className="shrink-0 text-slate-400">{event.severity}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}
