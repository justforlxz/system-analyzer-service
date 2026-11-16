import { afterEach, describe, expect, it, vi } from "vitest";

import {
  parseCursorNs,
  parseWindowParams,
  resolveTimelineWindow,
  writeCursorToUrl,
} from "@/lib/timeline/cursor-url";
import { tailWindow, windowForCursor } from "@/lib/timeline/scale";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("parseCursorNs", () => {
  it("接受非负整数（含 0）", () => {
    expect(parseCursorNs("1200000000")).toBe(1_200_000_000);
    expect(parseCursorNs("0")).toBe(0);
    expect(parseCursorNs(" 42 ")).toBe(42);
  });

  it("空值、非法值、负数、小数与超出安全整数的值按未提供处理", () => {
    expect(parseCursorNs(null)).toBeNull();
    expect(parseCursorNs("")).toBeNull();
    expect(parseCursorNs("abc")).toBeNull();
    expect(parseCursorNs("-1")).toBeNull();
    expect(parseCursorNs("1.5")).toBeNull();
    expect(parseCursorNs("9007199254740992")).toBeNull();
  });
});

describe("parseWindowParams", () => {
  const axis = { startNs: 1_000_000_000, endNs: 9_000_000_000 };

  it("接受完整落在轴内且起点小于终点的窗口", () => {
    expect(parseWindowParams("2000000000", "3000000000", axis)).toEqual({
      startNs: 2_000_000_000,
      endNs: 3_000_000_000,
    });
  });

  it("缺一个参数、起点不小于终点、负数或超出轴范围时都不采用", () => {
    expect(parseWindowParams(null, "3000000000", axis)).toBeNull();
    expect(parseWindowParams("2000000000", null, axis)).toBeNull();
    expect(parseWindowParams("3000000000", "3000000000", axis)).toBeNull();
    expect(parseWindowParams("4000000000", "3000000000", axis)).toBeNull();
    expect(parseWindowParams("-1", "3000000000", axis)).toBeNull();
    expect(parseWindowParams("0", "3000000000", axis)).toBeNull();
    expect(parseWindowParams("2000000000", "9000000001", axis)).toBeNull();
    expect(parseWindowParams("abc", "3000000000", axis)).toBeNull();
    expect(parseWindowParams("1.5", "3000000000", axis)).toBeNull();
  });
});

describe("resolveTimelineWindow", () => {
  const axis = { startNs: 1_000_000_000, endNs: 9_000_000_000 };
  const longAxis = { startNs: 0, endNs: 300_000_000_000 };

  it("地址栏有合法窗口时直接采用，不按 at_ns 重算", () => {
    expect(
      resolveTimelineWindow({
        axis,
        atNs: 2_000_000_000,
        windowStartRaw: "3000000000",
        windowEndRaw: "4000000000",
      }),
    ).toEqual({ startNs: 3_000_000_000, endNs: 4_000_000_000 });
  });

  it("窗口非法时回落到按 at_ns 选窗口的规则", () => {
    expect(
      resolveTimelineWindow({
        axis: longAxis,
        atNs: 100_000_000_000,
        windowStartRaw: "5000000000",
        windowEndRaw: "4000000000",
      }),
    ).toEqual(windowForCursor(100_000_000_000, longAxis));
  });

  it("没有窗口也没有 at_ns 时用尾窗口", () => {
    expect(
      resolveTimelineWindow({
        axis: longAxis,
        atNs: null,
        windowStartRaw: null,
        windowEndRaw: null,
      }),
    ).toEqual(tailWindow(longAxis));
  });
});

