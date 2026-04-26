import { expect, type Page, test } from "@playwright/test";

const adminUsername = process.env.E2E_ADMIN_USERNAME ?? "admin";
const adminPassword = process.env.E2E_ADMIN_PASSWORD ?? "admin";

async function login(page: Page) {
  await page.goto("/#outbound");
  const loginForm = page.locator("#login-form");
  if (await loginForm.isVisible()) {
    await page.locator('#login-form input[name="username"]').fill(adminUsername);
    await page.locator('#login-form input[name="password"]').fill(adminPassword);
    await page.locator("#login-form button").click();
  }
  await expect(page.locator('[data-page="outbound"]')).toBeVisible();
}

test("outbound toolbar stays on one row and row opens detail modal", async ({ page }) => {
  await login(page);

  const enqueueResult = await page.evaluate(async () => {
    const response = await fetch("/api/reports/weekly/enqueue", { method: "POST" });
    if (!response.ok) return null;
    return response.json();
  });
  expect(enqueueResult).toBeTruthy();

  await page.goto("/#outbound");
  await expect(page.locator("#outbound-filter-form")).toBeVisible();

  const toolbarRows = await page.locator("#outbound-filter-form").evaluate((form) => {
    const tops = Array.from(form.children).map((child) => Math.round(child.getBoundingClientRect().top));
    return new Set(tops).size;
  });
  expect(toolbarRows).toBe(1);

  const row = page.locator("#outbound-list [data-outbound-id]").first();
  await expect(row).toBeVisible();
  await row.click();

  await expect(page.locator("#mail-detail-modal")).toBeVisible();
  await expect(page.locator("#mail-detail-meta")).toContainText("外发队列");
  await expect(page.locator("#mail-detail-body")).not.toHaveText("无正文内容");
});
