import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";

/**
 * 任务 4.1 的端到端流程。所有请求都经 Next.js 的同源转发打到 `e2e/mock-api`，由它给出
 * 与真实 FastAPI 相同形状的响应（状态化：采集任务与导入任务每次被读取就往前走一步）。
 *
 * 覆盖的验收项：
 * - 新增机器 → 测试连接 → 上传 Journal → 看到导入记录；
 * - 采集任务创建、状态变化、取消；
 * - 四类可区分的连接失败原因；
 * - 失败原因可读、空状态不报错；
 * - 编辑与删除机器。
 */
const MOCK_API = "http://127.0.0.1:3110";
const JOURNAL_FIXTURE = fileURLToPath(
  new URL("../../backend/tests/fixtures/journal_archive/boot.journal.gz", import.meta.url),
);

test.describe.configure({ mode: "serial" });

async function resetMock(
  request: APIRequestContext,
  collections: "fixtures" | "empty" = "fixtures",
) {
  const response = await request.post(`${MOCK_API}/api/_reset?collections=${collections}`);
  expect(response.status()).toBe(200);
}

async function seedTarget(
  request: APIRequestContext,
  draft: { host: string; username?: string; port?: number },
): Promise<string> {
  const response = await request.post(`${MOCK_API}/api/targets`, {
    data: { username: "uos", port: 22, ...draft },
  });
  expect(response.status()).toBe(201);
  const body = (await response.json()) as { target_id: string };
  return body.target_id;
}

async function fillTargetForm(
  page: Page,
  values: { host: string; username: string; port: string },
) {
  await page.getByLabel("主机名或 IP 地址").fill(values.host);
  await page.getByLabel("SSH 用户名").fill(values.username);
  await page.getByLabel("SSH 端口").fill(values.port);
}

test.beforeEach(async ({ request }) => {
  await resetMock(request);
});

// 空状态用例会把假后端的诊断记录切成空列表；用例结束后立刻恢复，避免影响时间线用例。
test.afterEach(async ({ request }) => {
  await resetMock(request);
});

test("空状态：没有数据时显示下一步该做什么，不报错", async ({ page, request }) => {
  await resetMock(request, "empty");

  await page.goto("/machines");
  await expect(page.getByTestId("machines-empty")).toContainText("还没有机器");
  await expect(page.getByTestId("machines-error")).toHaveCount(0);

  await page.goto("/tasks");
  await expect(page.getByTestId("collection-tasks-empty")).toContainText("还没有采集任务");
  await expect(page.getByTestId("tasks-error")).toHaveCount(0);

  await page.goto("/collections");
  await expect(page.getByTestId("collections-empty")).toContainText("还没有诊断记录");
  await expect(page.getByTestId("collections-error")).toHaveCount(0);

  await page.goto("/imports");
  await expect(page.getByTestId("import-empty")).toBeVisible();
});

test("新增机器 → 测试连接 → 上传 Journal → 看到导入记录", async ({ page }) => {
  await page.goto("/machines");
  await fillTargetForm(page, { host: "10.20.50.152", username: "uos", port: "22" });
  await page.getByRole("button", { name: "新增机器" }).click();

  // 新增成功后进入详情页
  await expect(page).toHaveURL(/\/machines\/target-e2e-1$/);
  await expect(page.getByTestId("machine-config")).toContainText("10.20.50.152");

  // 连接测试（只读）成功，显示系统基本信息
  await expect(page.getByTestId("connection-untested")).toBeVisible();
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByTestId("connection-success")).toContainText("uos-PC");
  await expect(page.getByTestId("connection-success")).toContainText("6.6.0-amd64-desktop");

  // 上传 Journal 压缩包，看完整过程直到出结果
  await page.goto("/imports");
  await page.getByTestId("import-file").setInputFiles(JOURNAL_FIXTURE);
  await page.getByRole("button", { name: "上传并导入" }).click();
  await expect(page.getByTestId("import-job-status")).toBeVisible();
  await expect(page.getByTestId("import-success")).toContainText("collection:mock-import", {
    timeout: 20_000,
  });

  // 从导入结果跳到诊断记录，能看到刚导入的采集
  await page.getByRole("link", { name: "查看诊断记录" }).click();
  await expect(page).toHaveURL(/\/collections$/);
  await expect(page.getByTestId("collections-list")).toBeVisible();
});

