import Link from "next/link";

import { ImportUploader } from "@/components/management/import-uploader";

export const metadata = { title: "导入 · system-analyzer-service" };

/** 导入页：上传文件并看完整过程（上传 → 排队 → 执行 → 结果/失败原因）。
 *
 * 页面本身是 Server Component，只为给出标题与说明；上传与轮询都在 Client Component 里做，
 * 因为要用到文件选择与定时刷新。
 */
export default function ImportsPage() {
  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-slate-900">导入</h1>
        <p className="text-sm text-slate-600">
          上传一份 Journal 压缩包或 .bootrec 容器。上传只把文件写到服务端并入队，解析由 Worker
          执行；失败时会显示后端给出的可读原因。
        </p>
        <p className="text-xs text-slate-500">
          已经导入完成的记录在
          <Link className="mx-1 underline" href="/collections">
            诊断记录
          </Link>
          里。
        </p>
      </header>
      <ImportUploader />
    </main>
  );
}
