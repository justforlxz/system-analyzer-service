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
