import { describe, expect, it } from "vitest";

import {
  clampWindow,
  fractionToNs,
  isInsideWindow,
  MIN_WINDOW_NS,
  nsToFraction,
  panWindow,
  windowAround,
  windowSpan,
  zoomWindow,
} from "@/lib/timeline/scale";

const axis = { startNs: 1_000_000_000, endNs: 9_000_000_000 };

describe("nsToFraction / fractionToNs", () => {
  it("把窗口两端映射到 0 与 1", () => {
    expect(nsToFraction(axis.startNs, axis)).toBe(0);
    expect(nsToFraction(axis.endNs, axis)).toBe(1);
  });

  it("中间时刻按线性比例映射，并能反算回原值", () => {
    const middle = 5_000_000_000;
    expect(nsToFraction(middle, axis)).toBeCloseTo(0.5, 12);
    expect(fractionToNs(0.5, axis)).toBe(middle);
    expect(fractionToNs(nsToFraction(3_250_000_000, axis), axis)).toBe(3_250_000_000);
  });

  it("起点与终点相同的窗口按跨度 1 处理，不做除零", () => {
    const instant = { startNs: 7, endNs: 7 };
    expect(windowSpan(instant)).toBe(1);
    expect(nsToFraction(7, instant)).toBe(0);
    expect(fractionToNs(1, instant)).toBe(8);
  });
});

describe("isInsideWindow", () => {
  it("含两端", () => {
    expect(isInsideWindow(axis.startNs, axis)).toBe(true);
    expect(isInsideWindow(axis.endNs, axis)).toBe(true);
    expect(isInsideWindow(axis.startNs - 1, axis)).toBe(false);
    expect(isInsideWindow(axis.endNs + 1, axis)).toBe(false);
  });
});

describe("clampWindow", () => {
  it("窗口完全在轴内时原样返回", () => {
    expect(clampWindow(2_000_000_000, 3_000_000_000, axis)).toEqual({
      startNs: 2_000_000_000,
      endNs: 3_000_000_000,
    });
  });

  it("越界时整体移进轴内，跨度不变", () => {
    expect(clampWindow(-5_000_000_000, 0, axis)).toEqual({
      startNs: axis.startNs,
      endNs: axis.startNs + 5_000_000_000,
    });
    expect(clampWindow(8_000_000_000, 12_000_000_000, axis)).toEqual({
      startNs: axis.endNs - 4_000_000_000,
      endNs: axis.endNs,
    });
  });

  it("跨度小于最小跨度时放大到最小跨度", () => {
    const clamped = clampWindow(5_000_000_000, 5_000_000_001, axis);
    expect(windowSpan(clamped)).toBe(MIN_WINDOW_NS);
  });

  it("跨度大于整条轴时收成整条轴", () => {
    expect(clampWindow(-1, 1e15, axis)).toEqual(axis);
  });

  it("退化轴（起止相同）返回该点，不产生负跨度", () => {
    const instant = { startNs: 5, endNs: 5 };
    expect(clampWindow(0, 10, instant)).toEqual({ startNs: 5, endNs: 5 });
  });
});

describe("zoomWindow", () => {
  it("以锚点为不动点把跨度减半", () => {
    const zoomed = zoomWindow(axis, 0.5, 5_000_000_000, axis);
    expect(zoomed).toEqual({ startNs: 3_000_000_000, endNs: 7_000_000_000 });
  });

  it("锚点在缩放前后的相对位置一致", () => {
    const anchor = 3_000_000_000;
    const before = nsToFraction(anchor, axis);
    const zoomed = zoomWindow(axis, 0.5, anchor, axis);
    expect(nsToFraction(anchor, zoomed)).toBeCloseTo(before, 12);
  });

  it("放大到最小跨度后不再继续缩小范围", () => {
    let range = zoomWindow(axis, 0.001, 5_000_000_000, axis);
    range = zoomWindow(range, 0.001, 5_000_000_000, axis);
    expect(windowSpan(range)).toBe(MIN_WINDOW_NS);
  });

  it("缩小不会超过整条轴", () => {
    expect(zoomWindow(axis, 100, 5_000_000_000, axis)).toEqual(axis);
  });

  it("锚点在窗口外时仍贴边缩放，不越出轴", () => {
    const zoomed = zoomWindow(axis, 0.5, axis.startNs - 1_000_000_000, axis);
    expect(zoomed.startNs).toBe(axis.startNs);
    expect(windowSpan(zoomed)).toBe(4_000_000_000);
  });
});

describe("panWindow", () => {
  it("正数向右平移", () => {
    const range = { startNs: 3_000_000_000, endNs: 5_000_000_000 };
    expect(panWindow(range, 1_000_000_000, axis)).toEqual({
      startNs: 4_000_000_000,
      endNs: 6_000_000_000,
    });
  });

  it("贴到轴左端时不再向左", () => {
    const range = { startNs: 3_000_000_000, endNs: 5_000_000_000 };
    expect(panWindow(range, -1e12, axis)).toEqual({
      startNs: axis.startNs,
      endNs: axis.startNs + 2_000_000_000,
    });
  });

  it("贴到轴右端时不再向右", () => {
    const range = { startNs: 3_000_000_000, endNs: 5_000_000_000 };
    expect(panWindow(range, 1e12, axis)).toEqual({
      startNs: axis.endNs - 2_000_000_000,
      endNs: axis.endNs,
    });
  });
});

describe("windowAround", () => {
  it("以指定时刻为中心保持跨度", () => {
    const moved = windowAround(5_000_000_000, 2_000_000_000, axis);
    expect(moved).toEqual({ startNs: 4_000_000_000, endNs: 6_000_000_000 });
  });

  it("靠近轴起点时贴边", () => {
    const moved = windowAround(axis.startNs, 2_000_000_000, axis);
    expect(moved).toEqual({ startNs: axis.startNs, endNs: axis.startNs + 2_000_000_000 });
  });
});
