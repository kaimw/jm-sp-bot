import fs from "node:fs/promises";
import { chromium } from "playwright";

function parseArg(name, fallback = "") {
  const prefix = `--${name}=`;
  const hit = process.argv.find((arg) => arg.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : fallback;
}

function clean(value) {
  return String(value ?? "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
}

const cdpUrl = process.env.FXIAOKE_CDP_URL || "http://127.0.0.1:9333";
const navigateUrl = parseArg("url", "");
const out = parseArg("out", `/private/tmp/fxiaoke-list-${Date.now()}.json`);
const waitMs = Number(parseArg("wait-ms", "3000"));

const browser = await chromium.connectOverCDP(cdpUrl);
try {
  const page = browser.contexts()[0]?.pages()[0] || (await browser.newPage());
  if (navigateUrl) {
    await page.goto(navigateUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(waitMs);
  }
  const payload = await page.evaluate(() => {
    const cleanText = (value) => String(value || "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
    const headerCandidates = Array.from(document.querySelectorAll("thead th, [role='columnheader'], .ant-table-thead th, .el-table__header th, th"));
    const headers = headerCandidates.map((node) => cleanText(node.innerText || node.textContent)).filter(Boolean);
    const rowCandidates = Array.from(document.querySelectorAll("tbody tr, [role='row'], .ant-table-tbody tr, .el-table__row"));
    const rows = [];
    for (const rowNode of rowCandidates) {
      const cells = Array.from(rowNode.querySelectorAll("td, [role='gridcell'], .cell"));
      const values = cells.map((cell) => cleanText(cell.innerText || cell.textContent)).filter(Boolean);
      if (values.length < 2) continue;
      const row = {};
      values.forEach((value, index) => {
        row[headers[index] || `col_${index + 1}`] = value;
      });
      row.__values = values;
      rows.push(row);
    }
    return {
      title: document.title,
      url: location.href,
      headers,
      rows,
      text: cleanText(document.body.innerText || "").slice(0, 20000),
    };
  });
  await fs.writeFile(out, JSON.stringify(payload, null, 2), "utf-8");
  console.log(JSON.stringify({ ok: true, out, title: payload.title, url: payload.url, rowCount: payload.rows.length, headers: payload.headers.slice(0, 20) }, null, 2));
} finally {
  await browser.close();
}
