import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // 前端只消费 FastAPI 的 OpenAPI 定义，不实现业务逻辑；这里只保留构建所需配置。
  poweredByHeader: false,
};

export default nextConfig;
