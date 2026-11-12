import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

import { panWindow, zoomWindow } from "../src/lib/timeline/scale";

type FixtureItem = {
  type: "event" | "span" | "metric";
  record_id: string;
  monotonic_ns: number | null;
  end_monotonic_ns?: number | null;
};

type FixtureTracks = {
  axis_start_ns: number;
  axis_end_ns: number;
  items: FixtureItem[];
  tracks: { track_id: string; status: string; unavailable_reason: string | null }[];
  resources: { available: boolean | null; reasons: string[] };
};

function fixture<T>(name: string): T {
  return JSON.parse(
    readFileSync(new URL(`./mock-api/fixtures/${name}.json`, import.meta.url), "utf8"),
  ) as T;
}

const MACHINE_ID = "machine:uos-PC";
const BOOT = {
  full: "boot-full",
  journalOnly: "boot-journal-only",
  resourcesMissing: "boot-resources",
};

function timelineUrl(bootId: string): string {
  return `/timeline?machine_id=${encodeURIComponent(MACHINE_ID)}&boot_id=${encodeURIComponent(bootId)}`;
}

/** 可停靠时刻：条目时刻加上区间的结束时刻，与后端的 `step_cursor` 口径一致。 */
function stopTimesOf(tracks: FixtureTracks): number[] {
  const times = new Set<number>();
  for (const item of tracks.items) {
    if (item.monotonic_ns !== null) times.add(item.monotonic_ns);
    if (item.type === "span" && item.end_monotonic_ns) times.add(item.end_monotonic_ns);
  }
  return [...times].sort((left, right) => left - right);
}

function axisOf(tracks: FixtureTracks) {
  return { startNs: tracks.axis_start_ns, endNs: tracks.axis_end_ns };
}

/** 游标线的横坐标与绘图区比例一致（同一个时间游标贯穿所有轨道）。 */
async function expectCursorAligned(
  page: Page,
  positionNs: number,
  axis: { startNs: number; endNs: number },
) {
  const geometry = await page.evaluate(() => {
    const plot = document.querySelector('[data-testid="track-plot"]');
    const line = document.querySelector('[data-testid="shared-cursor"]');
    if (!plot || !line) return null;
    const plotBox = plot.getBoundingClientRect();
    const lineBox = line.getBoundingClientRect();
    return { plotLeft: plotBox.left, plotWidth: plotBox.width, lineLeft: lineBox.left };
  });
  expect(geometry).not.toBeNull();
  const fraction = (positionNs - axis.startNs) / (axis.endNs - axis.startNs);
  const expectedLeft = geometry!.plotLeft + fraction * geometry!.plotWidth;
  expect(Math.abs(geometry!.lineLeft - expectedLeft)).toBeLessThan(2);
}

