import Link from "next/link";

import { apiBaseUrl } from "@/lib/api/config";
import { Button } from "@/components/ui/button";

/**
 * 首页是 Server Component：骨架阶段只证明前端能构建、能读到后端地址配置。
 * 业务页面在任务 4.x 里加入，交互复杂的部分再改写成 Client Components。
 */
export default function HomePage() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-6 py-16">
      <h1 className="text-2xl font-semibold">system-analyzer-service</h1>
      <p className="text-slate-600">工程骨架已就绪，业务功能由后续任务逐步加入。</p>
      <dl className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
        <dt className="font-medium text-slate-700">后端 API 地址</dt>
        <dd className="mt-1 font-mono text-slate-500">{apiBaseUrl()}</dd>
      </dl>
      <div className="flex items-center gap-3">
        <Button type="button">占位按钮</Button>
        <Link className="text-sm text-slate-900 underline" href="/timeline">
          打开启动过程时间线回放
        </Link>
      </div>
    </main>
  );
}
