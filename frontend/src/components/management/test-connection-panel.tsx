"use client";

import { useState } from "react";

import { StatusBadge } from "@/components/management/status-badge";
import { Button } from "@/components/ui/button";
import { describeError, testTargetConnection, type TestConnectionResponse } from "@/lib/api/client";
import { failureKindLabel, formatTimestamp } from "@/lib/management/labels";
import { formatBytes } from "@/lib/timeline/format";

type Props = {
  targetId: string;
  host: string;
};

/** 「测试连接」按钮与它的结果。
 *
 * 结果只活在这个组件里：本轮不把连接状态写进库（要持久化就得加列加迁移），所以刷新页面回到
 * 「未测试」。后端已经把失败分成四类，这里只把分类与说明原样显示。
 */
export function TestConnectionPanel({ targetId, host }: Props) {
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<TestConnectionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);

  async function handleTest() {
    setPending(true);
    setError(null);
    try {
      setResult(await testTargetConnection(targetId));
      setCheckedAt(new Date().toISOString());
    } catch (caught) {
      setResult(null);
      setError(describeError(caught));
    } finally {
      setPending(false);
    }
  }

  const system = result?.ok ? (result.system ?? null) : null;
  const failure = result && !result.ok ? (result.failure ?? null) : null;
  const memoryBytes = system?.memory_total_bytes ?? null;

  return (
    <section
      className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4"
      data-testid="test-connection-panel"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-slate-900">连接状态</h2>
        <div className="flex items-center gap-3">
          {checkedAt ? (
            <span className="text-xs text-slate-500">测试于 {formatTimestamp(checkedAt)}</span>
          ) : null}
          <Button type="button" size="sm" onClick={handleTest} disabled={pending}>
            {pending ? "测试中…" : "测试连接"}
          </Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        只读测试：读取 {host} 的系统版本、内核与内存信息，不改目标机器的任何配置。
      </p>

      {error ? (
        <p
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
          role="alert"
        >
          {error}
        </p>
      ) : null}

      {!result && !error ? (
        <p className="text-xs text-slate-500" data-testid="connection-untested">
          未测试。点「测试连接」后在这里看结果，刷新页面会回到未测试。
        </p>
      ) : null}

      {result?.ok && result.system ? (
        <dl
          className="grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2"
          data-testid="connection-success"
        >
          <Row label="主机名" value={result.system.hostname} />
          <Row label="系统版本" value={result.system.os_release} />
          <Row label="内核版本" value={result.system.kernel_version} />
          <Row label="架构" value={result.system.architecture} />
          <Row label="CPU 核数" value={result.system.cpu_count} />
          <Row label="内存总量" value={memoryBytes === null ? null : formatBytes(memoryBytes)} />
          <Row label="启动 ID" value={result.system.boot_id} mono />
        </dl>
      ) : null}

      {failure ? (
        <div
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
          data-testid="connection-failure"
        >
          <div className="flex items-center gap-2">
            <StatusBadge label={failureKindLabel(failure.kind)} tone="failure" />
            <span data-testid="connection-failure-kind" className="font-mono">
              {failure.kind}
            </span>
          </div>
          <p className="mt-1 break-words">{failure.message}</p>
        </div>
      ) : null}
    </section>
  );
}

function Row({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | number | null | undefined;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className={mono ? "font-mono text-slate-900" : "text-slate-900"}>
        {value === null || value === undefined ? "未读到" : value}
      </dd>
    </div>
  );
}
