import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TimelineReplay } from "@/components/timeline/timeline-replay";
import type { CursorResponse, TimelineStateResponse, TracksResponse } from "@/lib/api/client";
import { panWindow, zoomWindow } from "@/lib/timeline/scale";
import {
  AXIS_END,
  AXIS_START,
  makeCursor,
  makeEvent,
  makeEventTrack,
  makeMetricTrack,
  makeSpan,
  makeSpanTrack,
  makeState,
  makeTracksResponse,
  machine,
  resourcesUnavailable,
} from "@/test-fixtures/timeline";

vi.mock("@/components/timeline/metric-track-chart", () => ({
  MetricTrackChart: (props: {
    range: { startNs: number; endNs: number };
    cursorNs: number | null;
    points: unknown[];
  }) => (
    <div
      data-testid="metric-chart"
      data-window-start={props.range.startNs}
      data-window-end={props.range.endNs}
      data-cursor={props.cursorNs === null ? "" : String(props.cursorNs)}
      data-points={props.points.length}
    />
  ),
}));

type Handlers = {
  tracks?: (url: URL) => TracksResponse;
  cursor?: (url: URL) => CursorResponse;
  state?: (url: URL) => TimelineStateResponse;
};

function stubApi(handlers: Handlers) {
  const calls: { path: string; url: URL }[] = [];
  const defaultTracks = makeTracksResponse();
  const defaultCursor = makeCursor({ position_ns: 2_000_000_000 });
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, "http://frontend.test");
    calls.push({ path: url.pathname, url });
    let payload: unknown;
    if (url.pathname.endsWith("/timeline/tracks")) {
      payload = handlers.tracks?.(url) ?? defaultTracks;
    } else if (url.pathname.endsWith("/timeline/cursor")) {
      payload = handlers.cursor?.(url) ?? defaultCursor;
    } else if (url.pathname.endsWith("/timeline/state")) {
      payload = handlers.state?.(url) ?? makeState();
    } else {
      throw new Error(`测试没有为这个请求准备响应：${url.pathname}`);
    }
    return { ok: true, status: 200, json: async () => payload } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock, defaultCursor, defaultTracks };
}

function renderTimeline(
  tracks: TracksResponse = makeTracksResponse(),
  cursor: CursorResponse = makeCursor({ position_ns: 2_000_000_000 }),
) {
  return render(
    <TimelineReplay
      machine={machine}
      bootId={tracks.boot_id}
      initialTracks={tracks}
      initialCursor={cursor}
    />,
  );
}

