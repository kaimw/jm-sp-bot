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
  if (value && typeof value === "object") {
    return cleanText(
      value.name ||
      value.label ||
      value.display_name ||
      value.displayName ||
      value.value ||
      value.full_name ||
      value.nickName ||
      value.text ||
      "",
    );
  }
  return String(value ?? "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
}

function firstValue(...values) {
  for (const value of values) {
    const text = cleanText(value);
    if (text) return text;
  }
  return "";
}

function timestampToDate(value) {
  if (!value) return "";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);
  const date = new Date(Number(value));
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function describeFields(body) {
  return body?.Value?.objectDescribeExt?.fields || body?.objectDescribeExt?.fields || {};
}

function optionLabel(body, fieldName, value) {
  const text = cleanText(value);
  if (!text) return "";
  const options = describeFields(body)?.[fieldName]?.options || [];
  const hit = options.find((item) => cleanText(item.value) === text);
  return cleanText(hit?.label) || text;
}

function valueByFieldLabels(body, detail, labelPattern, fallbackKeys = []) {
  for (const key of fallbackKeys) {
    const value = detail?.[key];
    if (cleanText(value)) return value;
  }
  for (const [key, field] of Object.entries(describeFields(body))) {
    const label = `${field?.label || ""} ${field?.label_r || ""} ${field?.description || ""} ${key}`;
    if (labelPattern.test(label) && cleanText(detail?.[key])) return detail[key];
  }
  return "";
}

