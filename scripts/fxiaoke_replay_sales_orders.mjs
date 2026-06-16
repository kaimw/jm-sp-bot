import fs from "node:fs/promises";
import path from "node:path";

const CDP_URL = process.env.FXIAOKE_CDP_URL || "http://127.0.0.1:9333";
const DEFAULT_PROBE = process.env.FXIAOKE_PROBE_FILE || "";
const PAGE_SIZE = Number(process.env.FXIAOKE_PAGE_SIZE || "20");
const MAX_PAGES = Number(process.env.FXIAOKE_MAX_PAGES || "0");
const DETAIL_ENABLED = !["0", "false", "no", "off"].includes(String(process.env.FXIAOKE_DETAIL_ENABLED || "true").toLowerCase());
const REQUEST_TIMEOUT_MS = Number(process.env.FXIAOKE_REQUEST_TIMEOUT_MS || "15000");
const DOM_FALLBACK_ENABLED = !["0", "false", "no", "off"].includes(String(process.env.FXIAOKE_DOM_FALLBACK_ENABLED || "true").toLowerCase());
const CRM_USERNAME = process.env.FXIAOKE_USERNAME || "";
const CRM_PASSWORD = process.env.FXIAOKE_PASSWORD || "";
const CRM_HOME_URL = "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj";
const CRM_LOGIN_URL = `https://www.fxiaoke.com/proj/page/loginv2?returnUrl=${encodeURIComponent(CRM_HOME_URL)}`;
const CDP_COMMAND_TIMEOUT_MS = Number(process.env.FXIAOKE_CDP_COMMAND_TIMEOUT_MS || "60000");
const LOGIN_COOLDOWN_MS = Number(process.env.FXIAOKE_LOGIN_COOLDOWN_MS || String(10 * 60 * 1000));
const LOGIN_SETTLE_MS = Number(process.env.FXIAOKE_LOGIN_SETTLE_MS || "8000");
const LOGIN_STATE_FILE = process.env.FXIAOKE_LOGIN_STATE_FILE || "/private/tmp/fxiaoke-login-renewal-state.json";

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

function emailFromValue(value, seen = new Set()) {
  if (!value) return "";
  if (typeof value === "string") {
    const match = value.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
    return match ? match[0] : "";
  }
  if (typeof value !== "object") return "";
  if (seen.has(value)) return "";
  seen.add(value);
  for (const key of ["email", "email_address", "emailAddress", "mail", "work_email", "workEmail", "user_email", "userEmail"]) {
    const hit = emailFromValue(value[key], seen);
    if (hit) return hit;
  }
  for (const item of Object.values(value)) {
    const hit = emailFromValue(item, seen);
    if (hit) return hit;
  }
  return "";
}

