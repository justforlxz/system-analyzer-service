import { describe, expect, it } from "vitest";

import {
  escapeHtml,
  formatBootOffset,
  formatBytes,
  formatDuration,
  formatMetricValue,
  formatNsExact,
  formatWallClockUtc,
} from "@/lib/timeline/format";

describe("formatNsExact", () => {
  it("原样输出纳秒整数，不加千位分隔符", () => {
    expect(formatNsExact(1_200_000_000)).toBe("1200000000");
    expect(formatNsExact(0)).toBe("0");
  });
});

describe("formatBootOffset", () => {
  it("不到一分钟按秒写三位小数", () => {
    expect(formatBootOffset(1_200_000_000)).toBe("T+1.200 s");
    expect(formatBootOffset(0)).toBe("T+0.000 s");
  });

  it("负数表示轴起点之前", () => {
    expect(formatBootOffset(-500_000_000)).toBe("T-0.500 s");
  });

  it("超过一分钟补上分钟", () => {
    expect(formatBootOffset(65_000_000_000)).toBe("T+1 min 5.000 s");
  });
});

describe("formatDuration", () => {
  it("按量级选择单位", () => {
    expect(formatDuration(850)).toBe("850 ns");
    expect(formatDuration(1_500)).toBe("1.5 μs");
    expect(formatDuration(1_500_000)).toBe("1.5 ms");
    expect(formatDuration(8_500_000_000)).toBe("8.500 s");
  });

  it("负数带负号", () => {
    expect(formatDuration(-1_500_000_000)).toBe("-1.500 s");
  });
});

describe("formatWallClockUtc", () => {
  it("按 UTC 写成毫秒精度的墙钟文字", () => {
    const realtimeNs = 1_794_009_600_123_000_000;
    const expected = new Date(1_794_009_600_123).toISOString();
    expect(formatWallClockUtc(realtimeNs)).toBe(
      `${expected.slice(0, 10)} ${expected.slice(11, 19)}.${expected.slice(20, 23)} UTC`,
    );
  });
});

describe("formatBytes", () => {
  it("二进制单位，字节不带小数", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(524_288)).toBe("512.00 KiB");
    expect(formatBytes(4_200_000_000)).toBe("3.91 GiB");
  });

  it("每秒速率加 /s", () => {
    expect(formatBytes(1_048_576, true)).toBe("1.00 MiB/s");
  });

  it("非有限值写占位符", () => {
    expect(formatBytes(Number.NaN)).toBe("-");
  });
});

describe("formatMetricValue", () => {
  it("按单位选择格式", () => {
    expect(formatMetricValue(12.5, "percent")).toBe("12.5 %");
    expect(formatMetricValue(4_200_000_000, "bytes")).toBe("3.91 GiB");
    expect(formatMetricValue(1_048_576, "bytes_per_second")).toBe("1.00 MiB/s");
    expect(formatMetricValue(7, "count")).toBe("7 count");
    expect(formatMetricValue(7, null)).toBe("7");
  });

  it("非有限值写占位符", () => {
    expect(formatMetricValue(Number.POSITIVE_INFINITY, "percent")).toBe("-");
  });
});

describe("escapeHtml", () => {
  it("转义会改变 HTML 结构的字符", () => {
    expect(escapeHtml('<img src=x onerror="alert(1)">')).toBe(
      "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;",
    );
    expect(escapeHtml("a & b 'c'")).toBe("a &amp; b &#39;c&#39;");
  });

  it("普通文字原样返回", () => {
    expect(escapeHtml("12.5 %")).toBe("12.5 %");
    expect(escapeHtml("")).toBe("");
  });
});
