import { chromium } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const CDP_URL = process.env.FXIAOKE_CDP_URL || "http://127.0.0.1:9333";
const LOGIN_URL =
  "https://www.fxiaoke.com/proj/page/loginv2?returnUrl=https%3A%2F%2Fwww.fxiaoke.com%2FXV%2FUI%2FHome%23crm%2Flist%2F%3D%2FSalesOrderObj";

function readStdin() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      input += chunk;
    });
    process.stdin.on("end", () => resolve(input));
    process.stdin.on("error", reject);
  });
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text || "{}");
  } catch {
    return {};
  }
}

async function firstPage(browser) {
  const context = browser.contexts()[0] || (await browser.newContext());
  return context.pages()[0] || (await context.newPage());
}

async function loginIfNeeded(page, username, password) {
  if (!username || !password) return { loggedIn: false, skipped: "missing credentials" };
  await page.goto(LOGIN_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(1500);
  const title = await page.title().catch(() => "");
  if (!/loginv2|登录/i.test(page.url() + title)) return { loggedIn: true, skipped: "already logged in" };
  const phone = page.locator('input[name="phoneNumber"]');
  const pass = page.locator('input[name="password"]');
  if ((await phone.count()) !== 1 || (await pass.count()) !== 1) {
    return { loggedIn: false, reason: "login form not found", title, url: page.url() };
  }
  await phone.fill(username);
  await pass.fill(password);
  const agree = page.locator("#IHaveReadAndAgreedToTheServiceAgreementAndPrivacyPolicy");
  if ((await agree.count()) === 1) {
    await agree.setChecked(true).catch(async () => {
      const label = page.locator('label[for="IHaveReadAndAgreedToTheServiceAgreementAndPrivacyPolicy"]');
      if ((await label.count()) === 1) await label.click();
    });
  }
  await page.locator("button.loginsdk-button").click();
  await page.waitForTimeout(3000);
  const bodyText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
  if (/验证码|滑块|短信|安全验证/.test(bodyText)) {
    return { loggedIn: false, reason: "manual verification required", title: await page.title(), url: page.url() };
  }
  return { loggedIn: !/loginv2/.test(page.url()), title: await page.title(), url: page.url() };
}

async function downloadWithContext(context, url, outputPath) {
  const response = await context.request.get(url, {
    headers: {
      Referer: "https://www.fxiaoke.com/",
      "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
    },
    timeout: 30000,
  });
  if (!response.ok()) {
    throw new Error(`HTTP ${response.status()} ${await response.text().catch(() => "")}`);
  }
  const buffer = await response.body();
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, buffer);
  return { status: "Cached", outputPath, fileSize: buffer.length };
}

async function main() {
  const input = safeJsonParse(await readStdin());
  if (!input.url || !input.outputPath) throw new Error("url/outputPath required");
  const browser = await chromium.connectOverCDP(input.cdpUrl || CDP_URL);
  const page = await firstPage(browser);
  const login = await loginIfNeeded(page, input.username, input.password);
  const context = page.context();
  try {
    const result = await downloadWithContext(context, input.url, input.outputPath);
    console.log(JSON.stringify({ ok: true, login, ...result }));
  } finally {
    await browser.close().catch(() => {});
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: String(error), stack: error.stack }));
  process.exitCode = 1;
});
