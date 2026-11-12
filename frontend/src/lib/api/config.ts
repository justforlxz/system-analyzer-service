/** 后端 API 的基地址。
 *
 * FastAPI 是唯一的业务 API 层，前端所有请求都从这里出发；浏览器端与
 * Server Component 都用同一个变量。未配置时回落到本地开发端口，方便新 clone 直接跑。
 */
const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

export function apiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return configured && configured.length > 0 ? configured : DEFAULT_API_BASE_URL;
}

/** 请求时实际拼在路径前面的前缀。
 *
 * - Server Component 里没有页面来源，必须用绝对地址，直接连 FastAPI。
 * - 浏览器里一律用同源路径（空前缀），由 `next.config.ts` 的 `rewrites` 转发到同一个
 *   FastAPI。后端没有开放跨域，同源转发让界面不必依赖后端的 CORS 配置，也不改变
 *   「FastAPI 是唯一业务 API 层」的划分：Next.js 只做转发，不实现业务。
 */
export function apiRequestBaseUrl(): string {
  return typeof window === "undefined" ? apiBaseUrl() : "";
}
