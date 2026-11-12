import type {
  CursorResponse,
  MachineResponse,
  ServiceDiagnosticsResponse,
  ServiceSummary,
} from "@/lib/api/client";

export const DIAGNOSTICS_MACHINE_ID = "machine:test-host";
export const DIAGNOSTICS_BOOT_ID = "boot-0001";
export const DIAGNOSTICS_AXIS_START = 1_000_000_000;
export const DIAGNOSTICS_AXIS_END = 9_000_000_000;
export const DIAGNOSTICS_AT_NS = 3_000_000_000;

export const diagnosticsMachine: MachineResponse = {
  machine_id: DIAGNOSTICS_MACHINE_ID,
  hostname: "test-host",
  architecture: "x86_64",
  kernel_version: "6.6.0-1-amd64",
  os_release: "UOS Desktop 25 Professional",
};

export const diagnosticsCursor: CursorResponse = {
  machine_id: DIAGNOSTICS_MACHINE_ID,
  boot_id: DIAGNOSTICS_BOOT_ID,
  start_ns: DIAGNOSTICS_AXIS_START,
  end_ns: DIAGNOSTICS_AXIS_END,
  position_ns: DIAGNOSTICS_AT_NS,
  previous_ns: 2_000_000_000,
  next_ns: 3_200_000_000,
};

export const diagnosticsServices: ServiceSummary[] = [
  {
    entity: "service:nginx.service",
    unit: "nginx.service",
    lifecycle_start_ns: 1_000_000_000,
    lifecycle_end_ns: 6_000_000_000,
    annotation_count: 1,
  },
  {
    entity: "service:firefox.service",
    unit: "firefox.service",
    lifecycle_start_ns: 2_000_000_000,
    lifecycle_end_ns: 6_000_000_000,
    annotation_count: 1,
  },
];

export function makeDiagnostics(
  overrides: Partial<ServiceDiagnosticsResponse> = {},
): ServiceDiagnosticsResponse {
  return {
    machine_id: DIAGNOSTICS_MACHINE_ID,
    boot_id: DIAGNOSTICS_BOOT_ID,
    entity: "service:nginx.service",
    unit: "nginx.service",
    at_ns: DIAGNOSTICS_AT_NS,
    state: {
      state: "active",
      since_ns: 2_000_000_000,
      record_id: "span-active",
      stale: false,
      note: null,
    },
    identity: {
      pid: {
        value: "812",
        record_id: "journal-started",
        monotonic_ns: 2_000_000_000,
        note: null,
      },
      invocation_id: {
        value: "inv-1",
        record_id: "journal-started",
        monotonic_ns: 2_000_000_000,
        note: null,
      },
      cgroup: {
        value: "/system.slice/nginx.service",
        record_id: "journal-started",
        monotonic_ns: 2_000_000_000,
        note: null,
      },
    },
    lifecycle: [
      {
        record_id: "span-activating",
        collection_id: "collection:one",
        monotonic_ns: 1_000_000_000,
        end_monotonic_ns: 2_000_000_000,
        end_is_declared: true,
        span_type: "service.activating",
        state: "active",
        invocation_id: "inv-1",
        pid: "812",
        cgroup: "/system.slice/nginx.service",
        attributes: {},
      },
      {
        record_id: "span-active",
        collection_id: "collection:one",
        monotonic_ns: 2_000_000_000,
        end_monotonic_ns: 6_000_000_000,
        end_is_declared: true,
        span_type: "service.active",
        state: "inactive",
        invocation_id: "inv-1",
        pid: "812",
        cgroup: "/system.slice/nginx.service",
        attributes: {},
      },
    ],
    journal: {
      window_start_ns: 2_400_000_000,
      window_end_ns: 3_600_000_000,
      total: 2,
      items: [
        {
          record_id: "journal-started",
          collection_id: "collection:one",
          monotonic_ns: 2_000_000_000,
          realtime_ns: 1_794_009_602_000_000_000,
          message: "Started nginx.service",
          identifier: "systemd",
          unit: "nginx.service",
          priority: "6",
          from_evidence: true,
          attributes: {},
        },
        {
          record_id: "journal-other",
          collection_id: "collection:one",
          monotonic_ns: 3_200_000_000,
          realtime_ns: 1_794_009_603_200_000_000,
          message: "worker process started",
          identifier: "nginx",
          unit: "nginx.service",
          priority: "6",
          from_evidence: false,
          attributes: {},
        },
      ],
    },
    resources: {
      at_ns: DIAGNOSTICS_AT_NS,
      latest: [
        {
          entity: "metric:cpu.unit.utilization:nginx.service",
          name: "cpu.unit.utilization",
          unit: "percent",
          value: 12.5,
          record_id: "metric-cpu",
          monotonic_ns: 3_000_000_000,
        },
        {
          entity: "metric:memory.unit.used:nginx.service",
          name: "memory.unit.used",
          unit: "bytes",
          value: 4_194_304,
          record_id: "metric-memory",
          monotonic_ns: 2_900_000_000,
        },
      ],
      series: [
        {
          entity: "metric:cpu.unit.utilization:nginx.service",
          name: "cpu.unit.utilization",
          unit: "percent",
          points: [
            { monotonic_ns: 2_500_000_000, value: 8 },
            { monotonic_ns: 3_000_000_000, value: 12.5 },
          ],
        },
      ],
      unavailable: [],
      note: null,
    },
    annotations: [
      {
        record_id: "annotation-failed",
        annotation_type: "systemd.service_failed",
        analyzer: "systemd.lifecycle",
        severity: "error",
        message: "nginx.service 进入 failed 状态：Failed with result 'oom-kill'.",
        confidence: 0.8,
        monotonic_ns: 2_500_000_000,
        evidence: [
          {
            record_id: "journal-started",
            found: true,
            kind: "event",
            monotonic_ns: 2_000_000_000,
            message: "Started nginx.service",
          },
        ],
        evidence_missing: false,
      },
    ],
    uncertain: [],
    ...overrides,
  };
}

export function makeUnknownDiagnostics(): ServiceDiagnosticsResponse {
  return makeDiagnostics({
    state: {
      state: "unknown",
      since_ns: null,
      record_id: null,
      stale: false,
      note: "游标之前没有该服务的生命周期记录，无法确定当时的状态",
    },
    identity: {
      pid: {
        value: null,
        record_id: null,
        monotonic_ns: null,
        note: "生命周期区间与游标之前的关联 Journal 都没有 pid，无法确定",
      },
      invocation_id: {
        value: null,
        record_id: null,
        monotonic_ns: null,
        note: "生命周期区间与游标之前的关联 Journal 都没有 invocation_id，无法确定",
      },
      cgroup: {
        value: null,
        record_id: null,
        monotonic_ns: null,
        note: "生命周期区间与游标之前的关联 Journal 都没有 cgroup，无法确定",
      },
    },
    lifecycle: [],
    journal: { window_start_ns: 0, window_end_ns: 1, total: 0, items: [] },
    resources: {
      at_ns: DIAGNOSTICS_AT_NS,
      latest: [],
      series: [],
      unavailable: [],
      note: "这次启动没有该服务的资源采样，无法确定资源使用",
    },
    annotations: [],
    uncertain: [
      { field: "state", note: "游标之前没有该服务的生命周期记录，无法确定当时的状态" },
      { field: "pid", note: "生命周期区间与游标之前的关联 Journal 都没有 pid，无法确定" },
      { field: "resources", note: "这次启动没有该服务的资源采样，无法确定资源使用" },
    ],
  });
}
