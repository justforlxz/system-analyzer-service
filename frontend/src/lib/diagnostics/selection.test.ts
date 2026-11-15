import { describe, expect, it } from "vitest";

import type { ServiceSummary } from "@/lib/api/client";
import { defaultService, resolveDiagnosticsSelection } from "@/lib/diagnostics/selection";

function service(
  unit: string,
  values: {
    annotationCount?: number;
    startNs?: number | null;
    endNs?: number | null;
  } = {},
): ServiceSummary {
  return {
    entity: `service:${unit}`,
    unit,
    lifecycle_start_ns: values.startNs ?? null,
    lifecycle_end_ns: values.endNs ?? null,
    annotation_count: values.annotationCount ?? 0,
  };
}

describe("defaultService", () => {
  it("三类服务同时存在时选中有生命周期且有结论的服务", () => {
    const services = [
      service("aaa.service"),
      service("bbb.service", { startNs: 1_000, endNs: 2_000 }),
      service("zzz.service", { annotationCount: 2, startNs: 3_000, endNs: 4_000 }),
    ];

    expect(defaultService(services)?.entity).toBe("service:zzz.service");
  });

  it("只有结论没有生命周期区间的服务不参与优先选择", () => {
    const services = [
      service("aaa.service", { annotationCount: 9 }),
      service("bbb.service", { startNs: 1_000, endNs: 2_000 }),
    ];

    expect(defaultService(services)?.entity).toBe("service:bbb.service");
  });

  it("有生命周期的服务都没有结论时按 unit 字母序选第一个", () => {
    const services = [
      service("zeta.service", { startNs: 5_000 }),
      service("alpha.service", { startNs: 1_000 }),
      service("mmm.service", { startNs: 3_000 }),
    ];

    expect(defaultService(services)?.entity).toBe("service:alpha.service");
  });

  it("同为最大结论数时按 unit 字母序选第一个", () => {
    const services = [
      service("zeta.service", { annotationCount: 4, startNs: 5_000 }),
      service("alpha.service", { annotationCount: 4, startNs: 1_000 }),
      service("beta.service", { annotationCount: 2, startNs: 3_000 }),
    ];

    expect(defaultService(services)?.entity).toBe("service:alpha.service");
  });

  it("没有任何服务有生命周期区间时回退列表第一个", () => {
    const services = [
      service("alpha.service", { annotationCount: 3 }),
      service("zeta.service", { annotationCount: 1 }),
    ];

    expect(defaultService(services)?.entity).toBe("service:alpha.service");
  });

  it("服务列表为空时没有默认服务", () => {
    expect(defaultService([])).toBeNull();
  });
});

describe("resolveDiagnosticsSelection", () => {
  const services = [
    service("aaa.service"),
    service("bbb.service", { startNs: 1_000, endNs: 4_000 }),
    service("ccc.service", { annotationCount: 1, startNs: 2_000, endNs: 5_000 }),
  ];

  it("没有地址参数时选默认服务并停在轴终点", () => {
    expect(
      resolveDiagnosticsSelection({
        services,
        requestedEntity: null,
        requestedAtNs: null,
        axisEndNs: 9_000,
      }),
    ).toEqual({ entity: "service:ccc.service", atNs: 9_000 });
  });

  it("地址带 entity 与 at_ns 时原样使用，不覆盖", () => {
    expect(
      resolveDiagnosticsSelection({
        services,
        requestedEntity: "service:aaa.service",
        requestedAtNs: 3_210,
        axisEndNs: 9_000,
      }),
    ).toEqual({ entity: "service:aaa.service", atNs: 3_210 });
  });

  it("只带 entity 时时刻仍取轴终点", () => {
    expect(
      resolveDiagnosticsSelection({
        services,
        requestedEntity: "service:bbb.service",
        requestedAtNs: null,
        axisEndNs: 9_000,
      }),
    ).toEqual({ entity: "service:bbb.service", atNs: 9_000 });
  });

  it("只带 at_ns 时保留该时刻，服务仍用默认值", () => {
    expect(
      resolveDiagnosticsSelection({
        services,
        requestedEntity: null,
        requestedAtNs: 7_000,
        axisEndNs: 9_000,
      }),
    ).toEqual({ entity: "service:ccc.service", atNs: 7_000 });
  });

  it("地址里的 entity 不在服务列表时返回 null", () => {
    expect(
      resolveDiagnosticsSelection({
        services,
        requestedEntity: "service:missing.service",
        requestedAtNs: null,
        axisEndNs: 9_000,
      }),
    ).toBeNull();
  });
});
