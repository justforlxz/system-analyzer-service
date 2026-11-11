import { expect, test } from "@playwright/test";

test("首页可以打开并显示项目名", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { level: 1 })).toContainText("system-analyzer-service");
});
