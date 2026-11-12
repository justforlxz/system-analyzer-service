import { describe, expect, it } from "vitest";

import type { TrackResponse, TracksResponse } from "@/lib/api/client";
import {
  clipSpan,
  groupTracks,
  isOpenSpan,
  resourcesNotice,
  spanEndLabel,
  trackCursorReadout,
  trackLabel,
  trackSourceLabel,
  trackStatusView,
} from "@/lib/timeline/tracks";
import {
  AXIS_START,
  makeEvent,
  makeEventTrack,
  makeMetric,
  makeMetricTrack,
  makeSpan,
  makeSpanTrack,
  makeTrackState,
  resourcesAvailable,
  resourcesUnavailable,
} from "@/test-fixtures/timeline";

const window = { startNs: AXIS_START, endNs: 4_000_000_000 };

describe("groupTracks", () => {
  it("按生命周期、事件、资源的顺序分组，组内保留后端顺序", () => {
    const groups = groupTracks([
      makeMetricTrack(),
      makeSpanTrack({ track_id: "span:b" }),
      makeSpanTrack({ track_id: "span:a" }),
      makeEventTrack(),
    ]);

    expect(groups.map((group) => group.key)).toEqual(["span", "event", "metric"]);
    expect(groups[0].tracks.map((track) => track.track_id)).toEqual(["span:b", "span:a"]);
    expect(groups[0].title).toBe("Systemd Unit 生命周期");
  });

  it("没有轨道的分组不出现", () => {
    expect(groupTracks([makeMetricTrack()]).map((group) => group.key)).toEqual(["metric"]);
    expect(groupTracks([])).toEqual([]);
  });
});

describe("trackLabel", () => {
  it("指标用后端给的 name", () => {
    expect(trackLabel(makeMetricTrack())).toBe("cpu.total.utilization");
  });

  it("其它种类用实体名去掉类型前缀", () => {
    expect(trackLabel(makeSpanTrack())).toBe("nginx.service");
    expect(trackLabel(makeEventTrack())).toBe("nginx.service");
  });

  it("没有实体时退回 stream_id，再退回 track_id", () => {
    const track: TrackResponse = {
      ...makeSpanTrack(),
      entity: null,
      stream_id: "systemd.units",
    };
    expect(trackLabel(track)).toBe("systemd.units");
    expect(trackLabel({ ...track, stream_id: null })).toBe(track.track_id);
  });
});

describe("trackSourceLabel", () => {
  it("写出逻辑流与来源插件", () => {
    expect(trackSourceLabel(makeSpanTrack())).toBe("systemd.units · systemd");
    expect(trackSourceLabel({ ...makeMetricTrack(), stream_id: null, source_plugin: null })).toBe(
      "未标流 · 未标来源",
    );
  });
});

describe("trackStatusView", () => {
  it("available 显示有数据", () => {
    const view = trackStatusView(makeMetricTrack());
    expect(view).toEqual({ key: "available", badge: "有数据", text: "有数据", tone: "ok" });
  });

  it("unavailable 必须带上后端给出的原因", () => {
    const view = trackStatusView({
      ...makeMetricTrack(),
      status: "unavailable",
      items: [],
      unavailable_reason: "目标机器没有 iostat，磁盘 I/O 未采集",
    });
    expect(view.key).toBe("unavailable");
    expect(view.text).toBe("不可用：目标机器没有 iostat，磁盘 I/O 未采集");
    expect(view.badge).toBe("不可用");
    expect(view.tone).toBe("warn");
  });

  it("no_data 的文案与 unavailable 不同，且不写成 0", () => {
    const view = trackStatusView({ ...makeMetricTrack(), status: "no_data", items: [] });
    expect(view.key).toBe("no_data");
    expect(view.badge).toBe("无数据");
    expect(view.text).toContain("无数据");
    expect(view.text).not.toContain("不可用");
    expect(view.text).not.toContain("0");
    expect(view.tone).toBe("muted");
  });
});

describe("resourcesNotice", () => {
  it("整次启动没有资源数据时给出不可用提示与原因", () => {
    const notice = resourcesNotice(resourcesUnavailable);
    expect(notice).toEqual({
      title: "资源数据不可用",
      reasons: resourcesUnavailable.reasons,
      tone: "warn",
    });
  });

  it("资源可用但有说明时用提示口吻", () => {
    const notice = resourcesNotice({
      available: true,
      reasons: ["有一次采集没有采集资源数据"],
    });
    expect(notice?.tone).toBe("info");
    expect(notice?.reasons).toHaveLength(1);
  });

  it("没有任何说明时不显示", () => {
    expect(resourcesNotice(resourcesAvailable)).toBeNull();
  });
});

