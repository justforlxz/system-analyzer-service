import Link from "next/link";

import { Panel } from "@/components/management/panel";
import { StatusBadge } from "@/components/management/status-badge";
import { TargetForm } from "@/components/management/target-form";
import { describeError, fetchTargets, type SshTarget } from "@/lib/api/client";
import { formatTimestamp } from "@/lib/management/labels";

export const dynamic = "force-dynamic";

export const metadata = { title: "机器 · system-analyzer-service" };

/** 机器列表：新增表单 + 已保存的机器。
 *
 * 列表是 Server Component（首屏数据在服务端取），新增表单是 Client Component。
 * 「连接状态」本轮不落库，列表统一显示「未测试」；要测试就到详情页点「测试连接」。
 */
export default async function MachinesPage() {
  let targets: SshTarget[];
  try {
    targets = await fetchTargets();
  } catch (error) {
    return (
      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
        <Panel title="无法读取机器列表" tone="error" testId="machines-error">
          <p>{describeError(error)}</p>
        </Panel>
      </main>
    );
  }

  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-slate-900">机器</h1>
        <p className="text-sm text-slate-600">
          管理 SSH 目标机器。口令不在界面上填写：库里只保存变量名，口令由服务端进程的运行环境提供。
        </p>
      </header>

      <TargetForm mode="create" />

      {targets.length === 0 ? (
        <Panel title="还没有机器" testId="machines-empty">
          <p>
            填写上面的表单新增第一台机器。保存后进入详情页可以做只读连接测试，再为它创建采集任务。
          </p>
        </Panel>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="machines-list">
          {targets.map((target) => (
            <li
              key={target.target_id}
              className="rounded-lg border border-slate-200 bg-white p-4"
              data-testid="machine-row"
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Link
                  className="text-sm font-medium text-slate-900 underline"
                  href={`/machines/${encodeURIComponent(target.target_id)}`}
                >
                  {target.host}
                </Link>
                <span className="text-xs text-slate-600">
                  {target.username}@{String(target.port)}
                </span>
                <StatusBadge label="未测试" tone="neutral" testId="machine-connection-status" />
                <span className="font-mono text-xs text-slate-500">{target.target_id}</span>
              </div>
              <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                <div className="flex gap-1">
                  <dt>口令变量名</dt>
                  <dd className="font-mono">{target.password_env_var}</dd>
                </div>
                <div className="flex gap-1">
                  <dt>创建</dt>
                  <dd>{formatTimestamp(target.created_at)}</dd>
                </div>
                <div className="flex gap-1">
                  <dt>更新</dt>
                  <dd>{formatTimestamp(target.updated_at)}</dd>
                </div>
              </dl>
              <p className="mt-2 text-xs text-slate-500">
                连接状态不落库：到
                <Link
                  className="mx-1 underline"
                  href={`/machines/${encodeURIComponent(target.target_id)}`}
                >
                  详情页
                </Link>
                点「测试连接」，刷新后回到未测试。
              </p>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