test.describe("统一时间线回放", () => {
  test("多轨道共用一个游标，换帧后所有轨道同步更新", async ({ page }) => {
    const tracks = fixture<FixtureTracks>("full-tracks");
    const times = stopTimesOf(tracks);
    await page.goto(timelineUrl(BOOT.full));

    await expect(page.getByTestId("timeline-replay")).toBeVisible();
    await expect(page.getByTestId("track-row")).toHaveCount(tracks.tracks.length);
    await expect(page.getByTestId("shared-cursor")).toHaveCount(1);
    await expect(page.getByTestId("cursor-position-exact")).toHaveText(String(times[0]));
    await expectCursorAligned(page, times[0], axisOf(tracks));

    // 三类轨道都在：生命周期（SVG）、事件（SVG）、资源（ECharts 画布）
    expect(await page.getByTestId("span-track").count()).toBeGreaterThan(0);
    expect(await page.getByTestId("event-track").count()).toBeGreaterThan(0);
    expect(await page.getByTestId("metric-chart").count()).toBeGreaterThan(0);
    expect(await page.locator('[data-testid="metric-chart"] canvas').count()).toBeGreaterThan(0);

    const readouts = page.getByTestId("track-cursor-readout");
    await expect(readouts).toHaveCount(tracks.tracks.length);
    const before = await readouts.allTextContents();

    await page.getByTestId("cursor-next").click();

    await expect(page.getByTestId("cursor-position-exact")).toHaveText(String(times[1]));
    await expectCursorAligned(page, times[1], axisOf(tracks));
    await expect(page.getByTestId("shared-cursor")).toHaveCount(1);
    // 每条轨道的读数都随这同一个游标刷新，不是各画各的线
    await expect
      .poll(async () => (await readouts.allTextContents()).join("\u0000"))
      .not.toBe(before.join("\u0000"));
    expect((await readouts.allTextContents()).every((text) => text.length > 0)).toBe(true);
  });

  test("缩放后的窗口值与后端回显逐项一致", async ({ page }) => {
    const tracks = fixture<FixtureTracks>("full-tracks");
    const times = stopTimesOf(tracks);
    const axis = axisOf(tracks);
    await page.goto(timelineUrl(BOOT.full));
    await expect(page.getByTestId("timeline-replay")).toBeVisible();

    await page.getByTestId("cursor-next").click();
    await expect(page.getByTestId("cursor-position-exact")).toHaveText(String(times[1]));

    const zoomed = zoomWindow(axis, 0.5, times[1], axis);
    await page.getByTestId("zoom-in").click();
    await expect(page.getByTestId("window-range-exact")).toHaveText(
      `${zoomed.startNs} → ${zoomed.endNs}`,
    );
    // 轴范围不随缩放变化：后端 axis_* 是整次启动的范围
    await expect(page.getByTestId("axis-range-exact")).toHaveText(
      `${axis.startNs} → ${axis.endNs}`,
    );
    await expectCursorAligned(page, times[1], zoomed);

    // 再缩小一次回到整条轴
    await page.getByTestId("zoom-reset").click();
    await expect(page.getByTestId("window-range-exact")).toHaveText(
      `${axis.startNs} → ${axis.endNs}`,
    );
    await expectCursorAligned(page, times[1], axis);
  });

  test("拖动轨道平移窗口，窗口起点按拖动距离变化", async ({ page }) => {
    const tracks = fixture<FixtureTracks>("full-tracks");
    const times = stopTimesOf(tracks);
    const axis = axisOf(tracks);
    await page.goto(timelineUrl(BOOT.full));
    await expect(page.getByTestId("timeline-replay")).toBeVisible();

    await page.getByTestId("cursor-next").click();
    await page.getByTestId("zoom-in").click();
    const zoomed = zoomWindow(axis, 0.5, times[1], axis);
    await expect(page.getByTestId("window-range-exact")).toHaveText(
      `${zoomed.startNs} → ${zoomed.endNs}`,
    );

    const plot = page.getByTestId("track-plot").first();
    const box = await plot.boundingBox();
    expect(box).not.toBeNull();
    const dragDistance = 120;
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + box!.width / 2 - dragDistance, box!.y + box!.height / 2, {
      steps: 6,
    });
    await page.mouse.up();

    const expected = panWindow(
      zoomed,
      (dragDistance / box!.width) * (zoomed.endNs - zoomed.startNs),
      axis,
    );
    await expect(page.getByTestId("window-range-exact")).toHaveText(
      `${expected.startNs} → ${expected.endNs}`,
    );
  });

  test("点关键事件把游标定位到事件时刻", async ({ page }) => {
    const tracks = fixture<FixtureTracks>("full-tracks");
    const events = tracks.items
      .filter((item) => item.type === "event" && item.monotonic_ns !== null)
      .sort((left, right) => (left.monotonic_ns ?? 0) - (right.monotonic_ns ?? 0));
    expect(events.length).toBeGreaterThan(0);

    await page.goto(timelineUrl(BOOT.full));
    await expect(page.getByTestId("event-list")).toBeVisible();
    await page.getByTestId("event-jump").first().click();

    await expect(page.getByTestId("cursor-position-exact")).toHaveText(
      String(events[0].monotonic_ns),
    );
    await expectCursorAligned(page, events[0].monotonic_ns as number, axisOf(tracks));
  });

  test("Journal-only 数据可以回放，资源轨道显示不可用与原因", async ({ page }) => {
    const tracks = fixture<FixtureTracks>("journal-only-tracks");
    await page.goto(timelineUrl(BOOT.journalOnly));

    await expect(page.getByTestId("track-row")).toHaveCount(tracks.tracks.length);
    const notice = page.getByTestId("resources-unavailable");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("资源数据不可用");
    for (const reason of tracks.resources.reasons) {
      await expect(notice).toContainText(reason);
    }
    // 没有资源数据时不画空图，也不把缺失写成 0
    await expect(page.getByTestId("metric-chart")).toHaveCount(0);
    await expect(page.getByTestId("event-track")).toHaveCount(tracks.tracks.length);
    await expect(page.getByTestId("shared-cursor")).toHaveCount(1);
    await expect(page.getByTestId("cursor-position-exact")).toHaveText(
      String(stopTimesOf(tracks)[0]),
    );
  });

  test("「无数据」与「不可用」在文案与样式上可区分", async ({ page }) => {
    const tracks = fixture<FixtureTracks>("resources-missing-tracks");
    await page.goto(timelineUrl(BOOT.resourcesMissing));

    const unavailable = tracks.tracks.filter((track) => track.status === "unavailable");
    expect(unavailable.length).toBeGreaterThan(0);

    const placeholders = page.getByTestId("track-placeholder");
    await expect(placeholders).toHaveCount(unavailable.length + 1);
    await expect(
      page.locator('[data-testid="track-placeholder"][data-status="unavailable"]'),
    ).toHaveCount(unavailable.length);
    await expect(
      page.locator('[data-testid="track-placeholder"][data-status="no_data"]'),
    ).toHaveCount(1);

    for (const track of unavailable) {
      await expect(page.getByText(`不可用：${track.unavailable_reason}`)).toBeVisible();
    }
    await expect(page.getByText(/无数据：这次启动里没有这条流的任何记录/)).toBeVisible();
    // 占位不是 0 值图
    await expect(
      page.locator('[data-testid="track-placeholder"][data-status="no_data"]'),
    ).not.toHaveText(/^\s*0/);
    await expect(page.getByTestId("metric-chart").first()).toBeVisible();
  });

  test("启动选择页列出可回放的启动", async ({ page }) => {
    await page.goto("/timeline");

    await expect(page.getByTestId("boot-picker")).toBeVisible();
    const link = page.locator('a[href*="boot_id=boot-full"]');
    await expect(link).toBeVisible();
    await link.click();
    await expect(page.getByTestId("timeline-replay")).toBeVisible();
    await expect(page.getByTestId("shared-cursor")).toHaveCount(1);
  });
});
