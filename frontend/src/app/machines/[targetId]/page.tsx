import Link from "next/link";

import { CollectionTasksPanel } from "@/components/management/collection-tasks-panel";
import { DeleteTargetButton } from "@/components/management/delete-target-button";
import { Panel } from "@/components/management/panel";
import { TargetForm } from "@/components/management/target-form";
import { TestConnectionPanel } from "@/components/management/test-connection-panel";
import {
  describeError,
  fetchCollectionTasks,
  fetchTarget,
  type CollectionTaskResponse,
  type PageOf,
  type SshTarget,
} from "@/lib/api/client";
import { formatTimestamp } from "@/lib/management/labels";

export const dynamic = "force-dynamic";

export const metadata = { title: "机器详情 · system-analyzer-service" };

type PageProps = { params: Promise<{ targetId: string }> };

/** 机器详情：配置、连接测试、编辑与删除、这台机器的采集任务。
 *
 * 这里不展示导入出来的诊断数据：`ssh_targets`（运维配置）与 `machines`（采集结果）之间
 * 没有关联字段，要挂到一起得先加列加迁移，超出本轮范围。
 */
export default async function MachineDetailPage({ params }: PageProps) {
  const { targetId } = await params;

  let target: SshTarget;
  try {
    target = await fetchTarget(targetId);
  } catch (error) {
    return (
      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
        <BackLink />
        <Panel title="无法读取这台机器" tone="error" testId="machine-detail-error">
          <p>{describeError(error)}</p>
        </Panel>
      </main>
    );
  }

  let tasks: PageOf<CollectionTaskResponse> = { items: [], total: 0, limit: 10, offset: 0 };
  let tasksError: string | null = null;
  try {
    tasks = await fetchCollectionTasks({ targetId: target.target_id, limit: 10 });
  } catch (error) {
    tasksError = describeError(error);
  }

  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
      <BackLink />
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-slate-900">{target.host}</h1>
        <p className="text-sm text-slate-600">
          {target.username}@{String(target.port)} ·{" "}
          <span className="font-mono">{target.target_id}</span>
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-medium text-slate-900">配置</h2>
        <dl
          className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2"
          data-testid="machine-config"
        >
          <Field label="主机" value={target.host} />
          <Field label="SSH 用户名" value={target.username} />
          <Field label="SSH 端口" value={String(target.port)} />
          <Field label="口令变量名" value={target.password_env_var} mono />
          <Field label="创建" value={formatTimestamp(target.created_at)} />
          <Field label="更新" value={formatTimestamp(target.updated_at)} />
        </dl>
      </section>

      <TestConnectionPanel targetId={target.target_id} host={target.host} />

      <TargetForm mode="edit" target={target} />

      <DeleteTargetButton targetId={target.target_id} host={target.host} />

      {tasksError ? (
        <Panel title="无法读取这台机器的采集任务" tone="error">
          <p>{tasksError}</p>
        </Panel>
      ) : (
        <CollectionTasksPanel
          targets={[target]}
          initial={tasks}
          targetId={target.target_id}
          showCreate={false}
        />
      )}
    </main>
  );
}

function BackLink() {
  return (
    <p className="text-xs">
      <Link className="text-slate-600 underline" href="/machines">
        ← 返回机器列表
      </Link>
    </p>
  );
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className={mono ? "font-mono text-slate-900" : "text-slate-900"}>{value}</dd>
    </div>
  );
}
