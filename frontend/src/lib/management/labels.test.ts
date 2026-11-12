import { describe, expect, it } from "vitest";

import type { CollectionTaskResponse } from "@/lib/api/client";
import {
  attemptStatusLabel,
  collectionTaskFailure,
  collectionTaskNotice,
  failureKindLabel,
  importFailureKindLabel,
  formatTimestamp,
  importStatusLabel,
  isActiveJobStatus,
  jobStatusLabel,
  jobStatusMeta,
  phaseLabel,
  phaseStatusLabel,
} from "@/lib/management/labels";

function task(overrides: Partial<CollectionTaskResponse> = {}): CollectionTaskResponse {
  return {
    task_id: "task-1",
    kind: "collect.target",
    status: "queued",
    attempts: 0,
    max_attempts: 3,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    target_id: "target:1",
    run_id: "run-1",
    cancel_requested: false,
    latest_attempt: null,
    failure: null,
    ...overrides,
  };
}

function attempt(status: string) {
  return {
    attempt: 1,
    status,
    started_at: "2026-01-01T00:00:00+00:00",
    finished_at: "2026-01-01T00:00:10+00:00",
    phases: [],
    events: [],
  };
}

describe("任务与阶段文案", () => {
  it("已知状态给中文文案与色调", () => {
    expect(jobStatusMeta("queued")).toEqual({ label: "排队中", tone: "neutral" });
    expect(jobStatusMeta("running")).toEqual({ label: "执行中", tone: "running" });
    expect(jobStatusMeta("succeeded")).toEqual({ label: "已完成", tone: "success" });
    expect(jobStatusMeta("failed")).toEqual({ label: "失败", tone: "failure" });
    expect(jobStatusLabel("failed")).toBe("失败");
  });

  it("未知状态原样返回，不显示空白", () => {
    expect(jobStatusLabel("paused")).toBe("paused");
    expect(jobStatusMeta("paused").tone).toBe("neutral");
  });

  it("只有排队中与执行中的任务需要轮询", () => {
    expect(isActiveJobStatus("queued")).toBe(true);
    expect(isActiveJobStatus("running")).toBe(true);
    expect(isActiveJobStatus("succeeded")).toBe(false);
    expect(isActiveJobStatus("failed")).toBe(false);
  });

  it("尝试与阶段状态各有自己的文案", () => {
    expect(attemptStatusLabel("cancelled")).toBe("已取消");
    expect(attemptStatusLabel("timed_out")).toBe("超时");
    expect(attemptStatusLabel("interrupted")).toBe("中断");
    expect(phaseStatusLabel("reused")).toBe("复用上次结果");
    expect(phaseStatusLabel("running")).toBe("执行中");
  });

  it("阶段按采集三步命名", () => {
    expect(phaseLabel("pre")).toBe("采集前准备");
    expect(phaseLabel("in_progress")).toBe("采集中");
    expect(phaseLabel("post")).toBe("采集后恢复");
    expect(phaseLabel("unknown")).toBe("unknown");
  });

  it("导入状态来自后端四个取值", () => {
    expect(importStatusLabel("pending")).toBe("待导入");
    expect(importStatusLabel("importing")).toBe("导入中");
    expect(importStatusLabel("imported")).toBe("已导入");
    expect(importStatusLabel("failed")).toBe("导入失败");
  });

  it("四类连接失败各有中文分类", () => {
    expect(failureKindLabel("connect")).toBe("连接失败");
    expect(failureKindLabel("auth")).toBe("认证失败");
    expect(failureKindLabel("timeout")).toBe("超时");
    expect(failureKindLabel("credential")).toBe("服务端未配置口令");
  });

  it("导入失败的两类原因也有中文分类", () => {
    expect(importFailureKindLabel("invalid_input")).toBe("输入不合格");
    expect(importFailureKindLabel("internal")).toBe("服务端内部错误");
    expect(importFailureKindLabel("其他")).toBe("其他");
  });
});

describe("采集任务的结果说明", () => {
  it("失败时把阶段名与后端原因拼成一句话", () => {
    const failure = collectionTaskFailure(
      task({
        status: "failed",
        latest_attempt: attempt("failed"),
        failure: { phase: "in_progress", message: "步骤 collect.cpu 失败：退出码 1" },
      }),
    );

    expect(failure).toBe("采集中：步骤 collect.cpu 失败：退出码 1");
  });

  it("与阶段无关的失败不硬拼阶段名", () => {
    const failure = collectionTaskFailure(
      task({
        status: "failed",
        failure: {
          phase: null,
          message: "任务执行失败，采集记录里没有写下失败原因；详细原因见 Worker 日志",
        },
      }),
    );

    expect(failure).toBe("任务执行失败，采集记录里没有写下失败原因；详细原因见 Worker 日志");
  });

  it("取消不是失败：不给失败原因，另给一条提示", () => {
    const cancelled = task({ status: "succeeded", latest_attempt: attempt("cancelled") });

    expect(collectionTaskFailure(cancelled)).toBeNull();
    expect(collectionTaskNotice(cancelled)).toBe("已按用户要求取消，采集后恢复阶段已执行");
  });

  it("中断会说明等待重新执行，且不算失败", () => {
    const interrupted = task({ status: "queued", latest_attempt: attempt("interrupted") });

    expect(collectionTaskFailure(interrupted)).toBeNull();
    expect(collectionTaskNotice(interrupted)).toContain("中断");
  });

  it("有失败原因时不再给提示", () => {
    const failed = task({
      status: "failed",
      latest_attempt: attempt("failed"),
      failure: { phase: "post", message: "恢复远端文件失败" },
    });

    expect(collectionTaskFailure(failed)).toBe("采集后恢复：恢复远端文件失败");
    expect(collectionTaskNotice(failed)).toBeNull();
  });

  it("执行中与成功都不给原因段落", () => {
    expect(collectionTaskFailure(task())).toBeNull();
    expect(
      collectionTaskFailure(task({ status: "succeeded", latest_attempt: attempt("succeeded") })),
    ).toBeNull();
  });
});

describe("时间戳", () => {
  it("按 UTC 写到秒", () => {
    expect(formatTimestamp("2026-01-02T03:04:05.678+00:00")).toBe("2026-01-02 03:04:05 UTC");
    expect(formatTimestamp("2026-01-02T03:04:05Z")).toBe("2026-01-02 03:04:05 UTC");
  });

  it("时区偏移换算成 UTC", () => {
    expect(formatTimestamp("2026-01-02T11:04:05+08:00")).toBe("2026-01-02 03:04:05 UTC");
  });

  it("空值与非法值不猜", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp(undefined)).toBe("—");
    expect(formatTimestamp("不是时间")).toBe("不是时间");
  });
});
