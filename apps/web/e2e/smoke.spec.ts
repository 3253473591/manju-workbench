import { test, expect } from "@playwright/test";

/**
 * 一条端到端冒烟：登录 → 流水线 → 分镜表 → 付费点弹窗。
 * 断言的是"界面能走到"，不断言具体文案数字（数字会随演示数据变化）。
 */
test("登录 → 分镜表 → 付费点 → 发布门", async ({ page }) => {
  await page.goto("/");

  await page.locator("input").nth(0).fill("seed@example.com");
  await page.locator('input[type="password"]').fill("seedpass12345");
  await page.getByRole("button", { name: "登录", exact: true }).click();

  await expect(page.getByRole("button", { name: "流水线", exact: true })).toBeVisible({ timeout: 15_000 });

  await page.getByRole("button", { name: "分镜表", exact: true }).click();
  await expect(page.getByRole("cell", { name: "烟测片段" })).toBeVisible();

  const row = page.getByRole("row", { name: /烟测片段/ });
  await row.getByRole("button", { name: "生成…" }).click();
  await expect(page.getByText(/付费点 · 片段/)).toBeVisible();

  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(page.getByText(/付费点 · 片段/)).toBeHidden();

  // 发布门：烟测集只有一条 prompted 片段，应当被阻塞并显示原因
  await page.getByRole("button", { name: "发布检查", exact: true }).click();
  await expect(page.getByText(/阻塞：/)).toBeVisible();
  await expect(page.getByText("片段产物完整")).toBeVisible();
  await expect(page.getByText("项目当天预算未超")).toBeVisible();
});
