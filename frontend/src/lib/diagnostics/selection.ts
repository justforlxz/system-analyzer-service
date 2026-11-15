import type { ServiceSummary } from "@/lib/api/client";

export type DiagnosticsSelection = {
  entity: string;
  atNs: number | null;
};

type SelectionInput = {
  services: ServiceSummary[];
  requestedEntity: string | null;
  requestedAtNs: number | null;
  axisEndNs: number | null;
};

function hasLifecycle(service: ServiceSummary): boolean {
  return service.lifecycle_start_ns !== null;
}

function compareUnit(left: ServiceSummary, right: ServiceSummary): number {
  if (left.unit < right.unit) return -1;
  if (left.unit > right.unit) return 1;
  return 0;
}

/**
 * 默认服务：先限定在有生命周期区间的服务里，取分析结论最多者，结论数相同按 unit 字母序；
 * 没有任何服务有生命周期区间时按 unit 字母序取第一个。结果不依赖服务列表的请求顺序。
 */
export function defaultService(services: ServiceSummary[]): ServiceSummary | null {
  let chosen: ServiceSummary | null = null;
  for (const service of services) {
    if (!hasLifecycle(service)) continue;
    if (
      chosen === null ||
      service.annotation_count > chosen.annotation_count ||
      (service.annotation_count === chosen.annotation_count && compareUnit(service, chosen) < 0)
    ) {
      chosen = service;
    }
  }
  if (chosen !== null) return chosen;
  for (const service of services) {
    if (chosen === null || compareUnit(service, chosen) < 0) chosen = service;
  }
  return chosen;
}

/**
 * 解析诊断页首次打开要用的服务与游标。地址里给了 `entity` 或 `at_ns` 时按给的值，不覆盖。
 * 默认时刻是这次启动的轴终点，和服务自身的生命周期结束时刻无关。服务列表为空时返回 `null`。
 */
export function resolveDiagnosticsSelection({
  services,
  requestedEntity,
  requestedAtNs,
  axisEndNs,
}: SelectionInput): DiagnosticsSelection | null {
  if (requestedEntity !== null && !services.some((item) => item.entity === requestedEntity)) {
    return null;
  }
  const selected = requestedEntity ?? defaultService(services)?.entity ?? null;
  if (selected === null) return null;
  return {
    entity: selected,
    atNs: requestedAtNs ?? axisEndNs,
  };
}