describe("writeCursorToUrl", () => {
  it("写入 at_ns 时保留其它参数，也不新增历史记录", () => {
    window.history.replaceState(
      null,
      "",
      "/timeline?machine_id=machine%3Auos-PC&boot_id=boot-1&keep=yes",
    );
    const historyLength = window.history.length;

    writeCursorToUrl({ atNs: 5_000_000_000 });

    const params = new URLSearchParams(window.location.search);
    expect(params.get("at_ns")).toBe("5000000000");
    expect(params.get("machine_id")).toBe("machine:uos-PC");
    expect(params.get("boot_id")).toBe("boot-1");
    expect(params.get("keep")).toBe("yes");
    expect(window.location.pathname).toBe("/timeline");
    expect(window.history.length).toBe(historyLength);
  });

  it("游标为 null 时删掉 at_ns", () => {
    window.history.replaceState(null, "", "/timeline?machine_id=m&boot_id=b&at_ns=7");

    writeCursorToUrl({ atNs: null });

    expect(new URLSearchParams(window.location.search).has("at_ns")).toBe(false);
  });

  it("传了 entity 就写入，传 null 就删掉", () => {
    window.history.replaceState(null, "", "/diagnostics?machine_id=m&boot_id=b&at_ns=7");

    writeCursorToUrl({ atNs: 7, entity: "service:nginx.service" });
    expect(new URLSearchParams(window.location.search).get("entity")).toBe("service:nginx.service");

    writeCursorToUrl({ atNs: 7, entity: null });
    expect(new URLSearchParams(window.location.search).has("entity")).toBe(false);
  });

  it("地址没有变化时不调用 replaceState", () => {
    window.history.replaceState(null, "", "/timeline?machine_id=m&boot_id=b&at_ns=7");
    const replace = vi.spyOn(window.history, "replaceState");

    writeCursorToUrl({ atNs: 7 });

    expect(replace).not.toHaveBeenCalled();
  });

  it("写入窗口时保留 at_ns 与其它参数，也不新增历史记录", () => {
    window.history.replaceState(
      null,
      "",
      "/timeline?machine_id=machine%3Auos-PC&boot_id=boot-1&at_ns=5000000000&keep=yes",
    );
    const historyLength = window.history.length;

    writeCursorToUrl({
      atNs: 5_000_000_000,
      window: { startNs: 4_000_000_000, endNs: 6_000_000_000 },
    });

    const params = new URLSearchParams(window.location.search);
    expect(params.get("at_ns")).toBe("5000000000");
    expect(params.get("window_start_ns")).toBe("4000000000");
    expect(params.get("window_end_ns")).toBe("6000000000");
    expect(params.get("machine_id")).toBe("machine:uos-PC");
    expect(params.get("keep")).toBe("yes");
    expect(window.history.length).toBe(historyLength);
  });

  it("窗口为 null 时删掉两个窗口参数", () => {
    window.history.replaceState(
      null,
      "",
      "/timeline?machine_id=m&boot_id=b&at_ns=7&window_start_ns=1&window_end_ns=2",
    );

    writeCursorToUrl({ atNs: 7, window: null });

    const params = new URLSearchParams(window.location.search);
    expect(params.has("window_start_ns")).toBe(false);
    expect(params.has("window_end_ns")).toBe(false);
    expect(params.get("at_ns")).toBe("7");
  });

  it("不传窗口时保持地址栏里的窗口参数不变", () => {
    window.history.replaceState(
      null,
      "",
      "/diagnostics?machine_id=m&boot_id=b&window_start_ns=1&window_end_ns=2",
    );

    writeCursorToUrl({ atNs: 7, entity: "service:nginx.service" });

    const params = new URLSearchParams(window.location.search);
    expect(params.get("window_start_ns")).toBe("1");
    expect(params.get("window_end_ns")).toBe("2");
    expect(params.get("at_ns")).toBe("7");
  });

  it("窗口与游标都没变化时不调用 replaceState", () => {
    window.history.replaceState(
      null,
      "",
      "/timeline?machine_id=m&boot_id=b&at_ns=7&window_start_ns=1&window_end_ns=2",
    );
    const replace = vi.spyOn(window.history, "replaceState");

    writeCursorToUrl({ atNs: 7, window: { startNs: 1, endNs: 2 } });

    expect(replace).not.toHaveBeenCalled();
  });
});
