import { chromium } from "playwright";
import fs from "node:fs/promises";
import path from "node:path";

const CDP_URL = process.env.FXIAOKE_CDP_URL || "http://127.0.0.1:9333";
const DEFAULT_PROBE = process.env.FXIAOKE_PROBE_FILE || "";
const PAGE_SIZE = Number(process.env.FXIAOKE_PAGE_SIZE || "20");

function parseArg(name, fallback = "") {
  const prefix = `--${name}=`;
  const hit = process.argv.find((arg) => arg.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : fallback;
}

function cleanText(value) {
  return String(value ?? "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
}

function timestampToDate(value) {
  if (!value) return "";
  const date = new Date(Number(value));
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function parseJson(text, label) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${error.message}`);
  }
}

function csvEscape(value) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function toCsv(rows) {
  const headers = [
    "crm_order_id",
    "crm_order_no",
    "customer_id",
    "customer_name",
    "opportunity_id",
    "opportunity_name",
    "life_status",
    "order_date",
    "settlement_method",
    "order_amount",
    "received_amount",
    "receivable_amount",
    "invoice_amount",
    "product_amount",
    "logistics_status",
    "owner_department",
    "created_at",
    "updated_at",
    "attachment_files",
  ];
  return [headers.join(","), ...rows.map((row) => headers.map((key) => csvEscape(row[key])).join(","))].join("\n");
}

function normalizeOrder(row) {
  const attachments = Array.isArray(row.UDAttach1__c) ? row.UDAttach1__c : [];
  return {
    crm_order_id: cleanText(row._id || row.id || row.dataId || row.extend_obj_data_id || ""),
    crm_order_no: cleanText(row.name || row.order_no || ""),
    customer_id: cleanText(row.account_id || ""),
    customer_name: cleanText(row.account_id__r || ""),
    opportunity_id: cleanText(row.new_opportunity_id || ""),
    opportunity_name: cleanText(row.new_opportunity_id__r || ""),
    life_status: cleanText(row.life_status || ""),
    order_date: timestampToDate(row.order_time),
    settlement_method: cleanText(row.field_2nI76__c || row.field_2ni76__c || ""),
    order_amount: cleanText(row.order_amount || ""),
    received_amount: cleanText(row.payment_amount || ""),
    receivable_amount: cleanText(row.receivable_amount || ""),
    invoice_amount: cleanText(row.invoice_amount || ""),
    product_amount: cleanText(row.product_amount || ""),
    logistics_status: cleanText(row.logistics_status || ""),
    owner_department: cleanText(row.owner_department || ""),
    created_at: row.create_time ? new Date(Number(row.create_time)).toISOString() : "",
    updated_at: row.last_modified_time ? new Date(Number(row.last_modified_time)).toISOString() : "",
    attachment_files: attachments.map((item) => item.filename).filter(Boolean).join("; "),
  };
}

function makePayload(basePostData, offset, limit) {
  const payload = parseJson(basePostData, "captured list payload");
  const query = parseJson(payload.search_query_info || "{}", "search_query_info");
  query.limit = limit;
  query.offset = offset;
  payload.search_query_info = JSON.stringify(query);
  return payload;
}

async function firstPage(browser) {
  const context = browser.contexts()[0] || (await browser.newContext());
  return context.pages()[0] || (await context.newPage());
}

async function main() {
  const probePath = parseArg("probe", DEFAULT_PROBE);
  const requestPath = parseArg("request", process.env.FXIAOKE_REQUEST_FILE || "");
  if (!probePath && !requestPath) {
    throw new Error("Probe or request file required: --probe=/private/tmp/fxiaoke-cdp-probe-....json or --request=/private/tmp/fxiaoke-list-request.json");
  }
  let listRequest;
  if (requestPath) {
    listRequest = parseJson(await fs.readFile(requestPath, "utf8"), requestPath);
  } else {
    const probe = parseJson(await fs.readFile(probePath, "utf8"), probePath);
    listRequest = probe.requests.find((request) => request.url.includes("/SalesOrderObj/controller/List?"));
  }
  if (!listRequest) throw new Error("SalesOrderObj List request not found in probe file");

  const browser = await chromium.connectOverCDP(CDP_URL);
  const page = await firstPage(browser);
  const rows = [];
  const rawPages = [];
  let total = null;

  for (let offset = 0; total === null || offset < total; offset += PAGE_SIZE) {
    const payload = makePayload(listRequest.postData, offset, PAGE_SIZE);
    const result = await page.evaluate(
      async ({ url, payload }) => {
        const response = await fetch(url, {
          method: "POST",
          credentials: "include",
          headers: {
            "content-type": "application/json;charset=UTF-8",
          },
          body: JSON.stringify(payload),
        });
        return {
          ok: response.ok,
          status: response.status,
          text: await response.text(),
        };
      },
      { url: listRequest.url, payload },
    );
    if (!result.ok) throw new Error(`List request failed: HTTP ${result.status}`);
    const body = parseJson(result.text, `List response offset ${offset}`);
    const dataList = body.Value?.dataList || [];
    rawPages.push({ offset, count: dataList.length, result: body.Result, total: dataList[0]?.total_num ?? null });
    for (const item of dataList) rows.push(normalizeOrder(item));
    if (total === null) total = Number(dataList[0]?.total_num || dataList.length || 0);
    if (dataList.length === 0) break;
  }

  const deduped = Array.from(new Map(rows.map((row) => [row.crm_order_no || row.crm_order_id, row])).values());
  const stamp = Date.now();
  const jsonPath = path.join("/private/tmp", `fxiaoke-sales-orders-${stamp}.json`);
  const csvPath = path.join("/private/tmp", `fxiaoke-sales-orders-${stamp}.csv`);
  await fs.writeFile(jsonPath, JSON.stringify({ total, count: deduped.length, pages: rawPages, rows: deduped }, null, 2));
  await fs.writeFile(csvPath, toCsv(deduped));
  console.log(JSON.stringify({ ok: true, total, count: deduped.length, jsonPath, csvPath, pages: rawPages.map((p) => ({ offset: p.offset, count: p.count, total: p.total })) }, null, 2));
  await browser.close().catch(() => {});
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: String(error), stack: error.stack }, null, 2));
  process.exitCode = 1;
});
