import { chromium } from "playwright";
import fs from "node:fs/promises";

const CDP_URL = process.env.FXIAOKE_CDP_URL || "http://127.0.0.1:9333";
const TARGET_URL = "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj";
const targetOrderNo = process.env.FXIAOKE_ORDER_NO || "20260520-006881";

function interestingUrl(url) {
  return /SalesOrderObj|object|Detail|detail|controller|data|relation|attachment|file|annex|FHH|EM1HNCRM/i.test(url);
}

async function firstPage(browser) {
  const context = browser.contexts()[0] || (await browser.newContext());
  return context.pages()[0] || (await context.newPage());
}

const browser = await chromium.connectOverCDP(CDP_URL);
const page = await firstPage(browser);
const requests = [];
const responses = [];
let capture = false;

page.on("request", (request) => {
  if (!capture || !interestingUrl(request.url())) return;
  requests.push({
    method: request.method(),
    resourceType: request.resourceType(),
    url: request.url(),
    postData: request.postData() || "",
  });
});

page.on("response", async (response) => {
  if (!capture || !interestingUrl(response.url())) return;
  const request = response.request();
  const item = {
    status: response.status(),
    method: request.method(),
    resourceType: request.resourceType(),
    url: response.url(),
    contentType: response.headers()["content-type"] || "",
  };
  if (/json|text/i.test(item.contentType)) {
    try {
      item.bodySnippet = (await response.text()).slice(0, 50000);
    } catch (error) {
      item.bodyError = String(error);
    }
  }
  responses.push(item);
});

await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
await page.waitForTimeout(8000);
capture = true;

const bodySample = (await page.locator("body").innerText({ timeout: 5000 }).catch(() => "")).slice(0, 2000);
let clicked = false;
const exactText = page.getByText(targetOrderNo, { exact: true }).first();
if ((await exactText.count()) > 0) {
  await exactText.click({ force: true });
  clicked = true;
}
if (!clicked) {
  const fuzzyText = page.getByText(targetOrderNo).first();
  if ((await fuzzyText.count()) > 0) {
    await fuzzyText.click({ force: true });
    clicked = true;
  }
}
if (!clicked) {
  const firstCell = page.locator("td.td-name").first();
  if ((await firstCell.count()) > 0) {
    await firstCell.click({ force: true });
    clicked = true;
  }
}

await page.waitForTimeout(12000);
capture = false;

const output = {
  ok: true,
  targetOrderNo,
  pageUrl: page.url(),
  clicked,
  bodySample,
  requestCount: requests.length,
  responseCount: responses.length,
  requests,
  responses,
};
const outPath = `/private/tmp/fxiaoke-detail-probe-${Date.now()}.json`;
await fs.writeFile(outPath, JSON.stringify(output, null, 2));
console.log(JSON.stringify({ ok: true, outPath, clicked, requestCount: requests.length, responseCount: responses.length, pageUrl: page.url(), bodySample: bodySample.slice(0, 300) }, null, 2));
await browser.close().catch(() => {});
