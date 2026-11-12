import type { ReactNode } from "react";

type Tone = "error" | "info";

/** 页面级的提示/空状态面板。
 *
 * 空状态和读取失败都用它：两者都是「这一块现在没有内容」，只是色调不同。空状态必须说明
 * 下一步该做什么，不能只写「没有数据」。
 */
export function Panel({
  title,
  tone = "info",
  testId,
  children,
}: {
  title: string;
  tone?: Tone;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <section
      data-testid={testId}
      className={
        tone === "error"
          ? "rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          : "rounded-lg border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600"
      }
    >
      <h2 className={tone === "error" ? "font-medium" : "font-medium text-slate-900"}>{title}</h2>
      <div className="mt-1 space-y-1">{children}</div>
    </section>
  );
}