test("采集任务：创建 → 看到状态变化 → 取消", async ({ page, request }) => {
  await seedTarget(request, { host: "10.20.50.152" });
  await page.goto("/tasks");

  // 创建任务，状态从排队/执行推进到已完成
  await page.getByRole("button", { name: "创建采集任务" }).click();
  const firstTask = page.getByTestId("collection-task").first();
  await expect(firstTask.getByTestId("task-status")).toHaveText(/排队中|执行中/);
  await expect(firstTask.getByTestId("task-status")).toHaveText("已完成", { timeout: 20_000 });
  await expect(firstTask.getByTestId("task-attempt")).toContainText("成功");

  // 再创建一个，然后取消：显示「已请求取消」，最终以采集记录为准
  await page.getByRole("button", { name: "创建采集任务" }).click();
  const newest = page.getByTestId("collection-task").first();
  await newest.getByRole("button", { name: "取消任务" }).click();
  await expect(newest.getByTestId("task-cancel-requested")).toBeVisible();
  await expect(newest.getByTestId("task-notice")).toContainText("已按用户要求取消", {
    timeout: 20_000,
  });
  await expect(newest.getByTestId("task-failure")).toHaveCount(0);
});

test("采集失败时显示后端给出的可读原因", async ({ page, request }) => {
  await seedTarget(request, { host: "collect-fail.example" });
  await page.goto("/tasks");

  await page.getByRole("button", { name: "创建采集任务" }).click();
  const task = page.getByTestId("collection-task").first();
  await expect(task.getByTestId("task-status")).toHaveText("失败", { timeout: 20_000 });
  await expect(task.getByTestId("task-failure")).toContainText(
    "采集中：步骤 collect.cpu 失败：远端命令以退出码 1 结束",
  );
  // 操作记录默认收在 <details> 里，展开后能看到失败那一步
  await task.locator("summary").click();
  await expect(task.getByTestId("task-events")).toBeVisible();
  await expect(task.getByTestId("task-events")).toContainText("phase_failed");
});

test("四类连接失败在界面上可区分", async ({ page, request }) => {
  const cases = [
    { host: "connect-fail.example", kind: "connect", readable: "不可达" },
    { host: "auth-fail.example", kind: "auth", readable: "拒绝了用户名或口令" },
    { host: "timeout.example", kind: "timeout", readable: "1 秒内没有完成" },
    // 口令没配置与「连不上」无关：说明里提到的是缺哪个环境变量，不是目标机器
    { host: "no-credential.example", kind: "credential", readable: "SAS_SSH_PASSWORD" },
  ];
  for (const item of cases) {
    const targetId = await seedTarget(request, { host: item.host });
    await page.goto(`/machines/${targetId}`);
    await page.getByRole("button", { name: "测试连接" }).click();
    const failure = page.getByTestId("connection-failure");
    await expect(failure).toBeVisible();
    await expect(page.getByTestId("connection-failure-kind")).toHaveText(item.kind);
    // 分类之外还有一句可读说明，不是只有「失败」两个字
    await expect(failure).toContainText(item.readable);
  }
});

test("编辑机器后配置整体替换，删除后回到空列表", async ({ page, request }) => {
  const targetId = await seedTarget(request, { host: "10.20.50.152" });
  await page.goto(`/machines/${targetId}`);

  await page.getByLabel("主机名或 IP 地址").fill("10.20.50.160");
  await page.getByLabel("SSH 端口").fill("2222");
  await page.getByRole("button", { name: "保存修改" }).click();
  await expect(page.getByTestId("machine-config")).toContainText("10.20.50.160");
  await expect(page.getByTestId("machine-config")).toContainText("2222");

  await page.getByRole("button", { name: "删除这台机器" }).click();
  await expect(page.getByTestId("delete-confirm")).toContainText("已经导入的诊断记录都保留");
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect(page).toHaveURL(/\/machines$/);
  await expect(page.getByTestId("machines-empty")).toBeVisible();
});

test("导入失败时显示分类与可读原因", async ({ page }) => {
  await page.goto("/imports");
  await page.getByTestId("import-file").setInputFiles({
    name: "bad.journal.gz",
    mimeType: "application/gzip",
    buffer: Buffer.from("not a real journal archive"),
  });
  await page.getByRole("button", { name: "上传并导入" }).click();

  const failure = page.getByTestId("import-failure");
  await expect(failure).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("import-failure-kind")).toHaveText("输入不合格");
  await expect(failure).toContainText("归档里没有 _HOSTNAME");
});