function parseJson(text, label) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${error.message}`);
  }
}

function applyTemplate(value, row) {
  if (typeof value === "string") {
    return value
      .replaceAll("{{crm_order_id}}", row.crm_order_id || "")
      .replaceAll("{{crm_order_no}}", row.crm_order_no || "")
      .replaceAll("{{id}}", row.crm_order_id || "")
      .replaceAll("{{name}}", row.crm_order_no || "");
  }
  if (Array.isArray(value)) return value.map((item) => applyTemplate(item, row));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, applyTemplate(item, row)]));
  }
  return value;
}

function deepValues(value, predicate, results = []) {
  if (results.length >= 20) return results;
  if (Array.isArray(value)) {
    for (const item of value) deepValues(item, predicate, results);
    return results;
  }
  if (value && typeof value === "object") {
    if (predicate(value)) results.push(value);
    for (const item of Object.values(value)) deepValues(item, predicate, results);
  }
  return results;
}

function detailObjectFromResponse(body, row) {
  const directCandidates = [
    body?.Value?.data,
    body?.Value?.objectData,
    body?.Value?.detail,
    body?.Value,
    body?.data,
    body?.objectData,
  ].filter((item) => item && typeof item === "object" && !Array.isArray(item));
  const matching = deepValues(body, (item) => {
    const id = cleanText(item._id || item.id || item.dataId || item.extend_obj_data_id || item.objectDataId);
    const name = cleanText(item.name || item.order_no || item.crm_order_no);
    return (row.crm_order_id && id === row.crm_order_id) || (row.crm_order_no && name === row.crm_order_no);
  });
  return matching[0] || directCandidates[0] || body;
}

function normalizeAttachments(...sources) {
  const raw = [];
  const seen = new Set();
  for (const source of sources) {
    if (!source) continue;
    if (Array.isArray(source)) raw.push(...source);
    else if (typeof source === "string") raw.push(...source.split(/[;；]/).map((item) => item.trim()).filter(Boolean));
  }
  return raw
    .map((item) => {
      if (typeof item === "string") {
        const key = `|${item.toLowerCase()}`;
        if (seen.has(key)) return null;
        seen.add(key);
        return { file_name: item, raw: item };
      }
      if (!item || typeof item !== "object") return null;
      const fileName = firstValue(item.file_name, item.filename, item.name, item.fileName);
      const fileUrl = firstValue(item.file_url, item.url, item.signedUrl, item.signed_url, item.download_url, item.downloadUrl, item.preview_url, item.previewUrl);
      const fileId = firstValue(item.file_id, item.fileId, item.id, item.fs_file_id, item.fsFileId);
      if (!fileName && !fileUrl && !fileId) return null;
      const key = `${fileId.toLowerCase()}|${fileName.toLowerCase() || fileUrl.toLowerCase()}`;
      if (seen.has(key)) return null;
      seen.add(key);
      return {
        file_name: fileName,
        file_url: fileUrl,
        file_id: fileId,
        raw: item,
      };
    })
    .filter(Boolean);
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
  const normalizedAttachments = normalizeAttachments(row.UDAttach1__c, row.attachments, row.attachment_files);
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
    attachment_files: normalizedAttachments.map((item) => item.file_name).filter(Boolean).join("; "),
    attachments: normalizedAttachments,
  };
}

function normalizeOrderDetail(body, row) {
  const detail = detailObjectFromResponse(body, row);
  const attachments = normalizeAttachments(
    detail.UDAttach1__c,
    detail.attachments,
    detail.attachment_files,
    detail.files,
    body?.Value?.attachments,
    body?.attachments,
  );
  return {
    crm_order_id: firstValue(detail._id, detail.id, detail.dataId, detail.extend_obj_data_id, row.crm_order_id),
    crm_order_no: firstValue(detail.name, detail.order_no, detail.crm_order_no, row.crm_order_no),
    customer_id: firstValue(detail.account_id, detail.customer_id, row.customer_id),
    customer_name: firstValue(detail.account_id__r, detail.customer_name, row.customer_name),
    opportunity_id: firstValue(detail.new_opportunity_id, detail.opportunity_id, row.opportunity_id),
    opportunity_name: firstValue(detail.new_opportunity_id__r, detail.opportunity_name, row.opportunity_name),
    sales_user_id: firstValue(detail.owner, detail.owner_id, detail.sales_user_id, row.sales_user_id),
    sales_user_name: firstValue(detail.owner__r, detail.owner_name, detail.sales_user_name, row.sales_user_name),
    owner_department: firstValue(detail.owner_department, detail.department, row.owner_department),
    life_status: firstValue(detail.life_status, row.life_status),
    approval_status: firstValue(detail.approval_status, detail.approve_status, row.approval_status),
    order_date: timestampToDate(detail.order_time) || row.order_date,
    settlement_method: firstValue(detail.field_2nI76__c, detail.field_2ni76__c, detail.settlement_method, row.settlement_method),
    order_amount: firstValue(detail.order_amount, row.order_amount),
    received_amount: firstValue(detail.payment_amount, detail.received_amount, row.received_amount),
    receivable_amount: firstValue(detail.receivable_amount, row.receivable_amount),
    invoice_amount: firstValue(detail.invoice_amount, row.invoice_amount),
    product_amount: firstValue(detail.product_amount, row.product_amount),
    logistics_status: optionLabel(body, "logistics_status", detail.logistics_status) || firstValue(detail.logistics_status, row.logistics_status),
    shipment_status: optionLabel(body, "order_status", detail.order_status) || firstValue(detail.shipment_status, detail.delivery_status, row.shipment_status),
    invoice_status: optionLabel(body, "invoice_status", detail.invoice_status) || firstValue(detail.invoice_status, row.invoice_status),
    receipt_contact: firstValue(
      valueByFieldLabels(body, detail, /收货人|收件人|收货联系人|收件联系人|联系人/, ["receipt_contact", "ship_to_id__r", "ship_to_id", "receiver", "consignee", "contact_name"]),
      row.receipt_contact,
    ),
    receipt_address: firstValue(
      valueByFieldLabels(body, detail, /收货地址|收件地址|客户收件信息|配送地址/, ["receipt_address", "ship_to_add", "receive_address", "address", "shipping_address"]),
      row.receipt_address,
    ),
    delivery_date: timestampToDate(valueByFieldLabels(body, detail, /交货日期|交付日期|期望交期|要求交期/, ["delivery_date", "expected_delivery_date", "delivery_time"]))
      || timestampToDate(detail.confirmed_delivery_date)
      || firstValue(detail.delivery_date, detail.expected_delivery_date, row.delivery_date),
    remark: firstValue(detail.remark, detail.delivery_comment, detail.description, detail.note, row.remark),
    attachment_files: attachments.map((item) => item.file_name).filter(Boolean).join("; ") || row.attachment_files,
    attachments: attachments.length ? attachments : row.attachments,
    detail_sync_status: "Synced",
    detail_raw: body,
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

function makeDetailRequest(detailRequest, row) {
  const templated = applyTemplate(detailRequest, row);
  let postData = templated.postData ?? templated.body ?? "";
  if (postData && typeof postData !== "string") postData = JSON.stringify(postData);
  return {
    method: templated.method || "POST",
    url: templated.url,
    postData,
  };
}

async function replayDetail(page, detailRequest, row) {
  const request = makeDetailRequest(detailRequest, row);
  if (!request.url) throw new Error("Detail request missing url");
  const result = await page.evaluate(
    async ({ request }) => {
      const response = await fetch(request.url, {
        method: request.method,
        credentials: "include",
        headers: {
          "content-type": "application/json;charset=UTF-8",
        },
        body: request.method.toUpperCase() === "GET" ? undefined : request.postData,
      });
      return {
        ok: response.ok,
        status: response.status,
        text: await response.text(),
      };
    },
    { request },
  );
  if (!result.ok) throw new Error(`Detail request failed for ${row.crm_order_no || row.crm_order_id}: HTTP ${result.status}`);
  return parseJson(result.text, `Detail response ${row.crm_order_no || row.crm_order_id}`);
}

async function main() {
  const probePath = parseArg("probe", DEFAULT_PROBE);
  const requestPath = parseArg("request", process.env.FXIAOKE_REQUEST_FILE || "");
  const detailRequestPath = parseArg("detail-request", process.env.FXIAOKE_DETAIL_REQUEST_FILE || "");
  const singleRowPath = parseArg("single-row", "");
  if (!probePath && !requestPath && !singleRowPath) {
    throw new Error("Probe or request file required: --probe=/private/tmp/fxiaoke-cdp-probe-....json or --request=/private/tmp/fxiaoke-list-request.json");
  }
  let listRequest = null;
  if (!singleRowPath) {
    if (requestPath) {
      listRequest = parseJson(await fs.readFile(requestPath, "utf8"), requestPath);
    } else {
      const probe = parseJson(await fs.readFile(probePath, "utf8"), probePath);
      listRequest = probe.requests.find((request) => request.url.includes("/SalesOrderObj/controller/List?"));
    }
    if (!listRequest) throw new Error("SalesOrderObj List request not found in probe file");
  }
  const detailRequest = detailRequestPath ? parseJson(await fs.readFile(detailRequestPath, "utf8"), detailRequestPath) : null;
  if (singleRowPath && !detailRequest) throw new Error("Single row detail sync requires --detail-request");

  const browser = await chromium.connectOverCDP(CDP_URL);
  const page = await firstPage(browser);
  const rows = [];
  const rawPages = [];
  let total = null;

  if (singleRowPath) {
    rows.push(parseJson(await fs.readFile(singleRowPath, "utf8"), singleRowPath));
    total = 1;
  }

  for (let offset = 0; !singleRowPath && (total === null || offset < total); offset += PAGE_SIZE) {
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
  const detailPages = [];
  if (detailRequest) {
    for (const row of deduped) {
      try {
        const body = await replayDetail(page, detailRequest, row);
        Object.assign(row, normalizeOrderDetail(body, row));
        detailPages.push({ crm_order_id: row.crm_order_id, crm_order_no: row.crm_order_no, status: "Synced" });
      } catch (error) {
        row.detail_sync_status = "Failed";
        row.detail_sync_error = String(error.message || error);
        detailPages.push({ crm_order_id: row.crm_order_id, crm_order_no: row.crm_order_no, status: "Failed", error: row.detail_sync_error });
      }
    }
  } else {
    for (const row of deduped) row.detail_sync_status = "ListOnly";
  }
  const stamp = Date.now();
  const jsonPath = path.join("/private/tmp", `fxiaoke-sales-orders-${stamp}.json`);
  const csvPath = path.join("/private/tmp", `fxiaoke-sales-orders-${stamp}.csv`);
  await fs.writeFile(jsonPath, JSON.stringify({ total, count: deduped.length, pages: rawPages, detailPages, rows: deduped }, null, 2));
  await fs.writeFile(csvPath, toCsv(deduped));
  console.log(JSON.stringify({ ok: true, total, count: deduped.length, jsonPath, csvPath, pages: rawPages.map((p) => ({ offset: p.offset, count: p.count, total: p.total })), detailPages }, null, 2));
  await browser.close().catch(() => {});
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: String(error), stack: error.stack }, null, 2));
  process.exitCode = 1;
});