function employeeIdFromValue(value, seen = new Set()) {
  if (!value) return "";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") {
    const text = cleanText(value);
    const profile = text.match(/empid[-=](\d+)/i);
    if (profile) return profile[1];
    if (/^\d{3,}$/.test(text)) return text;
    return "";
  }
  if (typeof value !== "object") return "";
  if (seen.has(value)) return "";
  seen.add(value);
  for (const key of ["employee_id", "employeeId", "emp_id", "empId", "user_id", "userId", "id", "value"]) {
    const hit = employeeIdFromValue(value[key], seen);
    if (hit) return hit;
  }
  for (const item of Object.values(value)) {
    const hit = employeeIdFromValue(item, seen);
    if (hit) return hit;
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

function emailByFieldLabels(body, detail, labelPattern, fallbackKeys = []) {
  for (const key of fallbackKeys) {
    const value = detail?.[key];
    const email = emailFromValue(value);
    if (email) return email;
  }
  for (const [key, field] of Object.entries(describeFields(body))) {
    const label = `${field?.label || ""} ${field?.label_r || ""} ${field?.description || ""} ${key}`;
    if (labelPattern.test(label)) {
      const email = emailFromValue(detail?.[key]);
      if (email) return email;
    }
  }
  return "";
}

function employeeIdByFieldLabels(body, detail, labelPattern, fallbackKeys = []) {
  for (const key of fallbackKeys) {
    const value = detail?.[key];
    const id = employeeIdFromValue(value);
    if (id) return id;
  }
  for (const [key, field] of Object.entries(describeFields(body))) {
    const label = `${field?.label || ""} ${field?.label_r || ""} ${field?.description || ""} ${key}`;
    if (labelPattern.test(label)) {
      const id = employeeIdFromValue(detail?.[key]);
      if (id) return id;
    }
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

function assertFxiaokeSuccess(body, label) {
  const result = body?.Result;
  const statusCode = Number(result?.StatusCode ?? 0);
  const failureMessage = cleanText(result?.FailureMessage);
  if (statusCode && statusCode !== 0) {
    throw new Error(`${label} failed: ${failureMessage || `StatusCode ${statusCode}`}`);
  }
}

function isLoginExpired(body) {
  const result = body?.Result;
  return Number(result?.StatusCode ?? 0) === 33 || /登录状态已过期|重新登录|未登录/.test(cleanText(result?.FailureMessage));
}

async function readLoginState() {
  try {
    return parseJson(await fs.readFile(LOGIN_STATE_FILE, "utf8"), LOGIN_STATE_FILE);
  } catch {
    return {};
  }
}

async function writeLoginState(patch) {
  const current = await readLoginState();
  await fs.writeFile(LOGIN_STATE_FILE, JSON.stringify({ ...current, ...patch }, null, 2), "utf8");
}

function manualLoginRequired(reason) {
  return new Error(`CRM 登录态已过期，自动续租未完成：${reason}。请人工打开 CRM 专用浏览器完成登录后再重试同步。`);
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
    sales_user_name: firstValue(
      valueByFieldLabels(body, detail, /(^|\s)负责人(\s|$)|销售负责人|订单负责人/, ["owner__r", "owner_name", "ownerName", "owner_display_name", "sales_user_name"]),
      row.sales_user_name,
    ),
    sales_user_email: firstValue(
      emailByFieldLabels(body, detail, /(^|\s)负责人(\s|$)|销售负责人|订单负责人/, ["owner", "owner_user", "ownerUser", "owner_info", "ownerInfo", "owner_email", "ownerEmail", "sales_user_email"]),
      emailByFieldLabels(body, detail, /创建人|创建者/, ["created_by", "createdBy", "creator", "creator_info", "creatorInfo", "create_user", "createUser", "created_by_email", "creator_email"]),
      emailByFieldLabels(body, detail, /最后修改人|最后修改者|修改人|修改者/, ["last_modified_by", "lastModifiedBy", "modified_by", "modifiedBy", "modifier", "modifier_info", "modifierInfo", "last_modified_by_email", "modifier_email"]),
      row.sales_user_email,
    ),
    owner_profile_id: firstValue(
      employeeIdByFieldLabels(body, detail, /(^|\s)负责人(\s|$)|销售负责人|订单负责人/, ["owner", "owner_id", "ownerId", "sales_user_id"]),
      row.owner_profile_id,
    ),
    created_by_profile_id: firstValue(
      employeeIdByFieldLabels(body, detail, /创建人|创建者/, ["created_by", "createdBy", "creator", "create_user", "createUser"]),
      row.created_by_profile_id,
    ),
    last_modified_by_profile_id: firstValue(
      employeeIdByFieldLabels(body, detail, /最后修改人|最后修改者|修改人|修改者/, ["last_modified_by", "lastModifiedBy", "modified_by", "modifiedBy", "modifier"]),
      row.last_modified_by_profile_id,
    ),
    owner_department: firstValue(
      valueByFieldLabels(body, detail, /负责人主属部门|主属部门|负责人部门|负责人所属部门/, ["owner_department", "owner_main_department", "ownerMainDepartment", "main_department", "department"]),
      row.owner_department,
    ),
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

async function connectReplayPage() {
  const baseUrl = CDP_URL.replace(/\/$/, "");
  const version = await fetch(`${baseUrl}/json/version`).then(async (response) => {
    if (!response.ok) throw new Error(`CDP version failed: HTTP ${response.status}`);
    return response.json();
  });
  if (!version.webSocketDebuggerUrl) throw new Error("CDP browser websocket url missing");
  const socket = new WebSocket(version.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(`${message.error.message || "CDP error"}${message.error.data ? `: ${message.error.data}` : ""}`));
    else resolve(message.result || {});
  });
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("CDP websocket connection failed")), { once: true });
  });
  const send = (method, params = {}, sessionId = null) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP command timed out: ${method}`));
    }, CDP_COMMAND_TIMEOUT_MS);
    pending.set(id, {
      resolve: (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      reject: (error) => {
        clearTimeout(timer);
        reject(error);
      },
    });
    socket.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
  });
  let targets = await send("Target.getTargets");
  let pages = (targets.targetInfos || []).filter((target) => target.type === "page");
  if (!pages.length) {
    await send("Target.createTarget", {
      url: "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj",
    });
    await new Promise((resolve) => setTimeout(resolve, 1500));
    targets = await send("Target.getTargets");
    pages = (targets.targetInfos || []).filter((target) => target.type === "page");
  }
  let target = pages.find((page) => String(page.url || "").includes("crm/list/=/SalesOrderObj"));
  if (!target) {
    const created = await send("Target.createTarget", { url: CRM_HOME_URL });
    await new Promise((resolve) => setTimeout(resolve, 2500));
    targets = await send("Target.getTargets");
    target = (targets.targetInfos || []).find((item) => item.targetId === created.targetId)
      || (targets.targetInfos || []).find((item) => item.type === "page" && String(item.url || "").includes("crm/list/=/SalesOrderObj"));
  }
  if (!target) throw new Error("No SalesOrderObj page target found from CDP");
  const attached = await send("Target.attachToTarget", { targetId: target.targetId, flatten: true });
  const sessionId = attached.sessionId;
  await send("Runtime.enable", {}, sessionId);
  if (!String(target.url || "").includes("crm/list/=/SalesOrderObj")) {
    await send("Page.enable", {}, sessionId);
    await send("Page.navigate", { url: CRM_HOME_URL }, sessionId);
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
  return {
    async evaluate(fn, arg) {
      const expression = `(${fn.toString()})(${JSON.stringify(arg)})`;
      const result = await send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      }, sessionId);
      if (result.exceptionDetails) {
        const detail = result.exceptionDetails.exception?.description || result.exceptionDetails.text || "Runtime.evaluate failed";
        throw new Error(detail);
      }
      return result.result?.value;
    },
    async navigate(url) {
      await send("Page.navigate", { url }, sessionId);
    },
    async readProfileEmail(employeeId) {
      const id = cleanText(employeeId);
      if (!id) return "";
      const profileUrl = `https://www.fxiaoke.com/XV/UI/Home#profile/=/empid-${encodeURIComponent(id)}`;
      const created = await send("Target.createTarget", { url: profileUrl });
      const attachedProfile = await send("Target.attachToTarget", { targetId: created.targetId, flatten: true });
      const profileSessionId = attachedProfile.sessionId;
      try {
        await send("Runtime.enable", {}, profileSessionId);
        await send("Page.enable", {}, profileSessionId);
        await new Promise((resolve) => setTimeout(resolve, 5000));
        const result = await send("Runtime.evaluate", {
          expression: `(() => {
            const text = String(document.body?.innerText || "");
            const match = text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
            return match ? match[0] : "";
          })()`,
          awaitPromise: true,
          returnByValue: true,
        }, profileSessionId);
        return cleanText(result.result?.value);
      } finally {
        await send("Target.detachFromTarget", { sessionId: profileSessionId }).catch(() => {});
        await send("Target.closeTarget", { targetId: created.targetId }).catch(() => {});
      }
    },
    async close() {
      await send("Target.detachFromTarget", { sessionId }).catch(() => {});
      socket.close();
    },
  };
}

