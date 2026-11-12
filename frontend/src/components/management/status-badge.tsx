import type { Tone } from "@/lib/management/labels";
import { cn } from "@/lib/utils";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "border-slate-300 bg-slate-100 text-slate-700",
  running: "border-sky-300 bg-sky-50 text-sky-800",
  success: "border-emerald-300 bg-emerald-50 text-emerald-800",
  failure: "border-red-300 bg-red-50 text-red-800",
};

/** 状态标签。文案与色调由 `lib/management/labels.ts` 决定，这里只负责画出来。 */
export function StatusBadge({
  label,
  tone,
  testId,
}: {
  label: string;
  tone: Tone;
  testId?: string;
}) {
  return (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs leading-5",
        TONE_CLASS[tone],
      )}
    >
      {label}
    </span>
  );
}