describe("区间结束", () => {
  it("记录自己声明结束时不是开放区间", () => {
    const span = makeSpan();
    expect(isOpenSpan(span)).toBe(false);
    expect(spanEndLabel(span)).toBe("");
  });

  it("没有声明结束是未闭合", () => {
    const span = makeSpan({
      end_monotonic_ns: null,
      end_realtime_ns: null,
      end_is_declared: false,
    });
    expect(isOpenSpan(span)).toBe(true);
    expect(spanEndLabel(span)).toBe("未闭合：采集结束时仍在进行");
  });

  it("只有墙钟声明结束时标为结束时刻未知", () => {
    const span = makeSpan({ end_monotonic_ns: null, end_is_declared: true });
    expect(isOpenSpan(span)).toBe(true);
    expect(spanEndLabel(span)).toBe("结束时刻未知：只有墙钟收尾");
  });
});

describe("clipSpan", () => {
  it("窗口内的区间按比例换算", () => {
    const clipped = clipSpan(
      makeSpan({ monotonic_ns: 1_000_000_000, end_monotonic_ns: 3_000_000_000 }),
      {
        startNs: 1_000_000_000,
        endNs: 5_000_000_000,
      },
    );
    expect(clipped).not.toBeNull();
    expect(clipped?.startFraction).toBe(0);
    expect(clipped?.endFraction).toBe(0.5);
    expect(clipped?.openEnd).toBe(false);
  });

  it("未闭合的区间用窗口终点收尾并标记 openEnd", () => {
    const clipped = clipSpan(
      makeSpan({ monotonic_ns: 2_000_000_000, end_monotonic_ns: null, end_is_declared: false }),
      window,
    );
    expect(clipped?.openEnd).toBe(true);
    expect(clipped?.endFraction).toBe(1);
  });

  it("从窗口之前开始、在窗口之内结束的区间要保留，并标记左边被裁", () => {
    const clipped = clipSpan(
      makeSpan({ monotonic_ns: 0, end_monotonic_ns: 3_000_000_000 }),
      window,
    );
    expect(clipped?.startFraction).toBe(0);
    expect(clipped?.endFraction).toBeCloseTo(2 / 3, 12);
    expect(clipped?.clippedStart).toBe(true);
    expect(clipped?.clippedEnd).toBe(false);
  });

  it("与窗口没有交集时返回 null", () => {
    expect(
      clipSpan(makeSpan({ monotonic_ns: 8_000_000_000, end_monotonic_ns: 9_000_000_000 }), window),
    ).toBeNull();
  });

  it("没有 boot 内位置的区间不画", () => {
    expect(clipSpan(makeSpan({ monotonic_ns: null }), window)).toBeNull();
  });
});

describe("trackCursorReadout", () => {
  it("指标轨道显示该时刻或之前最近的采样值", () => {
    const track = makeMetricTrack();
    expect(trackCursorReadout(track, makeTrackState())).toBe("30.3 %");
  });

  it("指标轨道在没有任何采样时说明此前没有采样点，而不是 0", () => {
    const track = makeMetricTrack();
    const readout = trackCursorReadout(track, makeTrackState({ metric: null }));
    expect(readout).toBe("此时刻之前没有采样点");
    expect(readout).not.toContain("0");
  });

  it("区间轨道显示正在进行的区间状态", () => {
    const readout = trackCursorReadout(
      makeSpanTrack(),
      makeTrackState({ kind: "span", metric: null, spans: [makeSpan({ state: "failed" })] }),
    );
    expect(readout).toBe("进行中：failed");
  });

  it("区间轨道没有进行中的区间时如实说明", () => {
    expect(
      trackCursorReadout(
        makeSpanTrack(),
        makeTrackState({ kind: "span", metric: null, spans: [] }),
      ),
    ).toBe("此时刻没有进行中的区间");
  });

  it("事件轨道显示最近一个事件", () => {
    const readout = trackCursorReadout(
      makeEventTrack(),
      makeTrackState({ kind: "event", metric: null, last_event: makeEvent() }),
    );
    expect(readout).toBe("systemd.unit.started");
  });

  it("不可用与无数据的轨道给出对应说明", () => {
    const unavailable = trackCursorReadout(
      { ...makeMetricTrack(), status: "unavailable", items: [] },
      undefined,
    );
    const noData = trackCursorReadout(
      { ...makeMetricTrack(), status: "no_data", items: [] },
      undefined,
    );
    expect(unavailable).toBe("不可用：没有采样点");
    expect(noData).toBe("无数据：这次启动里没有这条流的记录");
  });

  it("状态还没取回来时显示读取中", () => {
    expect(trackCursorReadout(makeMetricTrack(), undefined)).toBe("读取中…");
  });
});

describe("轨道数据形状", () => {
  it("指标点与轨道解耦：轨道的 items 只放这条轨道的条目", () => {
    const metric = makeMetric();
    const tracks: TracksResponse = {
      machine_id: "machine:x",
      boot_id: "boot-x",
      axis_start_ns: 0,
      axis_end_ns: 1,
      window_start_ns: null,
      window_end_ns: null,
      tracks: [makeMetricTrack({ items: [metric] })],
      items: [metric],
      wall_clock_items: [],
      resources: resourcesAvailable,
      clock: { anchor_ns: null, drift_ns: null, anchor_count: 0, unreliable: false },
    };
    expect(tracks.tracks[0].items[0].type).toBe("metric");
  });
});
