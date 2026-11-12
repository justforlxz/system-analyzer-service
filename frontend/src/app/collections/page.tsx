import Link from "next/link";

import { Panel } from "@/components/management/panel";
import { StatusBadge } from "@/components/management/status-badge";
import { describeError, fetchCollections, type CollectionResponse } from "@/lib/api/client";
import { importStatusMeta } from "@/lib/management/labels";
import { formatWallClockUtc } from "@/lib/timeline/format";

export const dynamic = "force-dynamic";

export const metadata = { title: "诊断记录 · system-analyzer-service" };

/** 诊断记录：已经导入的采集列表与导入状态。
 *
 * 数据来自 1.6 的 `GET /api/collections`，不新增接口。失败原因直接读后端的 `import_error`，
 * 界面不自己拼。
 */
export default async function CollectionsPage() {
  let collections: CollectionResponse[];
  try {
    collections = (await fetchCollections(100)).items;
  } catch (error) {
    return (
      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
        <Panel title="无法读取诊断记录" tone="error" testId="collections-error">
          <p>{describeError(error)}</p>
        </Panel>
      </main>
    );
  }

  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-slate-900">诊断记录</h1>
        <p className="text-sm text-slate-600">
          已导入的采集与它们的导入状态。导入成功后可以从这里跳到对应启动的时间线回放。
        </p>
      </header>

      {collections.length === 0 ? (
        <Panel title="还没有诊断记录" testId="collections-empty">
          <p>
            先到
            <Link className="mx-1 underline" href="/imports">
              导入
            </Link>
            上传一份 Journal 压缩包或 .bootrec 容器，导入完成后这里会出现对应的记录。
          </p>
        </Panel>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="collections-list">
          {collections.map((collection) => {
            const status = importStatusMeta(collection.import_status);
            return (
              <li
                key={collection.collection_id}
                className="rounded-lg border border-slate-200 bg-white p-4"
                data-testid="collection-row"
              >
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <Link
                    className="text-sm font-medium text-slate-900 underline"
                    href={`/timeline?machine_id=${encodeURIComponent(
                      collection.machine_id,
                    )}&boot_id=${encodeURIComponent(collection.boot_id)}`}
                  >
                    {collection.machine_id}
                  </Link>
                  <span className="font-mono text-xs text-slate-500">
                    启动 {collection.boot_id}
                  </span>
                  <StatusBadge
                    label={status.label}
                    tone={status.tone}
                    testId="collection-import-status"
                  />
                  <span className="font-mono text-xs text-slate-500">{collection.mode}</span>
                </div>
                <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                  <div className="flex gap-1">
                    <dt>采集</dt>
                    <dd className="font-mono">{collection.collection_id}</dd>
                  </div>
                  <div className="flex gap-1">
                    <dt>采集时间</dt>
                    <dd>
                      {collection.created_realtime_ns === null
                        ? "—"
                        : formatWallClockUtc(collection.created_realtime_ns)}
                    </dd>
                  </div>
                  <div className="flex gap-1">
                    <dt>资源数据</dt>
                    <dd>
                      {collection.resources_available === null
                        ? "未说明"
                        : collection.resources_available
                          ? "可用"
                          : `不可用：${collection.resources_reason ?? "未给原因"}`}
                    </dd>
                  </div>
                </dl>
                {collection.import_error ? (
                  <p
                    className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
                    data-testid="collection-import-error"
                  >
                    导入失败：{collection.import_error}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
