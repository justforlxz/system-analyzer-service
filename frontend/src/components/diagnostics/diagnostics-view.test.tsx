import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DiagnosticsView } from "@/components/diagnostics/diagnostics-view";
import type { ServiceDiagnosticsResponse } from "@/lib/api/client";
import {
  DIAGNOSTICS_AXIS_END,
  DIAGNOSTICS_AXIS_START,
  DIAGNOSTICS_BOOT_ID,
  diagnosticsCursor,
  diagnosticsMachine,
  diagnosticsServices,
  makeDiagnostics,
  makeUnknownDiagnostics,
} from "@/test-fixtures/diagnostics";

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
  diagnostics?: (url: URL) => ServiceDiagnosticsResponse;
  cursor?: (url: URL) => typeof diagnosticsCursor;
};

function stubApi(handlers: Handlers = {}) {
  const calls: URL[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, "http://frontend.test");
    calls.push(url);
    let payload: unknown;
    if (url.pathname.includes("/diagnostics/services/")) {
      payload = handlers.diagnostics?.(url) ?? makeDiagnostics();
    } else if (url.pathname.endsWith("/timeline/cursor")) {
      payload = handlers.cursor?.(url) ?? diagnosticsCursor;
    } else {
      throw new Error(`测试没有为这个请求准备响应：${url.pathname}`);
    }
    return { ok: true, status: 200, json: async () => payload } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

function renderDiagnostics(diagnostics: ServiceDiagnosticsResponse = makeDiagnostics()) {
  return render(
    <DiagnosticsView
      machine={diagnosticsMachine}
      bootId={DIAGNOSTICS_BOOT_ID}
      services={diagnosticsServices}
      initialDiagnostics={diagnostics}
      axis={{ startNs: DIAGNOSTICS_AXIS_START, endNs: DIAGNOSTICS_AXIS_END }}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DiagnosticsView", () => {
  it("展示状态、身份、生命周期、带证据的结论、Journal 与资源", async () => {
    stubApi();
    renderDiagnostics();

    expect(screen.getByTestId("diagnostics-view")).toBeInTheDocument();
    expect(screen.getByText("运行中（active）")).toBeInTheDocument();
    expect(screen.getByText("812")).toBeInTheDocument();
    expect(screen.getByText("inv-1")).toBeInTheDocument();
    expect(screen.getByText("/system.slice/nginx.service")).toBeInTheDocument();

    expect(screen.getAllByTestId("lifecycle-span")).toHaveLength(2);
    expect(screen.getByTestId("diagnostic-annotations")).toHaveTextContent(
      "systemd.service_failed",
    );
    expect(screen.getByTestId("diagnostic-annotations")).toHaveTextContent("置信度 0.8");
    expect(screen.getByTestId("annotation-evidence")).toHaveTextContent("Started nginx.service");
    expect(screen.getAllByTestId("journal-entry")).toHaveLength(2);
    expect(screen.getAllByTestId("resource-latest")).toHaveLength(2);
    expect(screen.getByTestId("diagnostic-resources")).toHaveTextContent("12.5 %");
    expect(screen.getByTestId("metric-chart")).toHaveAttribute("data-points", "2");
  });

  it("缺少关联信息时显示「无法确定」，不给确定性结论", async () => {
    stubApi();
    renderDiagnostics(makeUnknownDiagnostics());

    expect(screen.getByTestId("diagnostic-state")).toHaveTextContent("无法确定（unknown）");
    const facts = screen.getAllByTestId("identity-fact");
    expect(facts).toHaveLength(3);
    for (const fact of facts) {
      expect(fact).toHaveAttribute("data-missing", "true");
      expect(fact).toHaveTextContent("无法确定");
    }
    expect(screen.getByTestId("diagnostic-uncertain")).toHaveTextContent(
      "游标之前没有该服务的生命周期记录，无法确定当时的状态",
    );
    expect(screen.getByTestId("diagnostic-annotations")).toHaveTextContent("无法确定是否存在");
    expect(screen.getByTestId("diagnostic-journal")).toHaveTextContent("无法确定更多信息");
    expect(screen.getByTestId("diagnostic-resources")).toHaveTextContent("无法确定资源使用");
  });

  it("拖动游标按新时刻重新读取诊断结果", async () => {
    const { calls } = stubApi({
      diagnostics: (url) => {
        const atNs = Number(url.searchParams.get("at_ns"));
        return makeDiagnostics({
          at_ns: atNs,
          state: {
            state: atNs >= 5_000_000_000 ? "failed" : "active",
            since_ns: 2_000_000_000,
            record_id: "span-active",
            stale: false,
            note: null,
          },
        });
      },
    });
    renderDiagnostics();

    fireEvent.change(screen.getByTestId("cursor-slider"), {
      target: { value: "5000000000" },
    });

    await waitFor(() => {
      expect(
        calls.some(
          (url) =>
            url.pathname.includes("/diagnostics/services/") &&
            url.searchParams.get("at_ns") === "5000000000",
        ),
      ).toBe(true);
    });
    await waitFor(() =>
      expect(screen.getByTestId("diagnostic-state")).toHaveTextContent("启动失败（failed）"),
    );
  });

  it("下一帧使用时间线接口的 forward，并按返回位置刷新诊断", async () => {
    const { calls } = stubApi({
      cursor: () => ({
        ...diagnosticsCursor,
        position_ns: 5_000_000_000,
        previous_ns: 3_000_000_000,
        next_ns: 6_000_000_000,
      }),
      diagnostics: (url) => makeDiagnostics({ at_ns: Number(url.searchParams.get("at_ns")) }),
    });
    renderDiagnostics();

    await userEvent.click(screen.getByRole("button", { name: "下一帧" }));

    await waitFor(() => {
      const cursorCall = calls.find((url) => url.pathname.endsWith("/timeline/cursor"));
      expect(cursorCall?.searchParams.get("forward")).toBe("true");
      expect(cursorCall?.searchParams.get("at_ns")).toBe("3000000000");
    });
    await waitFor(() => {
      expect(
        calls.some(
          (url) =>
            url.pathname.includes("/diagnostics/services/") &&
            url.searchParams.get("at_ns") === "5000000000",
        ),
      ).toBe(true);
    });
    expect(screen.getByTestId("diagnostic-cursor")).toHaveTextContent("5000000000");
  });

  it("点证据跳到对应 Journal 记录，切换服务后重新读取该服务", async () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    });
    const { calls } = stubApi({
      diagnostics: (url) => {
        const entity = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
        return makeDiagnostics({ entity, unit: entity.replace("service:", "") });
      },
    });
    renderDiagnostics();

    await userEvent.click(screen.getByRole("button", { name: /Started nginx\.service/ }));
    expect(scrollIntoView).toHaveBeenCalled();
    expect(screen.getAllByTestId("journal-entry")[0]).toHaveAttribute("data-highlighted", "true");

    fireEvent.change(screen.getByTestId("service-select"), {
      target: { value: "service:firefox.service" },
    });
    await waitFor(() => {
      expect(
        calls.some((url) => decodeURIComponent(url.pathname).includes("service:firefox.service")),
      ).toBe(true);
    });
  });
});
