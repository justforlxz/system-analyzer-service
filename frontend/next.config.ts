import type { NextConfig } from "next";

/** 后端地址：浏览器请求经同源转发到这里。
 *
 * 后端没有开放跨域，浏览器端一律请求本域的 `/api/...`，由这里的 rewrite 转发给 FastAPI。
 * 这不改变「FastAPI 是唯一的业务 API 层」：Next.js 只转发字节，不实现任何业务逻辑。
 */
const apiOrigin = (process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || "http://127.0.0.1:8000").replace(
  /\/+$/,
  "",
);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // 前端只消费 FastAPI 的 OpenAPI 定义，不实现业务逻辑；这里只保留构建所需配置。
  poweredByHeader: false,
  // Next.js 16 默认只允许 `localhost` 访问开发期资源（含 HMR 客户端）。少了它，用
  // `127.0.0.1:3000` 打开页面时客户端脚本被拦下，界面完全没有交互（只有服务端渲染的静态
  // HTML）。本地开发和端到端测试都用 127.0.0.1，因此显式允许这两个来源。
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