async function autoLogin(page) {
  if (!CRM_USERNAME || !CRM_PASSWORD) {
    throw manualLoginRequired("未配置 CRM 账号密码");
  }
  const state = await readLoginState();
  const lastFailureAt = Number(state.last_failure_at || 0);
  const remainingCooldown = LOGIN_COOLDOWN_MS - (Date.now() - lastFailureAt);
  if (lastFailureAt && remainingCooldown > 0) {
    throw manualLoginRequired(`近期自动续租失败，为避免频繁登录触发风控，${Math.ceil(remainingCooldown / 1000)} 秒后再自动尝试`);
  }
  await page.navigate(CRM_LOGIN_URL);
  await new Promise((resolve) => setTimeout(resolve, 2500));
  const result = await page.evaluate(
    async ({ username, password, settleMs }) => {
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const visible = (element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      const clickByText = (patterns) => {
        const nodes = Array.from(document.querySelectorAll("button, a, div, span")).filter(visible);
        const hit = nodes.find((node) => patterns.some((pattern) => pattern.test((node.innerText || node.textContent || "").trim())));
        if (hit) {
          hit.click();
          return true;
        }
        return false;
      };
      clickByText([/账号登录/, /密码登录/, /帐号登录/, /手机.*登录/]);
      await sleep(500);

      const setValue = (element, value) => {
        const proto = Object.getPrototypeOf(element);
        const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
        if (descriptor?.set) descriptor.set.call(element, value);
        else element.value = value;
        element.dispatchEvent(new Event("input", { bubbles: true }));
        element.dispatchEvent(new Event("change", { bubbles: true }));
      };
      const inputs = Array.from(document.querySelectorAll("input")).filter(visible);
      const passwordInput = inputs.find((input) => String(input.type || "").toLowerCase() === "password");
      const usernameInput = inputs.find((input) => input !== passwordInput && ["", "text", "tel", "email", "number"].includes(String(input.type || "").toLowerCase()));
      if (!usernameInput || !passwordInput) {
        const bodyText = (document.body?.innerText || "").slice(0, 500);
        if (!/loginv2|登录|账号|密码/i.test(location.href + bodyText)) {
          return { ok: true, reason: "当前页面看起来已登录", url: location.href };
        }
        return { ok: false, manualRequired: true, reason: "未找到账号或密码输入框", url: location.href, inputCount: inputs.length, bodyText };
      }
      setValue(usernameInput, username);
      setValue(passwordInput, password);
      const clicked = clickByText([/^登录$/, /登\s*录/, /立即登录/]) || Boolean(passwordInput.form?.requestSubmit?.());
      if (!clicked && passwordInput.form) passwordInput.form.submit();
      await sleep(settleMs);
      const stillHasPasswordInput = Array.from(document.querySelectorAll("input")).some((input) => visible(input) && String(input.type || "").toLowerCase() === "password");
      const bodyText = (document.body?.innerText || "").slice(0, 500);
      const manualRequired = /验证码|短信|安全验证|滑块|拖动|二次验证|人机验证|风险|扫码/.test(bodyText);
      return { ok: !stillHasPasswordInput || !/loginv2|login/i.test(location.href), url: location.href, stillHasPasswordInput, manualRequired, bodyText };
    },
    { username: CRM_USERNAME, password: CRM_PASSWORD, settleMs: LOGIN_SETTLE_MS },
  );
  if (!result?.ok) {
    await writeLoginState({ last_failure_at: Date.now(), last_failure_reason: result?.reason || result?.bodyText || "登录后仍停留在登录页" });
    if (result?.manualRequired) {
      throw manualLoginRequired(result.reason || "CRM 要求验证码、短信或其他安全验证");
    }
    throw manualLoginRequired(result?.reason || "登录后仍停留在登录页，可能需要人工确认");
  }
  await writeLoginState({ last_success_at: Date.now(), last_failure_at: 0, last_failure_reason: "" });
  await new Promise((resolve) => setTimeout(resolve, 1500));
  return result;
}

async function ensureLoggedInForDom(page, reason = "当前页面停留在 CRM 登录页") {
  const loginState = await page.evaluate(() => {
    const text = String(document.body?.innerText || "").slice(0, 800);
    const hasPasswordInput = Array.from(document.querySelectorAll("input"))
      .some((input) => {
        const rect = input.getBoundingClientRect();
        const style = getComputedStyle(input);
        return String(input.type || "").toLowerCase() === "password"
          && rect.width > 0
          && rect.height > 0
          && style.visibility !== "hidden"
          && style.display !== "none";
      });
    return {
      url: location.href,
      isLoginPage: /loginv2|login/i.test(location.href) || hasPasswordInput || /验证码|短信|安全验证|账号登录|密码登录/.test(text),
      bodyText: text,
    };
  });
  if (loginState?.isLoginPage) {
    await autoLogin(page);
    await page.navigate(CRM_HOME_URL);
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  return loginState;
}

async function replayList(page, listRequest, offset, limit, allowLoginRetry = true) {
  const payload = makePayload(listRequest.postData, offset, limit);
  const result = await page.evaluate(
    async ({ url, payload, timeoutMs }) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, {
          method: "POST",
          credentials: "include",
          headers: {
            "content-type": "application/json;charset=UTF-8",
          },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
        return {
          ok: response.ok,
          status: response.status,
          text: await response.text(),
        };
      } finally {
        clearTimeout(timer);
      }
    },
    { url: listRequest.url, payload, timeoutMs: REQUEST_TIMEOUT_MS },
  );
  if (!result.ok) throw new Error(`List request failed: HTTP ${result.status}`);
  const body = parseJson(result.text, `List response offset ${offset}`);
  if (allowLoginRetry && isLoginExpired(body)) {
    await autoLogin(page);
    return replayList(page, listRequest, offset, limit, false);
  }
  assertFxiaokeSuccess(body, `List request offset ${offset}`);
  return body;
}

async function readListFromDom(page, limit = PAGE_SIZE) {
  await page.navigate(CRM_HOME_URL);
  await new Promise((resolve) => setTimeout(resolve, 3000));
  await ensureLoggedInForDom(page, "读取 CRM 列表前发现登录态失效");
  const rows = await page.evaluate(
    ({ limit }) => {
      const clean = (value) => String(value || "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
      const tableRows = Array.from(document.querySelectorAll("tr")).slice(0, 200);
      const rowInfo = tableRows
        .map((tr) => {
          const rect = tr.getBoundingClientRect();
          const cells = Array.from(tr.querySelectorAll("td")).map((td) => ({
            className: String(td.className || ""),
            title: clean(td.getAttribute("title") || ""),
            text: clean(td.innerText || td.getAttribute("title") || ""),
          }));
          return {
            tr,
            y: Math.round(rect.y * 2) / 2,
            width: rect.width,
            idAttr: tr.getAttribute("data-id") || tr.getAttribute("data-row-key") || "",
            cells,
            text: clean(cells.map((cell) => cell.text || cell.title).join(" ")),
          };
        })
        .filter((row) => row.y > 0 && row.cells.length);
      const fixedRows = rowInfo.filter((row) => /\b20\d{6}-\d{6}\b/.test(row.text));
      const detailRows = rowInfo.filter((row) => row.width > 1000 || row.cells.some((cell) => /td-account_id|td-order_time|td-life_status/.test(cell.className)));
      const parsed = [];
      const cellValue = (row, classPattern) => {
        const hit = row?.cells?.find((cell) => classPattern.test(cell.className));
        return hit ? (hit.text || hit.title) : "";
      };
      for (const fixed of fixedRows) {
        const orderNo = (fixed.text.match(/\b20\d{6}-\d{6}\b/) || [])[0];
        if (!orderNo) continue;
        const detail = detailRows.find((row) => Math.abs(row.y - fixed.y) < 2 && row.text && !row.text.includes(orderNo)) || null;
        const allCells = [...fixed.cells, ...(detail?.cells || [])];
        const allText = allCells.map((cell) => cell.text || cell.title).filter(Boolean);
        const customerName = cellValue(detail, /td-account_id/) || allText.find((text) => /公司|大学|科技|有限|研究院/.test(text)) || "";
        const amountText = cellValue(detail, /td-order_amount|td-sales_order_amount|amount/) || allText.find((text) => /^[\d,]+\.\d{2,3}$/.test(text)) || "";
        const dateText = cellValue(detail, /td-order_time/) || allText.find((text) => /^20\d{2}-\d{2}-\d{2}$/.test(text)) || "";
        const settlementMethod = cellValue(detail, /td-field_2ni76__c/) || allText.find((text) => /结算|CNY|USD|人民币/.test(text)) || "";
        const opportunityName = cellValue(detail, /td-new_opportunity_id/) || "";
        const lifeStatus = cellValue(detail, /td-life_status/) || (allText.includes("正常") ? "正常" : "");
        parsed.push({
          crm_order_id: fixed.idAttr || detail?.idAttr || orderNo,
          crm_order_no: orderNo,
          customer_name: customerName,
          opportunity_name: opportunityName,
          order_date: dateText,
          settlement_method: settlementMethod,
          order_amount: amountText.replace(/,/g, ""),
          life_status: lifeStatus,
          approval_status: "approved",
          detail_sync_status: "DomListOnly",
          raw_dom_cells: allText,
        });
        if (parsed.length >= limit) break;
      }
      return parsed;
    },
    { limit },
  );
  if (!rows.length) throw new Error("DOM fallback did not find SalesOrderObj rows");
  return rows;
}

async function readDetailFromDom(page, row) {
  await ensureLoggedInForDom(page, "读取 CRM 详情前发现登录态失效");
  const detail = await page.evaluate(
    async ({ crmOrderNo }) => {
      const clean = (value) => String(value || "").replace(/\u00a0/g, " ").replace(/[ \t\r\n]+/g, " ").trim();
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const visible = (element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      };
      const waitFor = async (predicate, timeoutMs = 10000) => {
        const started = Date.now();
        while (Date.now() - started < timeoutMs) {
          const value = predicate();
          if (value) return value;
          await sleep(250);
        }
        return null;
      };
      const hasDetailFields = () => {
        const text = clean(document.body?.innerText || "");
        const detailFieldCount = Array.from(document.querySelectorAll(".faci-field-display"))
          .filter((block) => /销售订单编号|产品合计|合同附件|创建时间|最后修改时间/.test(clean(block.innerText || block.textContent)))
          .length;
        return Boolean(text.includes(crmOrderNo) && (detailFieldCount >= 2 || /订单产品\(\d+\)|系统信息/.test(text)));
      };
      const loading = () => /加载中|正在加载|loading/i.test(clean(document.body?.innerText || ""));
      const detailReadiness = () => {
        const text = clean(document.body?.innerText || "");
        const labels = Array.from(document.querySelectorAll(".faci-field-display_label"))
          .map((node) => clean(node.innerText || node.textContent))
          .filter(Boolean);
        const hasHeadFields = labels.some((label) => /客户名称|负责人|销售订单金额|下单日期/.test(label));
        const hasProductArea = /订单产品(?:\(\d+\))?|产品名称|销售单价|小计/.test(text);
        const hasThisOrder = !crmOrderNo || text.includes(crmOrderNo);
        return hasThisOrder && !loading() && (hasHeadFields || hasProductArea || hasDetailFields());
      };
      const waitForStableDetail = async (timeoutMs = 45000) => {
        let lastSignature = "";
        let stableHits = 0;
        return waitFor(() => {
          if (!detailReadiness()) {
            stableHits = 0;
            lastSignature = "";
            return null;
          }
          const fieldsText = Array.from(document.querySelectorAll(".faci-field-display"))
            .map((node) => clean(node.innerText || node.textContent))
            .join("|");
          const productText = Array.from(document.querySelectorAll("table, [role='grid'], .ant-table, .el-table"))
            .map((node) => clean(node.innerText || node.textContent))
            .filter((text) => /产品|商品|数量|单价|小计|金额/.test(text))
            .join("|");
          const signature = `${fieldsText.length}:${productText.length}:${clean(document.body?.innerText || "").length}`;
          if (signature && signature === lastSignature) stableHits += 1;
          else stableHits = 0;
          lastSignature = signature;
          return stableHits >= 2 ? true : null;
        }, timeoutMs);
      };
      const clickByText = async (pattern, timeoutMs = 6000) => {
        const clicked = await waitFor(() => {
          const node = Array.from(document.querySelectorAll("a, button, span, div"))
            .find((item) => visible(item) && pattern.test(clean(item.innerText || item.textContent)));
          if (!node) return null;
          const clickable = node.closest("button, a, [role='button'], .ant-tabs-tab, .faci-tab-item, .faci-tabs-tab") || node;
          clickable.scrollIntoView({ block: "center", inline: "center" });
          clickable.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true, view: window }));
          clickable.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
          clickable.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
          clickable.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
          return true;
        }, timeoutMs);
        if (clicked) await sleep(800);
        return Boolean(clicked);
      };
      if (crmOrderNo && !hasDetailFields()) {
        const candidates = Array.from(document.querySelectorAll("a, button, span, div, td"))
          .filter((node) => visible(node) && clean(node.innerText || node.textContent) === crmOrderNo);
        const clickable = candidates.find((node) => ["A", "BUTTON"].includes(node.tagName))
          || candidates.map((node) => node.closest("td, a, button, .ant-table-row, tr, [role='row']")).find(Boolean)
          || candidates[0];
        if (clickable) {
          clickable.scrollIntoView({ block: "center", inline: "center" });
          await sleep(200);
          for (const type of ["mouseover", "mousedown", "mouseup", "click", "dblclick"]) {
            clickable.dispatchEvent(new MouseEvent(type, {
              bubbles: true,
              cancelable: true,
              view: window,
              detail: type === "dblclick" ? 2 : 1,
            }));
          }
          await waitForStableDetail(45000);
        }
      }
      if (crmOrderNo) {
        const text = clean(document.body?.innerText || "");
        if (!/销售订单编号|产品合计|合同附件|系统信息/.test(text) && /查看详情/.test(text)) {
          await clickByText(/^查看详情$/);
          await waitForStableDetail(45000);
        }
      }
      await waitForStableDetail(45000);
      const blocks = Array.from(document.querySelectorAll(".faci-field-display"));
      const fields = [];
      for (const block of blocks) {
        const label = clean(block.querySelector(".faci-field-display_label")?.innerText);
        const valueNode = block.querySelector(".faci-field-display_value");
        const value = clean(valueNode?.innerText || "");
        const link = block.querySelector("a[href*='empid-']");
        const href = link?.getAttribute("href") || "";
        const profileId = (href.match(/empid-(\d+)/i) || [])[1] || link?.getAttribute("data-cardid") || "";
        const apiName = block.getAttribute("data-apiname") || "";
        if (label) fields.push({ label, value, profileId, apiName });
      }
      const byLabel = (pattern) => {
        const hit = fields.find((field) => pattern.test(field.label));
        return hit?.value || "";
      };
      const profileByLabel = (pattern) => {
        const hit = fields.find((field) => pattern.test(field.label) && field.profileId);
        return hit?.profileId || "";
      };
      const readRowsFromTable = (table) => {
        const headerNodes = Array.from(table.querySelectorAll("thead th, [role='columnheader'], .ant-table-thead th, .el-table__header th"))
          .filter(visible);
        const headers = headerNodes.map((node) => clean(node.innerText || node.textContent)).filter(Boolean);
        const rowNodes = Array.from(table.querySelectorAll("tbody tr, .ant-table-tbody tr, .el-table__body tr, [role='row']"))
          .filter((node) => visible(node) && !node.matches("thead tr, .ant-table-thead tr, .el-table__header tr"));
        const rows = [];
        for (const rowNode of rowNodes) {
          const cells = Array.from(rowNode.querySelectorAll("td, [role='gridcell'], .ant-table-cell, .el-table__cell"))
            .filter(visible)
            .map((node) => clean(node.innerText || node.textContent))
            .filter(Boolean);
          if (cells.length < 2) continue;
          const row = { raw_values: cells, source: "dom_detail_order_products" };
          for (let index = 0; index < cells.length; index += 1) {
            const header = headers[index] || `列${index + 1}`;
            row[header] = cells[index];
          }
          rows.push(row);
        }
        return rows;
      };
      const normalizeProductRow = (row) => {
        const valueByHeader = (pattern) => {
          for (const [key, value] of Object.entries(row)) {
            if (pattern.test(String(key))) return clean(value);
          }
          return "";
        };
        const productName = valueByHeader(/商品名称|产品名称|成交产品|商品|产品|名称/);
        const skuCode = valueByHeader(/SKU|编码|货号|物料/);
        const quantity = valueByHeader(/数量|qty|件数/i);
        const unitPrice = valueByHeader(/单价|价格|unit/i).replace(/,/g, "");
        const lineAmount = valueByHeader(/金额|小计|合计|amount|total/i).replace(/,/g, "");
        return {
          ...row,
          product_name: productName || row.product_name || "",
          sku_code: skuCode || row.sku_code || "",
          quantity: quantity || row.quantity || "",
          unit_price: unitPrice || row.unit_price || "",
          line_amount: lineAmount || row.line_amount || "",
        };
      };
      const readOrderProductRows = () => {
        const candidates = [];
        const titles = Array.from(document.querySelectorAll("div, section, article, main, aside, h1, h2, h3, h4, span"))
          .filter((node) => visible(node) && /订单产品|产品明细|商品明细/.test(clean(node.innerText || node.textContent)));
        for (const title of titles) {
          let node = title;
          for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
            if (node.querySelector("table, [role='grid'], .ant-table, .el-table")) candidates.push(node);
          }
          const next = title.nextElementSibling;
          if (next) candidates.push(next);
        }
        candidates.push(document.body);
        const rows = [];
        const seen = new Set();
        for (const scope of candidates) {
          const tables = Array.from(scope.querySelectorAll("table, [role='grid'], .ant-table, .el-table"));
          for (const table of tables) {
            const tableText = clean(table.innerText || table.textContent);
            if (!/商品|产品|SKU|编码|数量|单价|金额/.test(tableText)) continue;
            for (const row of readRowsFromTable(table).map(normalizeProductRow)) {
              const key = JSON.stringify(row.raw_values || row);
              if (seen.has(key)) continue;
              seen.add(key);
              if (row.product_name || row.sku_code || row.quantity || row.line_amount) rows.push(row);
            }
          }
          if (rows.length) break;
        }
        return rows;
      };
      const readOrderProductRowsFromText = () => {
        const text = String(document.body?.innerText || "");
        const lines = text
          .split(/\n+/)
          .map((line) => clean(line))
          .filter((line) => line.includes(crmOrderNo));
        const rows = [];
        const customerName = byLabel(/^客户名称$/);
        for (const line of lines) {
          if (!/^\d{6,}\s+20\d{6}-\d{6}\s+/.test(line)) continue;
          const amounts = Array.from(line.matchAll(/[\d,]+(?:\.\d+)?/g)).map((match) => ({ value: match[0], index: match.index || 0 }));
          if (amounts.length < 5) continue;
          const tail = amounts.slice(-4).map((item) => item.value.replace(/,/g, ""));
          const prefix = clean(line.slice(0, amounts.slice(-4)[0].index));
          const beforeOrderNo = prefix.split(crmOrderNo)[0] || "";
          const productSegment = clean(prefix.slice(prefix.indexOf(crmOrderNo) + crmOrderNo.length));
          let productName = productSegment;
          if (customerName && productSegment.includes(customerName)) {
            productName = clean(productSegment.slice(productSegment.indexOf(customerName) + customerName.length));
          }
          if (!productName) continue;
          rows.push({
            source: "dom_detail_order_products_text",
            raw_values: [beforeOrderNo.trim(), crmOrderNo, customerName, productName, ...tail],
            product_name: productName,
            quantity: tail[1] || "",
            unit_price: tail[0] || "",
            line_amount: tail[3] || tail[2] || "",
          });
        }
        return rows;
      };
      if (crmOrderNo && !/订单产品\(\d+\)/.test(clean(document.body?.innerText || ""))) {
        await clickByText(/^订单产品(?:\(\d+\))?$/);
        await waitFor(() => /订单产品(?:\(\d+\))?|产品名称|销售单价|小计/.test(clean(document.body?.innerText || "")) && !loading(), 30000);
      } else {
        await clickByText(/^订单产品(?:\(\d+\))?$/, 1500);
        await waitFor(() => !loading(), 15000);
      }
      const bodyText = clean(document.body?.innerText || "");
      const orderNo = byLabel(/^销售订单编号$/) || (bodyText.match(/\b20\d{6}-\d{6}\b/) || [])[0] || crmOrderNo;
      if (crmOrderNo && orderNo && crmOrderNo !== orderNo && !bodyText.includes(crmOrderNo)) {
        return null;
      }
      const attachmentBlock = Array.from(document.querySelectorAll(".faci-field-display"))
        .find((block) => /合同附件|附件/.test(clean(block.querySelector(".faci-field-display_label")?.innerText)));
      const attachmentLinks = attachmentBlock
        ? Array.from(attachmentBlock.querySelectorAll("a"))
          .map((link) => ({
            title: clean(link.getAttribute("title")),
            text: clean(link.innerText || link.textContent),
            href: clean(link.getAttribute("href")),
          }))
          .filter((item) => item.href || item.title || item.text)
        : [];
      const attachmentNames = attachmentLinks
        .map((item) => item.title || item.text || "")
        .filter((item) => /\.(pdf|png|jpg|jpeg|docx?|xlsx?)$/i.test(item));
      const attachmentFiles = Array.from(new Set(attachmentNames)).join("; ");
      const attachments = attachmentFiles
        ? attachmentFiles.split(";").map((name) => {
          const cleanName = name.trim();
          const named = attachmentLinks.find((item) => (item.title || item.text) === cleanName) || {};
          const download = attachmentLinks.find((item) => item.title === "下载" || /FilesOne|Sign|AuthXC/.test(item.href || "")) || {};
          const previewUrl = named.href ? (named.href.startsWith("//") ? `https:${named.href}` : named.href) : "";
          const downloadUrl = download.href ? (download.href.startsWith("//") ? `https:${download.href}` : download.href) : "";
          return { file_name: cleanName, file_url: downloadUrl || previewUrl, download_url: downloadUrl, preview_url: previewUrl, raw: { source: "dom_detail" } };
        })
        : [];
      const productName = byLabel(/^成交产品$/);
      const productAmount = byLabel(/^产品合计$/).replace(/,/g, "") || byLabel(/^销售订单金额/).replace(/,/g, "");
      const productRows = readOrderProductRows();
      const textProductRows = productRows.length ? [] : readOrderProductRowsFromText();
      const hasAnyDetail = fields.length > 0
        || productRows.length > 0
        || textProductRows.length > 0
        || Boolean(productName)
        || Boolean(byLabel(/^客户名称$/))
        || Boolean(byLabel(/^销售订单金额/));
      if (!hasAnyDetail) {
        return null;
      }
      return {
        crm_order_no: orderNo || crmOrderNo,
        customer_name: byLabel(/^客户名称$/),
        opportunity_name: byLabel(/^商机/),
        order_date: byLabel(/^下单日期$/),
        settlement_method: byLabel(/^订单结算方式$/),
        order_amount: byLabel(/^销售订单金额/).replace(/,/g, ""),
        received_amount: byLabel(/^已回款金额/).replace(/,/g, ""),
        receivable_amount: byLabel(/^待回款金额/).replace(/,/g, ""),
        invoice_amount: byLabel(/^已开票金额/).replace(/,/g, ""),
        product_amount: byLabel(/^产品合计$/).replace(/,/g, ""),
        order_items: productRows.length ? productRows : (textProductRows.length ? textProductRows : (productName ? [{
          product_name: productName,
          quantity: "1",
          unit_price: productAmount,
          line_amount: productAmount,
          source: "dom_detail_field",
        }] : [])),
        sales_user_name: byLabel(/^负责人$/),
        owner_department: byLabel(/^负责人主属部门$/),
        created_by_name: byLabel(/^创建人$/),
        last_modified_by_name: byLabel(/^最后修改人$/),
        owner_profile_id: profileByLabel(/^负责人$/),
        created_by_profile_id: profileByLabel(/^创建人$/),
        last_modified_by_profile_id: profileByLabel(/^最后修改人$/),
        attachment_files: attachmentFiles,
        attachments,
        detail_sync_status: "Synced",
        detail_source: "DomDetailFallback",
        raw_dom_fields: fields,
      };
    },
    { crmOrderNo: row.crm_order_no || "" },
  );
  if (!detail) throw new Error("DOM detail fallback did not match current CRM order detail");
  return { ...row, ...detail, crm_order_id: row.crm_order_id };
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