/** 模拟一次完整的按下与抬起：轨道上画出的条目靠这对事件区分「点选」与「拖动」。 */
function pressItem(element: HTMLElement) {
  fireEvent.pointerDown(element, { pointerId: 9, clientX: 120 });
  fireEvent.pointerUp(element, { pointerId: 9, clientX: 120 });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TimelineReplay", () => {
  it("渲染生命周期、事件与资源三类轨道", async () => {
    stubApi({});
    renderTimeline();

    expect(screen.getAllByTestId("track-row")).toHaveLength(3);
    expect(screen.getByTestId("metric-chart")).toBeInTheDocument();
    expect(screen.getAllByTestId("span-track")).toHaveLength(1);
    expect(screen.getAllByTestId("event-track")).toHaveLength(1);
    expect(screen.getByText("Systemd Unit 生命周期")).toBeInTheDocument();
    expect(screen.getByText("资源采样")).toBeInTheDocument();
  });

  it("所有轨道共用同一条游标，位置来自后端 position_ns", async () => {
    stubApi({});
    renderTimeline();

    const cursors = screen.getAllByTestId("shared-cursor");
    expect(cursors).toHaveLength(1);
    // 轴 1e9..9e9，游标 2e9 → 12.5%
    expect(cursors[0].style.left).toBe("12.5%");
    expect(screen.getByTestId("cursor-position-exact")).toHaveTextContent("2000000000");
  });

  it("点「下一帧」用 forward 请求后端，并用返回的位置更新读数与该时刻状态", async () => {
    const { calls } = stubApi({
      cursor: (url) =>
        url.searchParams.get("forward") === "true"
          ? makeCursor({
              position_ns: 3_000_000_000,
              previous_ns: 2_000_000_000,
              next_ns: 4_000_000_000,
            })
          : makeCursor({ position_ns: 2_000_000_000 }),
    });
    renderTimeline();
    await waitFor(() =>
      expect(calls.some((call) => call.path.endsWith("/timeline/state"))).toBe(true),
    );

    await userEvent.click(screen.getByTestId("cursor-next"));

    await waitFor(() =>
      expect(screen.getByTestId("cursor-position-exact")).toHaveTextContent("3000000000"),
    );
    const cursorCalls = calls.filter((call) => call.path.endsWith("/timeline/cursor"));
    expect(cursorCalls).toHaveLength(1);
    expect(cursorCalls[0].url.searchParams.get("forward")).toBe("true");
    expect(cursorCalls[0].url.searchParams.get("at_ns")).toBe("2000000000");

    await waitFor(() => {
      const stateCalls = calls.filter((call) => call.path.endsWith("/timeline/state"));
      expect(stateCalls.at(-1)?.url.searchParams.get("monotonic_ns")).toBe("3000000000");
    });
    expect(screen.getByTestId("shared-cursor").style.left).toBe("25%");
  });

  it("放大后用后端回显的窗口值显示，请求参数是缩放后的窗口", async () => {
    const { calls } = stubApi({
      tracks: (url) => {
        const startNs = Number(url.searchParams.get("start_ns"));
        const endNs = Number(url.searchParams.get("end_ns"));
        return makeTracksResponse({ window_start_ns: startNs, window_end_ns: endNs });
      },
    });
    renderTimeline();
    const expected = zoomWindow({ startNs: AXIS_START, endNs: AXIS_END }, 0.5, 2_000_000_000, {
      startNs: AXIS_START,
      endNs: AXIS_END,
    });

    await userEvent.click(screen.getByTestId("zoom-in"));

    await waitFor(() =>
      expect(screen.getByTestId("window-range-exact")).toHaveTextContent(
        `${expected.startNs} → ${expected.endNs}`,
      ),
    );
    const tracksCall = calls.filter((call) => call.path.endsWith("/timeline/tracks")).at(-1);
    expect(tracksCall?.url.searchParams.get("start_ns")).toBe(String(expected.startNs));
    expect(tracksCall?.url.searchParams.get("end_ns")).toBe(String(expected.endNs));
    expect(screen.getByTestId("metric-chart")).toHaveAttribute(
      "data-window-start",
      String(expected.startNs),
    );
  });

  it("拖动轨道平移窗口；游标被拖到窗口外后不再画游标线", async () => {
    const { calls } = stubApi({
      tracks: (url) => {
        const startNs = Number(url.searchParams.get("start_ns"));
        const endNs = Number(url.searchParams.get("end_ns"));
        return makeTracksResponse({ window_start_ns: startNs, window_end_ns: endNs });
      },
    });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      width: 800,
      height: 40,
      top: 0,
      left: 0,
      right: 800,
      bottom: 40,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    renderTimeline();

    // 先缩小到一半，才有多余空间向右平移
    await userEvent.click(screen.getByTestId("zoom-in"));
    const zoomed = zoomWindow({ startNs: AXIS_START, endNs: AXIS_END }, 0.5, 2_000_000_000, {
      startNs: AXIS_START,
      endNs: AXIS_END,
    });
    await waitFor(() =>
      expect(screen.getByTestId("window-range-exact")).toHaveTextContent(
        `${zoomed.startNs} → ${zoomed.endNs}`,
      ),
    );

    const plot = screen.getAllByTestId("track-plot")[0];
    fireEvent.pointerDown(plot, { pointerId: 1, clientX: 400 });
    fireEvent.pointerMove(plot, { pointerId: 1, clientX: 200 });
    fireEvent.pointerUp(plot, { pointerId: 1, clientX: 200 });

    const expected = panWindow(zoomed, (200 / 800) * (zoomed.endNs - zoomed.startNs), {
      startNs: AXIS_START,
      endNs: AXIS_END,
    });
    await waitFor(() =>
      expect(screen.getByTestId("window-range-exact")).toHaveTextContent(
        `${expected.startNs} → ${expected.endNs}`,
      ),
    );
    expect(calls.filter((call) => call.path.endsWith("/timeline/cursor"))).toHaveLength(0);
    expect(screen.queryByTestId("shared-cursor")).not.toBeInTheDocument();
    expect(screen.getByText(/游标在窗口之外/)).toBeInTheDocument();
  });

  it("第二次缩放的窗口还没落到画面上时拖动，按当前画出来的窗口换算平移量", async () => {
    stubApi({
      tracks: (url) => {
        const startNs = Number(url.searchParams.get("start_ns"));
        const endNs = Number(url.searchParams.get("end_ns"));
        return makeTracksResponse({ window_start_ns: startNs, window_end_ns: endNs });
      },
    });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      width: 800,
      height: 40,
      top: 0,
      left: 0,
      right: 800,
      bottom: 40,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    renderTimeline();
    const axis = { startNs: AXIS_START, endNs: AXIS_END };

    await userEvent.click(screen.getByTestId("zoom-in"));
    const firstZoom = zoomWindow(axis, 0.5, 2_000_000_000, axis);
    await waitFor(() =>
      expect(screen.getByTestId("window-range-exact")).toHaveTextContent(
        `${firstZoom.startNs} → ${firstZoom.endNs}`,
      ),
    );

    // 第二次放大：目标窗口变了，但请求还在 80 毫秒防抖里，画面上仍是 firstZoom
    await userEvent.click(screen.getByTestId("zoom-in"));
    expect(screen.getByTestId("window-range-exact")).toHaveTextContent(
      `${firstZoom.startNs} → ${firstZoom.endNs}`,
    );

    const plot = screen.getAllByTestId("track-plot")[0];
    fireEvent.pointerDown(plot, { pointerId: 4, clientX: 600 });
    fireEvent.pointerMove(plot, { pointerId: 4, clientX: 400 });
    fireEvent.pointerUp(plot, { pointerId: 4, clientX: 400 });

    // 平移量必须按画面上的 firstZoom 换算；按目标窗口换算会得到另一个结果
    const expected = panWindow(
      firstZoom,
      (200 / 800) * (firstZoom.endNs - firstZoom.startNs),
      axis,
    );
    const fromTarget = panWindow(
      zoomWindow(firstZoom, 0.5, 2_000_000_000, axis),
      (200 / 800) * (firstZoom.endNs - firstZoom.startNs),
      axis,
    );
    expect(expected.startNs).not.toBe(fromTarget.startNs);
    await waitFor(() =>
      expect(screen.getByTestId("window-range-exact")).toHaveTextContent(
        `${expected.startNs} → ${expected.endNs}`,
      ),
    );
  });

  it("在轨道上单击把游标定位到该时刻", async () => {
    const { calls } = stubApi({
      cursor: (url) => makeCursor({ position_ns: Number(url.searchParams.get("at_ns")) }),
    });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      width: 800,
      height: 40,
      top: 0,
      left: 0,
      right: 800,
      bottom: 40,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    renderTimeline();

    const plot = screen.getAllByTestId("track-plot")[0];
    fireEvent.pointerDown(plot, { pointerId: 2, clientX: 400 });
    fireEvent.pointerUp(plot, { pointerId: 2, clientX: 400 });

    await waitFor(() => {
      const cursorCall = calls.find((call) => call.path.endsWith("/timeline/cursor"));
      expect(cursorCall?.url.searchParams.get("at_ns")).toBe("5000000000");
    });
    await waitFor(() =>
      expect(screen.getByTestId("cursor-position-exact")).toHaveTextContent("5000000000"),
    );
  });

  it("点关键事件把游标定位到事件时刻", async () => {
    const { calls } = stubApi({
      cursor: (url) => makeCursor({ position_ns: Number(url.searchParams.get("at_ns")) }),
    });
    renderTimeline();

    await userEvent.click(screen.getAllByTestId("event-jump")[0]);

    await waitFor(() => {
      const cursorCall = calls.find((call) => call.path.endsWith("/timeline/cursor"));
      expect(cursorCall?.url.searchParams.get("at_ns")).toBe("2000000000");
    });
  });

  it("点事件刻度：选中该事件、显示详情，游标用事件自己的 monotonic_ns", async () => {
    const event = makeEvent({
      record_id: "event-picked",
      monotonic_ns: 2_500_000_000,
    });
    const tracks = makeTracksResponse({
      tracks: [makeEventTrack({ items: [event] })],
      items: [event],
    });
    const { calls } = stubApi({
      tracks: () => tracks,
      cursor: (url) => makeCursor({ position_ns: Number(url.searchParams.get("at_ns")) }),
    });
    renderTimeline(tracks);

    const mark = screen.getByTestId("event-mark");
    expect(mark).toHaveAttribute("data-record-id", "event-picked");
    pressItem(mark);

    const panel = screen.getByTestId("selection-detail");
    expect(panel).toHaveTextContent("选中：事件");
    expect(screen.getByTestId("detail-event-type")).toHaveTextContent("systemd.unit.started");
    expect(screen.getByTestId("detail-severity")).toHaveTextContent("info");
    expect(screen.getByTestId("detail-message")).toHaveTextContent("Started nginx.service");
    expect(screen.getByTestId("detail-entity")).toHaveTextContent("service:nginx.service");
    expect(screen.getByTestId("detail-record-id")).toHaveTextContent("event-picked");
    expect(screen.getByTestId("detail-collection-id")).toHaveTextContent("collection:one");
    expect(screen.getByTestId("detail-monotonic-ns")).toHaveTextContent("2500000000");
    expect(screen.getByTestId("detail-realtime-ns")).toHaveTextContent("UTC");
    expect(mark).toHaveAttribute("data-selected", "true");

    await waitFor(() => {
      const cursorCall = calls.find((call) => call.path.endsWith("/timeline/cursor"));
      expect(cursorCall?.url.searchParams.get("at_ns")).toBe("2500000000");
    });
  });

  it("点区间条：详情含类型、状态与起止时刻，游标定位到区间起点", async () => {
    const span = makeSpan({
      record_id: "span-picked",
      monotonic_ns: 3_000_000_000,
      end_monotonic_ns: 5_000_000_000,
      span_type: "service.active",
      state: "active",
    });
    const tracks = makeTracksResponse({
      tracks: [makeSpanTrack({ items: [span] })],
      items: [span],
    });
    const { calls } = stubApi({
      tracks: () => tracks,
      cursor: (url) => makeCursor({ position_ns: Number(url.searchParams.get("at_ns")) }),
    });
    renderTimeline(tracks);

    const bar = screen.getByTestId("span-bar");
    pressItem(bar);

    expect(screen.getByTestId("selection-detail")).toHaveTextContent("选中：生命周期区间");
    expect(screen.getByTestId("detail-span-type")).toHaveTextContent("service.active");
    expect(screen.getByTestId("detail-state")).toHaveTextContent("active");
    expect(screen.getByTestId("detail-start")).toHaveTextContent("T+3.000 s");
    expect(screen.getByTestId("detail-start")).toHaveTextContent("3000000000");
    expect(screen.getByTestId("detail-end")).toHaveTextContent("T+5.000 s");
    expect(screen.getByTestId("detail-end")).toHaveTextContent("5000000000");
    expect(screen.getByTestId("detail-duration")).toHaveTextContent("2.000 s");
    expect(screen.getByTestId("detail-record-id")).toHaveTextContent("span-picked");
    expect(bar).toHaveAttribute("data-selected", "true");

    await waitFor(() => {
      const cursorCall = calls.find((call) => call.path.endsWith("/timeline/cursor"));
      expect(cursorCall?.url.searchParams.get("at_ns")).toBe("3000000000");
    });
  });

  it("选中未闭合区间：详情写未闭合，不把后端补的窗口边缘当成结束时刻", async () => {
    const span = makeSpan({
      record_id: "span-open",
      monotonic_ns: 3_000_000_000,
      end_monotonic_ns: 9_000_000_000,
      end_is_declared: false,
      closed_by: "collection:one",
    });
    const tracks = makeTracksResponse({
      tracks: [makeSpanTrack({ items: [span] })],
      items: [span],
    });
    stubApi({ tracks: () => tracks });
    renderTimeline(tracks);

    pressItem(screen.getByTestId("span-bar"));

    expect(screen.getByTestId("detail-end")).toHaveTextContent("未闭合：采集结束时仍在进行");
    expect(screen.getByTestId("detail-end")).not.toHaveTextContent("9000000000");
    expect(screen.getByTestId("detail-duration")).toHaveTextContent("未知");
  });

  it("点没有 monotonic_ns 的事件：显示详情、游标不动、不请求游标", async () => {
    const event = makeEvent({
      record_id: "event-no-clock",
      monotonic_ns: null,
      realtime_ns: null,
    });
    const tracks = makeTracksResponse({
      tracks: [makeEventTrack({ items: [event] })],
      items: [event],
    });
    const { calls } = stubApi({ tracks: () => tracks });
    renderTimeline(tracks);

    expect(screen.queryByTestId("event-mark")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("event-jump"));

    expect(screen.getByTestId("selection-detail")).toHaveTextContent("选中：事件");
    expect(screen.getByTestId("detail-monotonic-ns")).toHaveTextContent("无（不在 boot 时间轴上）");
    expect(screen.getByTestId("detail-realtime-ns")).toHaveTextContent("无");
    expect(screen.getByTestId("cursor-position-exact")).toHaveTextContent("2000000000");
    expect(calls.filter((call) => call.path.endsWith("/timeline/cursor"))).toHaveLength(0);
  });

  it("选中第二条替换第一条；点轨道空白处清除选中", async () => {
    stubApi({});
    renderTimeline();

    const eventMark = screen.getByTestId("event-mark");
    pressItem(eventMark);
    expect(screen.getByTestId("selection-detail")).toHaveTextContent("选中：事件");
    expect(eventMark).toHaveAttribute("data-selected", "true");

    const spanBar = screen.getByTestId("span-bar");
    pressItem(spanBar);
    expect(screen.getByTestId("selection-detail")).toHaveTextContent("选中：生命周期区间");
    expect(eventMark).toHaveAttribute("data-selected", "false");
    expect(spanBar).toHaveAttribute("data-selected", "true");

    const plot = screen.getAllByTestId("track-plot")[0];
    fireEvent.pointerDown(plot, { pointerId: 11, clientX: 40 });
    fireEvent.pointerUp(plot, { pointerId: 11, clientX: 40 });
    expect(screen.queryByTestId("selection-detail")).not.toBeInTheDocument();
    expect(spanBar).toHaveAttribute("data-selected", "false");
  });

  it("无数据的轨道显示「无数据」占位，不可用的轨道显示原因，都不画 0", async () => {
    stubApi({});
    renderTimeline(
      makeTracksResponse({
        tracks: [
          makeMetricTrack({ track_id: "metric:no-data", status: "no_data", items: [] }),
          makeMetricTrack({
            track_id: "metric:unavailable",
            status: "unavailable",
            items: [],
            unavailable_reason: "目标机器没有 iostat，磁盘 I/O 未采集",
          }),
        ],
        items: [],
      }),
    );

    expect(screen.queryByTestId("metric-chart")).not.toBeInTheDocument();
    const placeholders = screen.getAllByTestId("track-placeholder");
    expect(placeholders.map((node) => node.getAttribute("data-status"))).toEqual([
      "no_data",
      "unavailable",
    ]);
    expect(screen.getByText(/无数据：这次启动里没有这条流的任何记录/)).toBeInTheDocument();
    expect(screen.getByText("不可用：目标机器没有 iostat，磁盘 I/O 未采集")).toBeInTheDocument();
  });

  it("Journal-only 数据：资源不可用面板带原因，事件轨道照常回放", async () => {
    stubApi({});
    renderTimeline(
      makeTracksResponse({
        tracks: [makeEventTrack()],
        items: [makeEventTrack().items[0]],
        resources: resourcesUnavailable,
      }),
    );

    const panel = screen.getByTestId("resources-unavailable");
    expect(panel).toHaveTextContent("资源数据不可用");
    expect(panel).toHaveTextContent(resourcesUnavailable.reasons[0]);
    expect(screen.queryByTestId("metric-chart")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("event-track")).toHaveLength(1);
    expect(screen.getByTestId("track-status")).toHaveAttribute("data-status", "available");
  });

  it("没有可回放的数据时明确说明，不渲染空轨道", async () => {
    stubApi({});
    render(
      <TimelineReplay
        machine={machine}
        bootId="boot-empty"
        initialTracks={makeTracksResponse({
          tracks: [],
          items: [],
          axis_start_ns: null,
          axis_end_ns: null,
        })}
        initialCursor={makeCursor({
          start_ns: null,
          end_ns: null,
          position_ns: null,
          previous_ns: null,
          next_ns: null,
        })}
      />,
    );

    expect(screen.getByTestId("timeline-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("shared-cursor")).not.toBeInTheDocument();
    expect(screen.queryByTestId("track-row")).not.toBeInTheDocument();
  });

  it("后端报错时显示可读错误，不崩溃", async () => {
    const fetchMock = vi.fn(async () => {
      return {
        ok: false,
        status: 404,
        json: async () => ({
          error: { type: "not_found", message: "启动 machine:x/boot:y 不在库里", details: [] },
        }),
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    renderTimeline(makeTracksResponse(), makeCursor({ position_ns: 2_000_000_000 }));

    await waitFor(() =>
      expect(screen.getByTestId("timeline-error")).toHaveTextContent(
        "启动 machine:x/boot:y 不在库里",
      ),
    );
    expect(screen.getAllByTestId("track-row")).toHaveLength(3);
  });

  it("未闭合的区间在轨道上标注出来", async () => {
    stubApi({});
    const openSpan = makeSpan({
      end_monotonic_ns: null,
      end_realtime_ns: null,
      end_is_declared: false,
    });
    renderTimeline(
      makeTracksResponse({
        tracks: [makeSpanTrack({ items: [openSpan] })],
        items: [openSpan],
      }),
    );

    const badge = screen.getByTestId("track-open-span");
    expect(badge).toHaveTextContent("未闭合 1 段");
    expect(badge.getAttribute("title")).toContain("采集结束时仍在进行");
  });
});

describe("TimelineReplay 在真实规模下的渲染上限", () => {
  it("首屏直接使用后端回显的窗口，不会再把整条轴当成当前窗口", async () => {
    stubApi({});
    renderTimeline(
      makeTracksResponse({
        window_start_ns: 2_000_000_000,
        window_end_ns: 4_000_000_000,
      }),
    );

    expect(screen.getByTestId("window-range-exact")).toHaveTextContent("2000000000 → 4000000000");
    expect(screen.getByTestId("metric-chart")).toHaveAttribute("data-window-start", "2000000000");
  });

  it("事件轨道超过上限时先显示 20 条，点按钮再看剩下的", async () => {
    const tracks = Array.from({ length: 25 }, (_, index) =>
      makeEventTrack({
        track_id: `event:systemd:systemd.units:service:unit-${index}.service`,
        entity: `service:unit-${index}.service`,
        items: [makeEvent({ record_id: `event-${index}` })],
      }),
    );
    stubApi({});
    renderTimeline(makeTracksResponse({ tracks }));

    expect(screen.getAllByTestId("track-row")).toHaveLength(20);
    const more = screen.getByTestId("show-more-tracks");
    expect(more).toHaveTextContent("还有 5 条");

    await userEvent.click(more);
    expect(screen.getAllByTestId("track-row")).toHaveLength(25);
  });

  it("关键事件列表最多渲染 200 条，并说明当前窗口的总数", async () => {
    const items = Array.from({ length: 250 }, (_, index) =>
      makeEvent({
        record_id: `event-${index}`,
        monotonic_ns: 1_000_000_000 + index * 1_000_000,
      }),
    );
    stubApi({});
    renderTimeline(
      makeTracksResponse({
        tracks: [makeEventTrack({ items })],
        items,
      }),
    );

    expect(screen.getAllByTestId("event-jump")).toHaveLength(200);
    expect(screen.getByText(/当前窗口共有 250 条事件/)).toBeInTheDocument();
  });
});
