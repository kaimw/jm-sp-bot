import { chromium } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const CDP_URL = process.env.FXIAOKE_CDP_URL || "http://127.0.0.1:9333";
const TARGET_URL =
  "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj";
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
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function interestingUrl(url) {
  return /fxiaoke|FHH|EM1H|crm|object|list|query|data|SalesOrderObj|order/i.test(url);
}

function redact(text) {
  if (!text) return text;
  return String(text)
    .replace(/("?(?:password|pwd|passwd|token|fs_token|access_token|authorization)"?\s*[:=]\s*)("[^"]+"|[^&\s,}]+)/gi, "$1[REDACTED]")
    .replace(/\b1\d{10}\b/g, "[REDACTED_PHONE]");
}

async function wait(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function firstPage(browser) {
  const contexts = browser.contexts();
  const context = contexts[0] || (await browser.newContext());
  const pages = context.pages();
  return pages[0] || (await context.newPage());
}

async function loginIfNeeded(page, username, password) {
  await page.goto(LOGIN_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(2000);
  const title = await page.title().catch(() => "");
  const url = page.url();
  if (!/loginv2|登录/i.test(url + title)) return { loggedIn: true, skipped: "already logged in" };

  const phone = page.locator('input[name="phoneNumber"]');
  const pass = page.locator('input[name="password"]');
  if ((await phone.count()) !== 1 || (await pass.count()) !== 1) {
    return { loggedIn: false, reason: "login form not found", title, url };
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

  const modalText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
  if (modalText.includes("同意并继续")) {
    await page.getByText("同意并继续", { exact: true }).click().catch(async () => {
      const buttons = page.locator("button, a, [role=button]");
      const count = await buttons.count();
      for (let i = 0; i < Math.min(count, 30); i += 1) {
        const text = await buttons.nth(i).innerText().catch(() => "");
        if (text.trim() === "同意并继续") {
          await buttons.nth(i).click();
          break;
        }
      }
    });
    await page.waitForTimeout(4000);
  }

  const bodyText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
  if (/验证码|滑块|短信|安全验证/.test(bodyText)) {
    return { loggedIn: false, reason: "manual verification required", title: await page.title(), url: page.url() };
  }
  return { loggedIn: !/loginv2/.test(page.url()), title: await page.title(), url: page.url() };
}

async function main() {
  const input = safeJsonParse(await readStdin()) || {};
  const username = input.username || process.env.FXIAOKE_USERNAME;
  const password = input.password || process.env.FXIAOKE_PASSWORD;
  if (!username || !password) throw new Error("username/password required via stdin JSON");

  const browser = await chromium.connectOverCDP(CDP_URL);
  const page = await firstPage(browser);
  const login = await loginIfNeeded(page, username, password);
  if (!login.loggedIn) {
    console.log(JSON.stringify({ ok: false, login }, null, 2));
    return;
  }

  const requests = [];
  const responseBodies = [];
  let capture = true;

  page.on("request", (request) => {
    if (!capture) return;
    const url = request.url();
    if (!interestingUrl(url)) return;
    const postData = request.postData();
    requests.push({
      method: request.method(),
      resourceType: request.resourceType(),
      url,
      headers: Object.fromEntries(
        Object.entries(request.headers()).filter(([key]) =>
          /content-type|x-|trace|fs|referer|origin/i.test(key),
        ),
      ),
      postData: redact(postData ? postData.slice(0, 30000) : ""),
    });
  });

  page.on("response", async (response) => {
    if (!capture) return;
    const request = response.request();
    const url = response.url();
    if (!interestingUrl(url)) return;
    const contentType = response.headers()["content-type"] || "";
    const item = {
      status: response.status(),
      method: request.method(),
      resourceType: request.resourceType(),
      url,
      contentType,
    };
    if (/json|text|javascript/i.test(contentType)) {
      try {
        item.bodySnippet = redact((await response.text()).slice(0, 30000));
      } catch (error) {
        item.bodyError = String(error);
      }
    }
    responseBodies.push(item);
  });

  await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(12000);

  const tableSample = await page.evaluate(() => {
    const clean = (s) => (s || "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
    const orderNos = Array.from(document.querySelectorAll("td.td-name"))
      .map((td) => clean(td.innerText || td.getAttribute("title") || ""))
      .filter(Boolean);
    const col = (selector) => Array.from(document.querySelectorAll(selector)).map((td) => clean(td.innerText));
    const customer = col("td.td-account_id");
    const opportunity = col("td.td-new_opportunity_id");
    const status = col("td.td-life_status");
    const date = col("td.td-order_time");
    const settlement = col("td.td-field_2ni76__c");
    const amount = col("td.td-order_amount");
    const paid = col("td.td-payment_amount");
    return orderNos.slice(0, 20).map((orderNo, index) => ({
      crm_order_no: orderNo,
      customer_name: customer[index] || "",
      opportunity_name: opportunity[index] || "",
      life_status: status[index] || "",
      order_date: date[index] || "",
      settlement_method: settlement[index] || "",
      order_amount: amount[index] || "",
      received_amount: paid[index] || "",
    }));
  });

  capture = false;
  const output = {
    ok: true,
    capturedAt: new Date().toISOString(),
    login,
    page: { title: await page.title(), url: page.url() },
    requestCount: requests.length,
    responseCount: responseBodies.length,
    requests,
    responses: responseBodies,
    tableSample,
  };
  const outPath = path.join("/private/tmp", `fxiaoke-cdp-probe-${Date.now()}.json`);
  await fs.writeFile(outPath, JSON.stringify(output, null, 2));
  console.log(JSON.stringify({ ok: true, outPath, requestCount: requests.length, responseCount: responseBodies.length, tableRows: tableSample.length }, null, 2));
  await browser.close().catch(() => {});
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: String(error), stack: error.stack }, null, 2));
  process.exitCode = 1;
});