async function replayDetail(page, detailRequest, row, allowLoginRetry = true) {
  const request = makeDetailRequest(detailRequest, row);
  if (!request.url) throw new Error("Detail request missing url");
  const result = await page.evaluate(
    async ({ request, timeoutMs }) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(request.url, {
          method: request.method,
          credentials: "include",
          headers: {
            "content-type": "application/json;charset=UTF-8",
          },
          body: request.method.toUpperCase() === "GET" ? undefined : request.postData,
          signal: controller.signal,
        });
        return {
          ok: response.ok,
          status: response.status,
          text: await response.text(),
        };
      } finally {
        clearTimeout(timer);
      }
    },
    { request, timeoutMs: REQUEST_TIMEOUT_MS },
  );
  if (!result.ok) throw new Error(`Detail request failed for ${row.crm_order_no || row.crm_order_id}: HTTP ${result.status}`);
  const body = parseJson(result.text, `Detail response ${row.crm_order_no || row.crm_order_id}`);
  if (allowLoginRetry && isLoginExpired(body)) {
    await autoLogin(page);
    return replayDetail(page, detailRequest, row, false);
  }
  assertFxiaokeSuccess(body, `Detail request ${row.crm_order_no || row.crm_order_id}`);
  return body;
}

async function firstProfileEmail(page, ...employeeIds) {
  for (const employeeId of employeeIds) {
    const email = await page.readProfileEmail(employeeId);
    if (email) return email;
  }
  return "";
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

  const page = await connectReplayPage();
  const rows = [];
  const rawPages = [];
  let total = null;

  if (singleRowPath) {
    rows.push(parseJson(await fs.readFile(singleRowPath, "utf8"), singleRowPath));
    total = 1;
  }

  let fetchedPages = 0;
  if (!singleRowPath) {
    try {
      for (let offset = 0; total === null || offset < total; offset += PAGE_SIZE) {
        if (MAX_PAGES > 0 && fetchedPages >= MAX_PAGES) break;
        const body = await replayList(page, listRequest, offset, PAGE_SIZE);
        const dataList = body.Value?.dataList || [];
        rawPages.push({ offset, count: dataList.length, result: body.Result, total: dataList[0]?.total_num ?? null });
        for (const item of dataList) rows.push(normalizeOrder(item));
        if (total === null) total = Number(dataList[0]?.total_num || dataList.length || 0);
        fetchedPages += 1;
        if (dataList.length === 0) break;
      }
    } catch (error) {
      if (!DOM_FALLBACK_ENABLED) throw error;
      const domRows = await readListFromDom(page, PAGE_SIZE);
      rows.push(...domRows);
      total = domRows.length;
      rawPages.push({ offset: 0, count: domRows.length, result: { fallback: "DOM", source_error: String(error.message || error) }, total });
    }
  }

  const deduped = Array.from(new Map(rows.map((row) => [row.crm_order_no || row.crm_order_id, row])).values());
  const detailPages = [];
  if (detailRequest && DETAIL_ENABLED) {
    for (const row of deduped) {
      try {
        const body = await replayDetail(page, detailRequest, row);
        const normalized = normalizeOrderDetail(body, row);
        if (!normalized.sales_user_email) {
          normalized.sales_user_email = await firstProfileEmail(
            page,
            normalized.owner_profile_id,
            normalized.created_by_profile_id,
            normalized.last_modified_by_profile_id,
          );
        }
        Object.assign(row, normalized);
        detailPages.push({ crm_order_id: row.crm_order_id, crm_order_no: row.crm_order_no, status: "Synced" });
      } catch (error) {
        try {
          const normalized = await readDetailFromDom(page, row);
          if (!normalized.sales_user_email) {
            normalized.sales_user_email = await firstProfileEmail(
              page,
              normalized.owner_profile_id,
              normalized.created_by_profile_id,
              normalized.last_modified_by_profile_id,
            );
          }
          normalized.detail_sync_error = String(error.message || error);
          Object.assign(row, normalized);
          detailPages.push({ crm_order_id: row.crm_order_id, crm_order_no: row.crm_order_no, status: "Synced", source: "DOM", source_error: row.detail_sync_error });
        } catch (fallbackError) {
          row.detail_sync_status = "Failed";
          row.detail_sync_error = `${String(error.message || error)}; DOM fallback failed: ${String(fallbackError.message || fallbackError)}`;
          detailPages.push({ crm_order_id: row.crm_order_id, crm_order_no: row.crm_order_no, status: "Failed", error: row.detail_sync_error });
        }
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
  await page.close().catch(() => {});
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: String(error), stack: error.stack }, null, 2));
  process.exitCode = 1;
});
