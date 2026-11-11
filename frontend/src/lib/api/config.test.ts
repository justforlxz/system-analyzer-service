import { afterEach, describe, expect, it, vi } from "vitest";

import { apiBaseUrl } from "@/lib/api/config";

describe("apiBaseUrl", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("未配置时回落到本地开发地址", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", undefined);

    expect(apiBaseUrl()).toBe("http://127.0.0.1:8000");
  });

  it("配置为空字符串时同样回落", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "   ");

    expect(apiBaseUrl()).toBe("http://127.0.0.1:8000");
  });

  it("读取配置的后端地址", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.example.test:8080");

    expect(apiBaseUrl()).toBe("http://api.example.test:8080");
  });
});
