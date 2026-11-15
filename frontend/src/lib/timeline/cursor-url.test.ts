import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCursorNs, writeCursorToUrl } from "@/lib/timeline/cursor-url";

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
});
