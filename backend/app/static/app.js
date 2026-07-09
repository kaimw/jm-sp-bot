const $ = (selector) => document.querySelector(selector);
const hiddenPages = new Set(["templates"]);
let initialReviewState = { enabled: true, required_fields: [], rules: [], field_options: [], operator_options: [] };
let v2ReviewRulesState = { rules: [] };
let workflowRulesState = { items: [], editingVersionId: "", editingRules: null, readonly: false };
let productionDepartmentState = { items: [] };
let logisticsDepartmentState = { items: [] };
let workflowChatState = { messages: [], compiledRule: null, validationErrors: [], ready: false, editVersionId: "", editWorkflowName: "" };
let runtimeConfigState = {};
let startupReadinessState = { ready: false, missing: [] };
let inventoryDetailState = null;
let inventoryWarehouseSuggestTimer = null;
let inventoryWarehouseSuggestSeq = 0;
let productSpuSuggestTimer = null;
let productSpuSuggestSeq = 0;
let productSkuSuggestTimer = null;
let productSkuSuggestSeq = 0;
let productCenterState = {
  spu: null,
  sku: null,
  materialLowStock: null,
  materialZeroStock: null,
  finishedLowStock: null,
  finishedZeroStock: null,
};
let dashboardViewState = { period: "year" };
let currentCrmOrderDetailId = "";
let currentCrmOrderDetailFlow = null;
let baiduMapLoadPromise = null;
let baiduDemandMap = null;
let taskQueryState = { q: "", status: "", customer: "", product: "", salesperson: "", order_no: "", delivery: "", page: 1, page_size: 10 };
const tableStates = {
  workflows: { q: "", status: "", page: 1, page_size: 10 },
  departments: { q: "", status: "", page: 1, page_size: 10 },
  logisticsDepartments: { q: "", status: "", page: 1, page_size: 10 },
  logisticsTasks: { q: "", status: "", customer: "", product: "", salesperson: "", order_no: "", page: 1, page_size: 10 },
  mails: { q: "", classification: "", direction: "", from_address: "", page: 1, page_size: 10 },
  outbound: { q: "", status: "", mail_type: "", recipient: "", page: 1, page_size: 10 },
  exceptions: { q: "", status: "Open", severity: "", exception_type: "", page: 1, page_size: 10 },
  jobs: { q: "", status: "", job_type: "", page: 1, page_size: 10 },
  integrationEvents: { q: "", status: "", event_type: "", source_system: "", page: 1, page_size: 10 },
  agentRuns: { q: "", status: "", agent_name: "", task_type: "", page: 1, page_size: 10 },
  modelCalls: { q: "", status: "", task_type: "", page: 1, page_size: 10 },
  attachments: { q: "", parse_status: "", content_type: "", mail_id: "", page: 1, page_size: 10 },
  audit: { q: "", event_type: "", actor: "", related_object_type: "", page: 1, page_size: 10 },
  backups: { q: "", status: "", backup_type: "", page: 1, page_size: 10 },
  reviewRules: { q: "", status: "", page: 1, page_size: 10 },
  productsSpu: { q: "", page: 1, page_size: 10 },
  productsSku: { q: "", crm_semantic: false, page: 1, page_size: 10 },
  productsInventory: { q: "", warehouse_code: "", low_stock_only: "", measure_type: "countable", inventory_scope: "non_finished", threshold: "1", page: 1, page_size: 20 },
  productsFinishedInventory: { q: "", warehouse_code: "", low_stock_only: "", countable_only: "false", measure_type: "", inventory_scope: "finished", threshold: "1", page: 1, page_size: 20 },
  productsPricing: { q: "", page: 1, page_size: 10 },
  productsPromotions: { q: "", page: 1, page_size: 10 },
  crmOrders: { q: "", status: "", customer: "", page: 1, page_size: 20 },
};

async function api(path, options = {}) {
  const { skipAuthRedirect, headers, ...fetchOptions } = options;
  const mergedHeaders = { "Content-Type": "application/json", ...(headers || {}) };
  if (headers && "Content-Type" in headers && headers["Content-Type"] === undefined) {
    delete mergedHeaders["Content-Type"];
  }
  const response = await fetch(path, {
    headers: mergedHeaders,
    credentials: "same-origin",
    ...fetchOptions,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    if (response.status === 401 && !skipAuthRedirect) {
      showLogin();
    }
    // 后端返回的带面包屑的错误（CRM 同步/查询异常）
    if (error.detail && typeof error.detail === "string") {
      try {
        const parsed = JSON.parse(error.detail);
        if (parsed.breadcrumbs && Array.isArray(parsed.breadcrumbs)) {
          const enriched = new Error(error.detail);
          enriched.breadcrumbData = parsed;
          throw enriched;
        }
      } catch (e) {
        if (e.breadcrumbData) throw e;
        // 不是 JSON 面包屑，原样抛出
      }
    }
    // 无面包屑的常规错误：但将 detail 附在 error 上供 notifyError 使用
    const msg = error.detail || response.statusText;
    const enriched = new Error(msg);
    enriched.statusCode = response.status;
    if (error.detail && typeof error.detail === "object" && error.detail.detail) {
      // 有时 FastAPI 会把 detail 再包一层
      enriched.rawDetail = error.detail.detail;
    }
    throw enriched;
  }
  return response.json();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = String(message || "操作已完成");
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

function noticeTrail(parts, message, variant = "info") {
  const node = $("#notice-trail");
  if (!node) return;
  const item = document.createElement("div");
  item.className = `notice-crumb ${variant === "error" ? "is-error" : ""}`;
  item.innerHTML = `
    ${(parts || ["系统"]).map((part) => `<span>${h(part)}</span>`).join("<b>/</b>")}
    <b>/</b>
    <strong>${h(message)}</strong>
  `;
  node.prepend(item);
  while (node.children.length > 4) {
    node.lastElementChild.remove();
  }
  setTimeout(() => item.remove(), 9000);
}

function messageFromError(error) {
  return error?.message || String(error || "操作失败");
}

function notifyError(error, parts = ["系统", "操作异常"]) {
  const message = messageFromError(error);
  // 后端返回的带结构面包屑的错误
  if (error && error.breadcrumbData) {
    const data = error.breadcrumbData;
    notifyErrorWithBreadcrumbs(data, parts);
    return;
  }
  // 传统无面包屑的错误
  noticeTrail(parts, message, "error");
  toast(message);
}

function notifyErrorWithBreadcrumbs(data, topParts) {
  const node = $("#notice-trail");
  if (!node) return;
  const crumbs = data.breadcrumbs || [];
  const failedStep = crumbs.find((c) => c.status === "fail");
  const resolution = data.resolution || "请检查相关配置或联系 IT 运维";
  const item = document.createElement("div");
  item.className = "notice-crumb is-error is-breadcrumb";
  item.innerHTML = `
    <div class="crumb-summary">
      <span class="crumb-prefix">${(topParts || []).map((p) => h(p)).join(" <b>/</b> ")}</span>
    </div>
    <div class="crumb-chain">
      ${crumbs
        .map(
          (c, i) =>
            `<span class="crumb-step is-${c.status}">
              ${c.status === "fail" ? "✕" : c.status === "ok" ? "✓" : "○"}
              ${h(c.label)}
              ${c.error ? `<span class="crumb-step-error">${h(c.error.slice(0, 100))}</span>` : ""}
            </span>`
        )
        .join('<span class="crumb-arrow">→</span>')}
    </div>
    <div class="crumb-resolution">
      <strong>💡 解决建议：</strong>${h(resolution)}
    </div>
  `;
  node.prepend(item);
  while (node.children.length > 4) {
    node.lastElementChild.remove();
  }
  setTimeout(() => item.remove(), 15000);
  toast(`⚠ ${failedStep ? failedStep.label + " 失败" : "操作失败"} · ${resolution.slice(0, 40)}`);
}

async function guardedAction(parts, action) {
  try {
    return await action();
  } catch (error) {
    notifyError(error, parts);
    return null;
  }
}

window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  notifyError(event.reason, ["系统", "未处理异常"]);
});

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function normalizePercent(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.max(0, Math.min(100, Math.round(number)));
}

function splitEmails(value) {
  return String(value || "")
    .split(/[,，;；、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function splitRoutingNames(value) {
  return String(value || "")
    .split(/[,，;；、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 60) return `${Math.round(value)} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分`;
}

function queryFromState(state) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state)) {
    if (value !== "" && value !== null && value !== undefined) params.set(key, value);
  }
  return params.toString();
}

function normalizeListPayload(data, state) {
  if (!Array.isArray(data)) return data;
  return {
    items: data,
    total: data.length,
    page: 1,
    page_size: state.page_size || data.length || 10,
    total_pages: 1,
  };
}

function setSelectOptions(selector, options, allLabel, current = "") {
  const select = $(selector);
  if (!select) return;
  const existingCurrent = current || select.value;
  const optionValues = (options || []).map((option) => String(option));
  select.innerHTML = [
    `<option value="">${h(allLabel)}</option>`,
    ...optionValues.map((option) => `<option value="${h(option)}">${h(option)}</option>`),
  ].join("");
  select.value = optionValues.includes(existingCurrent) || existingCurrent === "" ? existingCurrent : "";
}

function setExceptionStatusOptions(options) {
  const select = $("#exceptions-filter-form [name=status]");
  if (!select) return;
  const current = tableStates.exceptions.status || "Open";
  const optionValues = Array.from(new Set(["Open", ...(options || []).map((option) => String(option))]));
  select.innerHTML = [
    `<option value="__all__">全部状态</option>`,
    ...optionValues.map((option) => `<option value="${h(option)}">${h(option)}</option>`),
  ].join("");
  select.value = optionValues.includes(current) || current === "__all__" ? current : "Open";
}

function renderListPagination(containerSelector, key, data) {
  const node = $(containerSelector);
  if (!node) return;
  const state = tableStates[key];
  const total = data.total || 0;
  const page = data.page || 1;
  const totalPages = data.total_pages || 1;
  state.page = page;
  state.page_size = data.page_size || state.page_size;
  node.innerHTML = `
    <div class="pagination-summary">共 ${h(total)} 条 · 第 ${h(page)} / ${h(totalPages)} 页</div>
    <div class="pagination-controls">
      <button class="button ghost" type="button" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button class="button ghost" type="button" data-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      <label>每页
        <select data-page-size>
          ${[10, 20, 50, 100].map((size) => `<option value="${size}" ${Number(state.page_size) === size ? "selected" : ""}>${size}</option>`).join("")}
        </select>
      </label>
    </div>
  `;
}

function paginateLocalRows(rows, key) {
  const state = tableStates[key];
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / state.page_size));
  state.page = Math.min(state.page, totalPages);
  const start = (state.page - 1) * state.page_size;
  return {
    items: rows.slice(start, start + state.page_size),
    total,
    page: state.page,
    page_size: state.page_size,
    total_pages: totalPages,
  };
}

function appendChatMessage(role, label, message, selector = "#model-chat-log") {
  const log = $(selector);
  if (!log) return;
  log.insertAdjacentHTML(
    "beforeend",
    `<div class="chat-message ${h(role)}"><small>${h(label)}</small><p>${h(message)}</p></div>`
  );
  log.scrollTop = log.scrollHeight;
}

function showLogin() {
  document.body.classList.remove("is-authenticated");
  const screen = $("#login-screen");
  if (screen) screen.hidden = false;
}

function showApp(user) {
  document.body.classList.add("is-authenticated");
  const screen = $("#login-screen");
  if (screen) screen.hidden = true;

  const userInfo = $("#user-info");
  if (userInfo && user) {
    userInfo.textContent = `${user.username} (${user.role_name || user.role})` + (user.department ? ` - ${user.department}` : "");
  }

  const toggleBtn = $("#system-toggle");
  const clearBtn = $("#business-data-clear");
  const isIT = user && (user.role === "admin" || user.role === "it_ops");
  if (toggleBtn) toggleBtn.style.display = isIT ? "" : "none";
  if (clearBtn) clearBtn.style.display = isIT ? "" : "none";
}

async function ensureAuthenticated() {
  const data = await api("/api/auth/me", { skipAuthRedirect: true });
  if (data.authenticated) {
    showApp(data);
    return true;
  }
  showLogin();
  return false;
}

function currentPageName() {
  return (window.location.hash || "#dashboard").slice(1) || "dashboard";
}

function initPageFeature(pageName = currentPageName()) {
  if (!document.body.classList.contains("is-authenticated")) return;
  if (pageName === "orders") initOrdersPage();
  else if (pageName === "master-data") initMasterDataPage();
  else if (pageName === "inventory") initInventoryPage();
}

function copilotContextForPage(pageName = currentPageName()) {
  const activePage = document.querySelector(".page.is-active");
  const title = activePage?.dataset?.title || $("#page-title")?.textContent || "当前页面";
  const subtitle = activePage?.dataset?.subtitle || $("#page-subtitle")?.textContent || "";
  const defaults = {
    title,
    summary: subtitle || "订单中台运行视图",
    focus: ["查看当前列表状态", "处理高优先级待办", "必要时进入运维日志"],
    actions: [
      ["异常台", "#exceptions", "阻断接管"],
      ["运维", "#ops", "队列与留痕"],
      ["CRM 订单", "#crm-orders", "订单流程"],
    ],
  };
  const contexts = {
    dashboard: {
      title: "Agent 概览",
      summary: "CRM 入库、预审、OMS 履约、异常和通知队列的总览。",
      focus: ["中台订单水位", "预审直通率", "OMS 阻断和开放异常"],
      actions: [["异常台", "#exceptions", "处理阻断"], ["CRM 订单", "#crm-orders", "查看流程"], ["运维", "#ops", "查日志"]],
    },
    "crm-orders": {
      title: "CRM 订单",
      summary: "从 CRM 同步来的订单事实源，点击订单可查看完整流程和当前状态。",
      focus: ["详情同步状态", "预审结果", "CRM 变更/撤销风险"],
      actions: [["接入设置", "#integration", "同步配置"], ["异常台", "#exceptions", "处理阻断"], ["发货执行", "#logistics-tasks", "履约视图"]],
    },
    exceptions: {
      title: "异常接管",
      summary: "阻断、死信和高危变更的人工接管入口。",
      focus: ["AI 诊断", "上下文证据", "分派、关闭、重开和 OMS 重放"],
      actions: [["运维", "#ops", "队列留痕"], ["CRM 订单", "#crm-orders", "订单流程"], ["外发", "#outbound", "通知状态"]],
    },
    ops: {
      title: "运维",
      summary: "处理队列、接口日志、Agent 运行和模型调用留痕。",
      focus: ["失败队列", "集成事件", "Agent/模型调用轨迹"],
      actions: [["异常台", "#exceptions", "业务阻断"], ["接入配置", "#integration", "外部系统"], ["外发", "#outbound", "通知队列"]],
    },
    integration: {
      title: "系统接入",
      summary: "CRM、OMS、模型、ERP 和端到端邮箱的运行配置。",
      focus: ["CRM 一期范围", "OMS 真实下推", "模型调用凭证"],
      actions: [["CRM 订单", "#crm-orders", "同步结果"], ["运维", "#ops", "接口日志"], ["工作台", "#dashboard", "总体水位"]],
    },
    products: {
      title: "物料中心",
      summary: "SKU、库存、价格、别名和促销规则支撑订单预审。",
      focus: ["SKU 映射", "库存可用量", "价格和促销分摊"],
      actions: [["CRM 订单", "#crm-orders", "预审验证"], ["异常台", "#exceptions", "主数据阻断"], ["运维", "#ops", "导入日志"]],
    },
  };
  return contexts[pageName] || defaults;
}

function renderCopilotDrawer() {
  const body = $("#copilot-body");
  if (!body) return;
  const context = copilotContextForPage();
  $("#copilot-title").textContent = context.title;
  body.innerHTML = `
    <div class="copilot-context-card">
      <strong>${h(context.title)}</strong>
      <p>${h(context.summary)}</p>
    </div>
    <div class="copilot-context-card">
      <strong>关注点</strong>
      <div class="copilot-action-list">
        ${(context.focus || []).map((item) => `<a href="${h(window.location.hash || "#dashboard")}"><span>${h(item)}</span><small>当前</small></a>`).join("")}
      </div>
    </div>
    <div class="copilot-context-card">
      <strong>动作</strong>
      <div class="copilot-action-list">
        ${(context.actions || []).map(([label, href, hint]) => `<a href="${h(href)}"><span>${h(label)}</span><small>${h(hint)}</small></a>`).join("")}
      </div>
    </div>
  `;
}

function openCopilotDrawer() {
  renderCopilotDrawer();
  $("#copilot-drawer").hidden = false;
}

function closeCopilotDrawer() {
  $("#copilot-drawer").hidden = true;
}

function setActivePage(pageName = currentPageName()) {
  const pages = [...document.querySelectorAll(".page")];
  const visiblePages = pages.filter((page) => !hiddenPages.has(page.dataset.page));
  const target = visiblePages.find((page) => page.dataset.page === pageName) || visiblePages.find((page) => page.dataset.page === "dashboard");
  if (!target) return;
  document.body.classList.toggle("is-settings-mode", target.dataset.settingsSection === "true");
  pages.forEach((page) => page.classList.toggle("is-active", page === target));
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.classList.toggle("active", link.dataset.pageLink === target.dataset.page);
  });
  $("#page-title").textContent = target.dataset.title || "工作台";
  $("#page-subtitle").textContent = target.dataset.subtitle || "";

  if (pageName === "skill-lab" || pageName === "self-maintenance") {
    refreshSkills();
  }
  // 进入物料中心时自动刷新列表
  if (pageName === "products") {
    refreshProductsSku();
  }
  initPageFeature(target.dataset.page);
}

async function refreshDashboard() {
  const [data, health, ticker, v2Summary] = await Promise.all([
    api("/api/dashboard"),
    api("/api/system/health"),
    api("/api/global-exception-ticker"),
    api("/api/v2/order-dashboard"),
  ]);
  const businessTodo =
    Number(data.drafted || 0) +
    Number(data.questioned || 0) +
    Number(data.change_review || 0) +
    Number(data.exceptions_open || 0);
  const statusCounts = v2Summary.status_counts || {};
  const omsAttention = Number(v2Summary.oms_retrying || 0) + Number(v2Summary.oms_blocked || 0);
  const labels = [
    ["中台订单", v2Summary.total_orders || 0, "CRM 审批后进入中台的订单", "normal"],
    ["预审直通率", `${v2Summary.stp_rate || 0}%`, "已通过预审或进入履约链路", Number(v2Summary.stp_rate || 0) >= 90 ? "ok" : "warn"],
    ["OMS 阻断", omsAttention, `重试 ${v2Summary.oms_retrying || 0} / 死信 ${v2Summary.oms_blocked || 0}`, omsAttention ? "danger" : "ok"],
    ["开放异常", data.exceptions_open || 0, `商务待办 ${businessTodo}`, Number(data.exceptions_open || 0) ? "warn" : "ok"],
  ];
  renderDashboardFocus(data, health);
  renderGlobalExceptionTicker(ticker.items || []);
  $("#dashboard-metrics").innerHTML = labels
    .map(([label, value, hint, tone]) => `<div class="metric metric-${h(tone)}"><span>${h(label)}</span><strong>${h(value)}</strong><small>${h(hint)}</small></div>`)
    .join("");
  renderDashboardInsights(data.analytics || {});
  renderAgentDashboardPanels(v2Summary, data, health, ticker.items || [], statusCounts);
}

function renderGlobalExceptionTicker(items) {
  const node = $("#global-exception-ticker");
  if (!node) return;
  const rows = (items || []).slice(0, 6);
  node.hidden = rows.length === 0;
  if (!rows.length) {
    node.innerHTML = "";
    return;
  }
  node.innerHTML = `
    <strong>高优先级异常</strong>
    <div class="global-exception-track">
      ${rows
        .map(
          (item) => `
            <a class="global-exception-item is-${h(item.tone || "warn")}" href="${h(item.href || "#exceptions")}" title="${h(item.message || "")}">
              <span>${h(item.title || item.type || "异常")}</span>
              <small>${h(item.sla_status ? `SLA ${item.sla_status}` : item.message || "")}</small>
            </a>`
        )
        .join("")}
    </div>
  `;
}

function statusTone(value, warnAt = 1, dangerAt = Infinity) {
  const number = Number(value || 0);
  if (number >= dangerAt) return "danger";
  if (number >= warnAt) return "warn";
  return "ok";
}

function renderAgentDashboardPanels(v2Summary, data, health, tickerItems, statusCounts) {
  renderDashboardActivity(data, health, tickerItems);
  renderOrderWaterline(v2Summary, statusCounts);
  renderExceptionDistribution(data, tickerItems);
  renderFulfillmentTrend(v2Summary, statusCounts);
}

function renderDashboardActivity(data, health, tickerItems) {
  const node = $("#dashboard-activity-table");
  if (!node) return;
  const processingCounts = health.queues?.processing?.counts || {};
  const outboundCounts = health.queues?.outbound?.counts || {};
  const rows = [
    [data.generated_at ? formatTime(data.generated_at) : "当前", "CRM 同步 / 订单预审 / 队列事件", `${processingCounts.Pending || 0} 待处理 / ${processingCounts.Failed || 0} 失败`],
    [data.generated_at ? formatTime(data.generated_at) : "当前", "OMS 下推 / 通知发送 / 异常接管", `${outboundCounts.Pending || 0} 待通知 / ${outboundCounts.Failed || 0} 死信`],
    ...(tickerItems || []).slice(0, 2).map((item) => [
      item.created_at ? formatTime(item.created_at) : "最新",
      item.title || item.type || "高优先级异常",
      item.sla_status || item.message || "待处理",
    ]),
  ];
  node.innerHTML = `
    <div><span>时间</span><span>活动</span><span>状态</span></div>
    ${rows
      .map(([time, activity, status]) => `<div><span>${h(time)}</span><span>${h(activity)}</span><strong>${h(status)}</strong></div>`)
      .join("")}
  `;
}

function renderOrderWaterline(v2Summary, statusCounts) {
  const node = $("#dashboard-order-waterline");
  if (!node) return;
  const rows = [
    ["预审阻断", statusCounts.VALIDATION_BLOCKED || 0, "需补字段/主数据/附件"],
    ["预审通过", statusCounts.VALIDATED || 0, "等待发货通知"],
    ["待推 OMS", statusCounts.OMS_PENDING || 0, "已确认待下推"],
    ["履约归档", statusCounts.FULFILLMENT_ARCHIVED || 0, "一期流程完成"],
  ];
  node.innerHTML = `
    <div class="dashboard-card-head">
      <h2>订单水位</h2>
      <a href="#crm-orders" class="button ghost">订单</a>
    </div>
    <strong class="dashboard-placeholder-value">${h(v2Summary.total_orders || 0)}</strong>
    <p>CRM 订单进入中台后的预审、OMS 和归档分布。</p>
    <div class="dashboard-focus-list">
      ${rows.map(([label, value, hint]) => `<a class="dashboard-focus-row is-${Number(value) ? "warn" : "ok"}" href="#crm-orders"><span>${h(label)}</span><strong>${h(value)}</strong><small>${h(hint)}</small></a>`).join("")}
    </div>
  `;
}

function renderExceptionDistribution(data, tickerItems) {
  const node = $("#dashboard-exception-distribution");
  if (!node) return;
  const highRisk = (tickerItems || []).filter((item) => item.type === "exception").length;
  const rows = [
    ["开放异常", data.exceptions_open || 0, "异常台待处理"],
    ["高优先级", highRisk, "Critical/High 或 SLA 风险"],
    ["处理死信", (tickerItems || []).filter((item) => item.type === "processing_dead_letter").length, "队列失败需接管"],
    ["通知死信", (tickerItems || []).filter((item) => item.type === "outbound_dead_letter").length, "邮件发送失败"],
  ];
  node.innerHTML = `
    <div class="dashboard-card-head">
      <h2>异常分布</h2>
      <a href="#exceptions" class="button ghost">异常</a>
    </div>
    <strong class="dashboard-placeholder-value">${h(data.exceptions_open || 0)}</strong>
    <p>商务、履约、系统队列和通知链路的阻断风险。</p>
    <div class="dashboard-focus-list">
      ${rows.map(([label, value, hint]) => `<a class="dashboard-focus-row is-${Number(value) ? "warn" : "ok"}" href="#exceptions"><span>${h(label)}</span><strong>${h(value)}</strong><small>${h(hint)}</small></a>`).join("")}
    </div>
  `;
}

function renderFulfillmentTrend(v2Summary, statusCounts) {
  const node = $("#dashboard-fulfillment-trend");
  if (!node) return;
  const fulfillmentActive =
    Number(statusCounts.OMS_PENDING || 0) +
    Number(statusCounts.OMS_RETRYING || 0) +
    Number(statusCounts.OMS_ACCEPTED || 0) +
    Number(statusCounts.PICKING || 0) +
    Number(statusCounts.SHIPPED || 0);
  const rows = [
    ["OMS 待推送", statusCounts.OMS_PENDING || 0, "待创建下游单"],
    ["OMS 重试中", statusCounts.OMS_RETRYING || 0, "自动补偿中"],
    ["OMS 已接收", statusCounts.OMS_ACCEPTED || 0, "等待执行状态"],
    ["拣货/已发货", Number(statusCounts.PICKING || 0) + Number(statusCounts.SHIPPED || 0), "仓库执行中"],
  ];
  node.innerHTML = `
    <div class="dashboard-card-head">
      <h2>履约趋势</h2>
      <a href="#logistics-tasks" class="button ghost">履约</a>
    </div>
    <strong class="dashboard-placeholder-value">${h(fulfillmentActive)}</strong>
    <p>已进入发货通知、OMS 下推、仓库执行的订单水位。</p>
    <div class="dashboard-focus-list">
      ${rows.map(([label, value, hint]) => `<a class="dashboard-focus-row is-${Number(value) ? "warn" : "ok"}" href="#logistics-tasks"><span>${h(label)}</span><strong>${h(value)}</strong><small>${h(hint)}</small></a>`).join("")}
    </div>
  `;
}

function renderDashboardFocus(data, health) {
  const business = $("#dashboard-business-panel");
  const ops = $("#dashboard-ops-panel");
  if (!business || !ops) return;
  const readiness = health.readiness || { ready: false, missing: [] };
  const processing = health.queues?.processing || {};
  const outbound = health.queues?.outbound || {};
  const processingCounts = processing.counts || {};
  const outboundCounts = outbound.counts || {};
  const configRiskCount = (readiness.ready ? 0 : Math.max(1, (readiness.missing || []).length)) + (health.bot_enabled ? 0 : 1);
  const businessRows = [
    ["草稿待确认", data.drafted, "等待初审或补充", Number(data.drafted || 0) ? "warn" : "ok", "#tasks"],
    ["生产疑问", data.questioned, "生产侧待答疑", Number(data.questioned || 0) ? "warn" : "ok", "#tasks"],
    ["变更/取消", data.change_review, "待商务复核", Number(data.change_review || 0) ? "warn" : "ok", "#crm-orders"],
    ["异常接管", data.exceptions_open, "阻断项需人工处理", Number(data.exceptions_open || 0) ? "danger" : "ok", "#exceptions"],
  ];
  const opsRows = [
    ["启动就绪", readiness.ready ? "已就绪" : "未就绪", readiness.ready ? "配置完整" : `缺少 ${(readiness.missing || []).length || 0} 项配置`, readiness.ready ? "ok" : "warn", "#integration"],
    ["机器人", health.bot_enabled ? "运行中" : "已停用", health.bot_enabled ? "自动流程可执行" : "不会自动消费邮件和队列", health.bot_enabled ? "ok" : "warn", "#integration"],
    ["入库队列", `Pending ${processingCounts.Pending || 0}`, `Failed ${processingCounts.Failed || 0}`, statusTone(Number(processingCounts.Failed || 0), 1, 1), "#ops"],
    ["外发队列", `Pending ${outboundCounts.Pending || 0}`, `Failed ${outboundCounts.Failed || 0}`, statusTone(Number(outboundCounts.Failed || 0), 1, 1), "#outbound"],
  ];
  const businessTotal = businessRows.reduce((sum, row) => sum + Number(row[1] || 0), 0);
  const opsTotal =
    configRiskCount +
    Number(processingCounts.Pending || 0) +
    Number(processingCounts.Failed || 0) +
    Number(outboundCounts.Pending || 0) +
    Number(outboundCounts.Failed || 0);
  const renderRows = (rows) =>
    rows
      .map(
        ([label, value, hint, tone, href]) => `
          <a class="dashboard-focus-row is-${h(tone)}" href="${h(href)}">
            <span>${h(label)}</span>
            <strong>${h(value ?? 0)}</strong>
            <small>${h(hint)}</small>
          </a>`
      )
      .join("");
  business.innerHTML = `
    <div class="dashboard-focus-head">
      <div>
        <small>商务关注</small>
        <h2>需要人工判断的订单事项</h2>
      </div>
      <strong>${h(businessTotal)}</strong>
    </div>
    <div class="dashboard-focus-list">${renderRows(businessRows)}</div>
    <div class="dashboard-focus-actions">
      <a class="button" href="${businessTotal ? "#exceptions" : "#crm-orders"}">${businessTotal ? "处理商务待办" : "查看 CRM 订单"}</a>
      <a class="button ghost" href="#tasks">生产任务</a>
    </div>
  `;
  ops.innerHTML = `
    <div class="dashboard-focus-head">
      <div>
        <small>运维关注</small>
        <h2>影响自动化运行的配置与队列</h2>
      </div>
      <strong>${h(opsTotal)}</strong>
    </div>
    <div class="dashboard-focus-list">${renderRows(opsRows)}</div>
    <div class="dashboard-focus-actions">
      <a class="button" href="${opsTotal ? "#integration" : "#ops"}">${opsTotal ? "检查系统接入" : "查看队列审计"}</a>
      <a class="button ghost" href="#outbound">通知队列</a>
    </div>
  `;
}

function pct(value, total) {
  const number = Number(value || 0);
  const base = Number(total || 0);
  if (!base) return 0;
  return Math.max(0, Math.min(100, Math.round((number / base) * 100)));
}

function topRows(rows, valueKey, labelKey, limit = 6) {
  return (rows || []).slice(0, limit).map((row) => ({
    label: row[labelKey] || "未识别",
    value: Number(row[valueKey] || 0),
    secondary: Number(row.confirmed_total || 0),
  }));
}

function renderPeriodTabs(periods) {
  const labels = { month: "月度", year: "年度" };
  return Object.keys(labels)
    .filter((key) => periods[key])
    .map(
      (key) => `
        <button type="button" class="${dashboardViewState.period === key ? "active" : ""}" data-dashboard-period="${h(key)}">
          ${h(labels[key])}
        </button>`
    )
    .join("");
}

function renderTrendChart(trend) {
  const rows = trend || [];
  const width = 360;
  const height = 190;
  const pad = { top: 12, right: 34, bottom: 34, left: 8 };
  const chartWidth = width - pad.left - pad.right;
  const chartHeight = height - pad.top - pad.bottom;
  const maxValue = Math.max(1, ...rows.map((row) => Number(row.total || 0)));
  const x = (index) => pad.left + (rows.length <= 1 ? chartWidth / 2 : (index / (rows.length - 1)) * chartWidth);
  const y = (value) => pad.top + chartHeight - (Number(value || 0) / maxValue) * chartHeight;
  const areaPath = (values) => {
    if (!rows.length) return "";
    const topLine = values.map((value, index) => `${index === 0 ? "M" : "L"} ${x(index).toFixed(1)} ${y(value).toFixed(1)}`).join(" ");
    const baseY = pad.top + chartHeight;
    return `${topLine} L ${x(rows.length - 1).toFixed(1)} ${baseY.toFixed(1)} L ${x(0).toFixed(1)} ${baseY.toFixed(1)} Z`;
  };
  const totalValues = rows.map((row) => Number(row.total || 0));
  const confirmedValues = rows.map((row) => Number(row.confirmed_total || 0));
  const labelStep = Math.max(1, Math.ceil(rows.length / 6));
  const axisLabels = rows
    .map((row, index) => ({ row, index }))
    .filter((item) => item.index === 0 || item.index === rows.length - 1 || item.index % labelStep === 0);
  const gridValues = [0.25, 0.5, 0.75, 1].map((ratio) => Math.round(maxValue * ratio));
  return `
    <div class="trend-chart area-trend-chart">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="任务趋势面积图" preserveAspectRatio="none">
        ${gridValues
          .map(
            (value) => `
              <line class="trend-grid-line" x1="${pad.left}" x2="${width - pad.right}" y1="${y(value).toFixed(1)}" y2="${y(value).toFixed(1)}"></line>
              <text class="trend-axis-y" x="${width - 4}" y="${(y(value) + 3).toFixed(1)}">${h(value)}</text>`
          )
          .join("")}
        <path class="trend-area trend-area-total" d="${areaPath(totalValues)}"></path>
        <path class="trend-area trend-area-confirmed" d="${areaPath(confirmedValues)}"></path>
        ${axisLabels
          .map(
            (item, index) => `
              <text class="trend-axis-x" x="${x(item.index).toFixed(1)}" y="${height - 18}" text-anchor="${index === 0 ? "start" : index === axisLabels.length - 1 ? "end" : "middle"}">${h(item.row.label)}</text>`
          )
          .join("")}
      </svg>
      <div class="trend-legend">
        <span><i class="legend-confirmed"></i>已确认</span>
        <span><i class="legend-total"></i>新需求</span>
      </div>
    </div>`;
}

function renderStatusDonut(rows) {
  const total = (rows || []).reduce((sum, row) => sum + Number(row.count || 0), 0);
  const palette = ["#0f766e", "#2563eb", "#b45309", "#b42318", "#64748b", "#7c3aed"];
  let cursor = 0;
  const segments = (rows || []).map((row, index) => {
    const value = pct(row.count, total);
    const start = cursor;
    cursor += value;
    return `${palette[index % palette.length]} ${start}% ${cursor}%`;
  });
  return `
    <div class="donut-wrap">
      <div class="donut" style="background: conic-gradient(${segments.join(", ") || "#e2e8f0 0% 100%"});">
        <span>${h(total)}</span>
        <small>总单数</small>
      </div>
      <div class="chart-legend">
        ${((rows || []).length ? rows : [{ label: "暂无数据", count: 0 }])
          .map(
            (row, index) => `
              <span><i style="background:${palette[index % palette.length]}"></i>${h(row.label)} ${h(row.count)}</span>`
          )
          .join("")}
      </div>
    </div>`;
}

function renderRankingChart(rows, emptyLabel) {
  const maxValue = Math.max(1, ...rows.map((row) => row.value));
  const list = rows.length ? rows : [{ label: emptyLabel, value: 0, secondary: 0 }];
  return `
    <div class="ranking-chart">
      ${list
        .map(
          (row) => `
            <div class="ranking-row">
              <span>${h(row.label)}</span>
              <div><i style="width:${pct(row.value, maxValue)}%"></i></div>
              <strong>${h(row.value)}</strong>
              <small>确认 ${h(row.secondary)}</small>
            </div>`
        )
        .join("")}
    </div>`;
}

function chartPanel(title, subtitle, body, extraClass = "") {
  return `
    <section class="floating-chart-card ${extraClass}">
      <div class="chart-head">
        <h3>${h(title)}</h3>
        <span>${h(subtitle)}</span>
      </div>
      ${body}
    </section>`;
}

function renderDemandMap(points, current, periods) {
  const rows = (points || []).slice(0, 12);
  const trend = current.trend || [];
  const salesRows = topRows(current.sales_top10 || [], "demand_total", "salesperson", 7);
  const productRows = topRows(current.product_top10 || [], "total", "product", 7);
  return `
    <div class="geo-dashboard">
      <div id="baidu-demand-map" class="baidu-map" aria-label="物料需求地理分布"></div>
      <div id="baidu-map-empty" class="baidu-map-empty" ${rows.length ? "hidden" : ""}>当前周期暂无可识别的需求地。</div>
      <div class="map-period-control segmented-control" role="tablist" aria-label="统计周期">
        ${renderPeriodTabs(periods)}
      </div>
      <div class="map-overlay map-overlay-left">
        ${chartPanel("状态分布", "环形图", renderStatusDonut(current.status_distribution || []), "floating-status-card")}
        ${chartPanel("任务趋势", "面积图：新需求 / 已确认", renderTrendChart(trend), "floating-trend-card")}
      </div>
      <div class="map-overlay map-overlay-right">
        ${chartPanel("人员排行", "销售需求量 Top 7", renderRankingChart(salesRows, "暂无人员数据"), "floating-ranking-card")}
        ${chartPanel("物料排行", "物料需求量 Top 7", renderRankingChart(productRows, "暂无物料数据"), "floating-ranking-card")}
      </div>
    </div>`;
}

function loadBaiduMap(ak) {
  if (window.BMapGL?.Map && window.BMapGL?.Point) return Promise.resolve(window.BMapGL);
  if (!ak) return Promise.reject(new Error("未配置百度地图 AK"));
  if (!baiduMapLoadPromise) {
    const callbackName = `initBaiduMap_${Date.now()}`;
    baiduMapLoadPromise = new Promise((resolve, reject) => {
      const cleanup = () => {
        window.clearTimeout(timer);
        delete window[callbackName];
      };
      const timer = window.setTimeout(() => {
        cleanup();
        baiduMapLoadPromise = null;
        reject(new Error("百度地图 API 加载超时，请检查 AK、域名白名单或网络访问。"));
      }, 10000);
      window[callbackName] = () => {
        if (window.BMapGL?.Map && window.BMapGL?.Point) {
          cleanup();
          resolve(window.BMapGL);
          return;
        }
        cleanup();
        baiduMapLoadPromise = null;
        reject(new Error("百度地图 API 未完成初始化。"));
      };
      const script = document.createElement("script");
      script.src = `https://api.map.baidu.com/api?v=1.0&type=webgl&ak=${encodeURIComponent(ak)}&callback=${callbackName}`;
      script.async = true;
      script.onerror = () => {
        cleanup();
        baiduMapLoadPromise = null;
        reject(new Error("百度地图 API 加载失败"));
      };
      document.head.appendChild(script);
    });
  }
  return baiduMapLoadPromise;
}

function resolveBaiduPoint(BMapGL, row) {
  return new Promise((resolve) => {
    const fallback = new BMapGL.Point(Number(row.lng), Number(row.lat));
    let settled = false;
    const finish = (point) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      resolve(point || fallback);
    };
    const timer = window.setTimeout(() => finish(fallback), 1500);
    if (!row.city || row.city === "海外") {
      finish(fallback);
      return;
    }
    try {
      const geocoder = new BMapGL.Geocoder();
      geocoder.getPoint(row.city, (point) => finish(point), row.province || row.city);
    } catch {
      finish(fallback);
    }
  });
}

async function renderBaiduDemandMap(points, mapConfig) {
  const container = $("#baidu-demand-map");
  const empty = $("#baidu-map-empty");
  if (!container) return;
  const rows = (points || []).filter((point) => Number.isFinite(Number(point.lng)) && Number.isFinite(Number(point.lat)));
  if (!mapConfig?.ak) {
    empty.hidden = false;
    empty.textContent = "请配置 baidu_map_ak 后加载百度地图。";
    return;
  }
  try {
    const BMapGL = await loadBaiduMap(mapConfig.ak);
    if (!document.body.contains(container)) return;
    baiduDemandMap = new BMapGL.Map(container);
    const center = rows.length
      ? new BMapGL.Point(Number(rows[0].lng), Number(rows[0].lat))
      : new BMapGL.Point(104.1954, 35.8617);
    baiduDemandMap.centerAndZoom(center, rows.length > 1 ? 5 : 8);
    baiduDemandMap.enableScrollWheelZoom(true);
    const resolvedRows = await Promise.all(
      rows.map(async (row) => ({
        row,
        point: await resolveBaiduPoint(BMapGL, row),
      }))
    );
    resolvedRows.forEach(({ row, point }) => {
      const label = new BMapGL.Label(
        `<span class="baidu-pin-count" data-count="${h(row.demand_total || 0)}"></span><span class="baidu-pin-name">${h(row.city || "")}</span>`,
        { position: point, offset: new BMapGL.Size(-18, -42) }
      );
      label.setStyle({
        border: "0",
        padding: "0",
        backgroundColor: "transparent",
        color: "inherit",
      });
      baiduDemandMap.addOverlay(label);
    });
    if (resolvedRows.length > 1) {
      const viewport = resolvedRows.map((item) => item.point);
      baiduDemandMap.setViewport(viewport, { margins: [80, 360, 80, 360] });
    }
    if (empty) empty.hidden = rows.length > 0;
  } catch (error) {
    if (empty) {
      empty.hidden = false;
      empty.textContent = messageFromError(error);
    }
  }
}

function renderDashboardInsights(analytics) {
  const node = $("#dashboard-insights");
  if (!node) return;
  const periods = analytics.periods || {};
  if (!periods[dashboardViewState.period]) {
    dashboardViewState.period = analytics.default_period || Object.keys(periods)[0] || "day";
  }
  const current = periods[dashboardViewState.period] || {};
  const stats = current.task_stats || {};
  const locationRows = current.location_points || [];
  node.innerHTML = `
    <div class="chart-grid">
      <section class="chart-card chart-card-map chart-card-wide">
        ${renderDemandMap(locationRows, current, periods)}
      </section>
    </div>
  `;
  renderBaiduDemandMap(locationRows, analytics.map || {});
}

async function refreshDepartments() {
  const data = normalizeListPayload(await api(`/api/departments?${queryFromState(tableStates.departments)}`), tableStates.departments);
  const rows = data.items || [];
  productionDepartmentState.items = rows;
  setSelectOptions("#departments-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.departments.status);
  $("#departments-list").innerHTML = rows
    .map(
      (row) => `
        <div class="row task-row">
          <div><strong>${h(row.department_name)}</strong><br /><small>${h(row.department_code)}</small></div>
          <div><small>主送</small><br />${h(row.mail_to.join(", ") || "未配置")}</div>
          <div><small>抄送</small><br />${h(row.mail_cc.join(", ") || "无")}</div>
          <div>
            <small>${h(row.status)}</small>
            <div class="actions row-actions">
              <button class="button ghost" type="button" data-action="edit-department" data-id="${h(row.id)}">编辑</button>
              <button class="button warn" type="button" data-action="delete-department" data-id="${h(row.id)}">删除</button>
            </div>
          </div>
        </div>`
    )
    .join("");
  if (!rows.length) $("#departments-list").innerHTML = `<div class="row"><div>暂无生产邮箱</div></div>`;
  renderListPagination("#departments-pagination", "departments", data);
}

async function refreshLogisticsDepartments() {
  const data = normalizeListPayload(await api(`/api/logistics-departments?${queryFromState(tableStates.logisticsDepartments)}`), tableStates.logisticsDepartments);
  const rows = data.items || [];
  logisticsDepartmentState.items = rows;
  setSelectOptions("#logistics-departments-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.logisticsDepartments.status);
  $("#logistics-departments-list").innerHTML = rows
    .map(
      (row) => `
        <div class="row task-row">
          <div><strong>${h(row.department_name)}</strong><br /><small>${h(row.department_code)}</small></div>
          <div><small>主送</small><br />${h(row.mail_to.join(", ") || "未配置")}</div>
          <div><small>抄送</small><br />${h(row.mail_cc.join(", ") || "无")}</div>
          <div>
            <small>${h(row.status)}</small>
            <div class="actions row-actions">
              <button class="button ghost" type="button" data-action="edit-logistics-department" data-id="${h(row.id)}">编辑</button>
              <button class="button warn" type="button" data-action="delete-logistics-department" data-id="${h(row.id)}">删除</button>
            </div>
          </div>
        </div>`
    )
    .join("");
  if (!rows.length) $("#logistics-departments-list").innerHTML = `<div class="row"><div>暂无物流邮箱</div></div>`;
  renderListPagination("#logistics-departments-pagination", "logisticsDepartments", data);
}

async function refreshTasks() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(taskQueryState)) {
    if (value !== "" && value !== null && value !== undefined) params.set(key, value);
  }
  const data = await api(`/api/tasks?${params.toString()}`);
  const rows = data.items || [];
  taskQueryState.page = data.page || 1;
  taskQueryState.page_size = data.page_size || taskQueryState.page_size;
  updateTaskStatusOptions(data.status_options || []);
  $("#tasks").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row task-row">
          <div>
            <strong>${h(row.task_no)}</strong><br />
            <small>${h(row.customer_name || "未识别客户")} · ${h(row.salesperson_email || "未知销售")}</small><br />
            <small>${h(row.external_order_no || "无订单号")}</small>
          </div>
          <div>${h(row.product_summary || "未识别物料")}<br /><small>${h(row.quantity_text || "")} ${h(row.expected_delivery_date || "")}</small></div>
          <div><small>${h(row.status)}</small></div>
          <div><small>创建时间</small><br />${h(formatTime(row.created_at))}</div>
          <div class="actions">
            <button class="button" data-action="workflow" data-id="${row.id}">查看工作流</button>
            <button class="button ghost" data-action="manual-close-task" data-id="${row.id}" ${row.status === "Closed" ? "disabled" : ""}>手动关闭</button>
          </div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无任务</div></div>`;
  renderTaskPagination(data);
}

function renderRelatedTaskLink(row) {
  if (!row || !row.related_task_id) return "未关联";
  const label = row.related_task_no || row.related_task_id;
  const action = row.related_task_type === "logistics" ? "jump-logistics-task" : "jump-task";
  return `<button class="link-button" type="button" data-action="${action}" data-task-id="${h(row.related_task_id)}" data-task-no="${h(row.related_task_no || "")}">${h(label)}</button>`;
}

async function refreshLogisticsTasks() {
  const data = normalizeListPayload(await api(`/api/logistics-tasks?${queryFromState(tableStates.logisticsTasks)}`), tableStates.logisticsTasks);
  const rows = data.items || [];
  setSelectOptions("#logistics-tasks-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.logisticsTasks.status);
  $("#logistics-tasks-list").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row task-row">
          <div>
            <strong>${h(row.task_no)}</strong><br />
            <small>${h(row.customer_name || "未识别客户")} · ${h(row.salesperson_email || "未知销售")}</small><br />
            <small>${h(row.external_order_no || "无订单号")}</small>
          </div>
          <div>${h(row.product_summary || "未识别物料")}<br /><small>${h(row.quantity_text || "")} ${h(row.expected_delivery_date || "")}</small></div>
          <div><small>${h(row.status)}</small><br /><small>${h(row.closed_reason || "")}</small></div>
          <div><small>物流邮箱</small><br />${h((row.target_mail_to || []).join(", ") || "未配置")}</div>
          <div>
            ${row.production_task_id ? `<small>关联生产</small><br />${h(row.production_task_id)}` : ""}
            <div class="actions row-actions">
              <button class="button" data-action="workflow" data-id="${h(row.id)}">查看工作流</button>
              <button class="button ghost" data-action="manual-close-logistics-task" data-id="${h(row.id)}" ${row.status === "Closed" ? "disabled" : ""}>手动关闭</button>
            </div>
          </div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无物流任务</div></div>`;
  renderListPagination("#logistics-tasks-pagination", "logisticsTasks", data);
}

function updateTaskStatusOptions(options) {
  const select = $("#task-filter-form [name=status]");
  if (!select) return;
  const current = select.value;
  select.innerHTML = [
    `<option value="">全部状态</option>`,
    ...options.map((status) => `<option value="${h(status)}">${h(status)}</option>`),
  ].join("");
  select.value = options.includes(current) ? current : "";
}

function renderTaskPagination(data) {
  const total = data.total || 0;
  const page = data.page || 1;
  const totalPages = data.total_pages || 1;
  $("#task-pagination").innerHTML = `
    <div class="pagination-summary">共 ${h(total)} 条 · 第 ${h(page)} / ${h(totalPages)} 页</div>
    <div class="pagination-controls">
      <button class="button ghost" type="button" data-task-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button class="button ghost" type="button" data-task-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      <label>每页
        <select id="task-page-size">
          ${[10, 20, 50, 100].map((size) => `<option value="${size}" ${Number(data.page_size) === size ? "selected" : ""}>${size}</option>`).join("")}
        </select>
      </label>
    </div>
  `;
}

async function jumpToTask(taskId, taskNo = "") {
  const query = String(taskNo || taskId || "").trim();
  if (!query) return;
  taskQueryState = {
    ...taskQueryState,
    q: query,
    status: "",
    customer: "",
    product: "",
    salesperson: "",
    order_no: "",
    delivery: "",
    page: 1,
  };
  const form = $("#task-filter-form");
  if (form) {
    form.reset();
    form.querySelector("[name=q]").value = query;
  }
  window.location.hash = "tasks";
  setActivePage("tasks");
  await refreshTasks();
  toast(`已定位任务：${taskNo || taskId}`);
}

async function jumpToLogisticsTask(taskId, taskNo = "") {
  const query = String(taskNo || taskId || "").trim();
  if (!query) return;
  tableStates.logisticsTasks = {
    ...tableStates.logisticsTasks,
    q: query,
    status: "",
    customer: "",
    product: "",
    salesperson: "",
    order_no: "",
    page: 1,
  };
  const form = $("#logistics-tasks-filter-form");
  if (form) {
    form.reset();
    form.querySelector("[name=q]").value = query;
  }
  window.location.hash = "logistics-tasks";
  setActivePage("logistics-tasks");
  await refreshLogisticsTasks();
  toast(`已定位物流任务：${taskNo || taskId}`);
}

function mailStatusClass(status) {
  return {
    Sent: "status-sent",
    Pending: "status-pending",
    Failed: "status-failed",
    Cancelled: "status-cancelled",
    Running: "status-running",
  }[status] || "status-muted";
}

function renderPendingDiagnosis(row) {
  const diagnosis = row.pending_diagnosis;
  if (!diagnosis) return "";
  const details = Array.isArray(diagnosis.details) ? diagnosis.details : [];
  return `
    <div class="pending-diagnosis pending-${h(diagnosis.severity || "waiting")}">
      <strong>${h(diagnosis.reason || "等待处理")}</strong>
      <small>已等待 ${h(formatDuration(diagnosis.pending_age_seconds))}${diagnosis.queue_position ? ` · 队列第 ${h(diagnosis.queue_position)} 位` : ""}</small>
      ${details.length ? `<ul>${details.map((item) => `<li>${h(item)}</li>`).join("")}</ul>` : ""}
    </div>
  `;
}

async function refreshOutbound() {
  const [listPayload, diagnostics] = await Promise.all([
    api(`/api/outbound-mails?${queryFromState(tableStates.outbound)}`),
    api("/api/outbound-mails/diagnostics"),
  ]);
  const data = normalizeListPayload(listPayload, tableStates.outbound);
  const rows = data.items || [];
  renderOutboundDiagnostics(diagnostics);
  setSelectOptions("#outbound-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.outbound.status);
  setSelectOptions("#outbound-filter-form [name=mail_type]", data.mail_type_options || [], "全部类型", tableStates.outbound.mail_type);
  $("#outbound-list").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row outbound-row clickable-row" data-outbound-id="${h(row.id)}" role="button" tabindex="0" title="查看外发邮件详情">
          <div><strong>${h(row.subject)}</strong><br /><small>${h(row.mail_type)}</small><br /><small>任务：${renderRelatedTaskLink(row)}</small></div>
          <div><small>主送</small><br />${h(row.to.join(", ") || "无")}</div>
          <div><small>抄送</small><br />${h(row.cc.join(", ") || "无")}</div>
          <div><small>发送时间</small><br />${h(row.sent_at ? formatTime(row.sent_at) : "未发送")}</div>
          <div>
            <small class="status-text ${mailStatusClass(row.status)}">${h(row.status)}</small>
            ${renderPendingDiagnosis(row)}
            ${
              row.status === "Failed"
                ? `<div class="actions row-actions"><button class="button ghost" data-action="retry-outbound" data-id="${h(row.id)}">重试</button></div>`
                : ""
            }
          </div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无外发任务</div></div>`;
  renderListPagination("#outbound-pagination", "outbound", data);
}

function renderOutboundDiagnostics(data) {
  const node = $("#outbound-diagnostics");
  if (!node) return;
  const status = data.status_counts || {};
  const failedTypes = data.failed_by_type || [];
  const recent = data.recent_failures || [];
  const deadLetters = data.dead_letters || [];
  const alerts = data.alerts || [];
  node.innerHTML = `
    ${
      alerts.length
        ? `<div class="alert-strip">${alerts.map((alert) => `<span>${h(alert.message)}</span>`).join("")}</div>`
        : ""
    }
    <div class="diagnostics-grid">
      <div class="diagnostic-card ${Number(status.Failed || 0) ? "is-warn" : "is-ok"}">
        <small>失败/死信</small>
        <strong>${h(status.Failed || 0)}</strong>
        <span>Pending ${h(status.Pending || 0)} / Sent ${h(status.Sent || 0)} / Cancelled ${h(status.Cancelled || 0)}</span>
      </div>
      <div class="diagnostic-card">
        <small>近 ${h(data.window_hours || 24)} 小时失败类型</small>
        <strong>${failedTypes.length ? h(failedTypes[0].mail_type) : "无"}</strong>
        <span>${failedTypes.length ? failedTypes.map((item) => `${h(item.mail_type)} ${h(item.count)}`).join("，") : "没有 SMTP 失败记录"}</span>
      </div>
      <div class="diagnostic-card ${recent.length ? "is-warn" : "is-ok"}">
        <small>最近失败</small>
        <strong>${recent.length ? h(recent[0].error || "发送失败") : "无"}</strong>
        <span>${recent.length ? h(recent[0].subject || recent[0].outbound_job_id || "") : "当前窗口内无异常"}</span>
      </div>
    </div>
    ${
      deadLetters.length
        ? `<div class="dead-letter-list">
            ${deadLetters
              .slice(0, 5)
              .map(
                (job) => `
                  <div>
                    <strong>${h(job.subject)}</strong>
                    <small>${h(job.mail_type)} · ${h(formatTime(job.created_at))} · ${h((job.to || []).join(", ") || "无收件人")}</small>
                  </div>`
              )
              .join("")}
          </div>`
        : ""
    }
  `;
}
async function refreshSkills() {
  const listNode = $("#skill-list");
  if (!listNode) return;
  
  await guardedAction(["技能实验室", "加载列表"], async () => {
    const skills = await api("/api/skills/list");
    listNode.innerHTML = skills.map((skill) => {
      const active = skill.active !== false && skill.enabled !== false;
      const statusLabel = skill.status_label || (active ? "已启用" : "已停用");
      const sourceLabel = skill.source === "dynamic" ? "动态技能" : "内置技能";
      return `
        <div class="row skill-row ${active ? "enabled-row" : "disabled-row"}">
          <div class="skill-info">
            <div class="skill-title-line">
              <strong>${h(skill.name)}</strong>
              <span class="skill-status ${active ? "is-enabled" : "is-disabled"}">${h(statusLabel)}</span>
              <span class="skill-source">${h(sourceLabel)}</span>
            </div>
            <p><small>${h(skill.description || "无描述")}</small></p>
          </div>
          <div class="actions">
            ${
              skill.toggleable
                ? `<button class="button ghost" data-action="toggle-skill" data-id="${h(skill.name)}" data-active="${active}">${active ? "停用" : "启用"}</button>`
                : ""
            }
            ${
              skill.deletable
                ? `<button class="icon-button danger" data-action="delete-skill" data-id="${h(skill.name)}" title="删除">🗑️</button>`
                : ""
            }
          </div>
        </div>
      `;
    }).join("") || '<div class="row">暂无可用技能</div>';
  });
}

$("#skill-list")?.addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  
  const skillName = target.dataset.id;
  const action = target.dataset.action;

  if (action === "toggle-skill") {
    const currentActive = target.dataset.active === "true";
    await guardedAction(["技能实验室", currentActive ? "停用技能" : "启用技能"], async () => {
      await api(`/api/skills/${skillName}/toggle?active=${!currentActive}`, { method: "POST" });
      toast(currentActive ? "技能已停用" : "技能已启用");
      refreshSkills();
    });
  } else if (action === "delete-skill") {
    if (!confirm(`确定要删除技能 ${skillName} 吗？此操作不可撤销。`)) return;
    await guardedAction(["技能实验室", "删除技能"], async () => {
      await api(`/api/skills/${skillName}`, { method: "DELETE" });
      toast("技能已删除");
      refreshSkills();
    });
  }
});


$("#skill-generate-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const requirement = form.requirement.value.trim();
  if (!requirement) return;

  const btn = form.querySelector("button");
  const resultPanel = $("#skill-gen-result");
  
  btn.disabled = true;
  btn.textContent = "正在进化中...";
  resultPanel.hidden = true;

  await guardedAction(["技能实验室", "进化新技能"], async () => {
    const res = await api("/api/skills/generate", {
      method: "POST",
      body: JSON.stringify({ requirement })
    });
    
    if (res.success) {
      toast("进化成功！新技能已加载。");
      form.requirement.value = "";
      $("#gen-skill-name").textContent = res.skill_name;
      $("#gen-skill-code").textContent = res.code_preview || "";
      resultPanel.hidden = false;
      refreshSkills();
    }
  });

  btn.disabled = false;
  btn.textContent = "生成并加载";
});

function fillForm(formSelector, values) {
  const form = $(formSelector);
  if (!form) return;
  for (const [key, value] of Object.entries(values)) {
    const input = form.elements.namedItem(key);
    if (!input || value === "***") continue;
    if (input.type === "checkbox") {
      input.checked = ["1", "true", "yes", "on"].includes(String(value).toLowerCase());
    } else {
      input.value = value ?? "";
    }
  }
}

function configEnabled(value, fallback = false) {
  if (value === undefined || value === null || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(String(value).toLowerCase());
}

function renderSystemToggle() {
  const button = $("#system-toggle");
  const runtimeStatusNode = $("#runtime-bot-enabled-status");
  const enabled = configEnabled(runtimeConfigState.bot_enabled, true);
  if (button) {
    button.textContent = enabled ? "暂停机器人" : "启动机器人";
    button.title = enabled
      ? "暂停机器人邮箱监听、同步和自动发送"
      : (startupReadinessState.ready ? "启动机器人邮箱监听、同步和自动发送" : `启动前需补齐：${(startupReadinessState.missing || []).join("、")}`);
    button.classList.toggle("is-paused", !enabled);
  }
  if (runtimeStatusNode) {
    runtimeStatusNode.textContent = enabled ? "已开启（随服务启动自动开启，可在顶部暂停）" : "已暂停（可在顶部启动）";
  }
}

function reviewRuleId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function optionLabel(options, key) {
  return options.find((item) => item.key === key)?.label || key;
}

function isReadonlyReviewRule(rule) {
  return Boolean(rule?.read_only || rule?.is_builtin || String(rule?.id || "").startsWith("builtin-"));
}

function workflowRecipientEmails(row) {
  const routing = row?.rules?.routing || {};
  const toNames = Array.isArray(routing.to_names) ? routing.to_names : [];
  return toNames.map((item) => String(item || "").trim()).filter(Boolean);
}

function reviewRuleSignature(rule) {
  const field = String(rule?.field || "source_text").trim().toLowerCase();
  const operator = String(rule?.operator || "contains").trim().toLowerCase();
  const value = String(rule?.value || "").replace(/\s+/g, "").trim();
  return `${field}::${operator}::${value}`;
}

function reviewRuleStatusText(rule) {
  if (rule?.is_workflow_rule) {
    const workflowName = String(rule.workflow_name || rule.workflow_code || "").trim();
    const enabledText = rule.enabled === false ? "停用" : "启用";
    return workflowName ? `自定义 · ${workflowName} · ${enabledText}` : `自定义 · ${enabledText}`;
  }
  if (isReadonlyReviewRule(rule)) return "系统内置 · 只读";
  return rule.enabled === false ? "停用" : "启用";
}

function reviewOperatorLabel(operator) {
  if (operator === "system_check") return "系统内置检查";
  return optionLabel(initialReviewState.operator_options || [], operator);
}

function renderInitialReviewRules() {
  const fieldSelect = $("#initial-review-rule-form [name=field]");
  const operatorSelect = $("#initial-review-rule-form [name=operator]");
  if (fieldSelect && operatorSelect) {
    fieldSelect.innerHTML = (initialReviewState.field_options || [])
      .map((field) => `<option value="${h(field.key)}">${h(field.label)}</option>`)
      .join("");
    operatorSelect.innerHTML = (initialReviewState.operator_options || [])
      .map((operator) => `<option value="${h(operator.key)}">${h(operator.label)}</option>`)
      .join("");
  }
  const openButton = $("#initial-review-rule-open");
  if (openButton) openButton.hidden = true;

  const q = tableStates.reviewRules.q.trim().toLowerCase();
  const status = tableStates.reviewRules.status;
  const filteredRules = (v2ReviewRulesState.rules || []).filter((rule) => {
    const enabled = rule.enabled !== false;
    if (status === "enabled" && !enabled) return false;
    if (status === "disabled" && enabled) return false;
    if (!q) return true;
    const haystack = [
      rule.name,
      rule.code,
      rule.description,
      rule.default_blocker_level,
      enabled ? "启用" : "停用",
    ].join(" ").toLowerCase();
    return haystack.includes(q);
  });
  const pageData = paginateLocalRows(filteredRules, "reviewRules");
  $("#initial-review-rules-list").innerHTML =
    (pageData.items || [])
      .map(
        (rule) => `
        <div class="review-rule-row">
          <div class="review-rule-main">
            <strong>${h(rule.name || "未命名规则")}</strong>
            <span class="status-pill ${rule.enabled === false ? "is-muted" : "is-active"}">${rule.enabled === false ? "停用" : "启用"}</span>
          </div>
          <div class="review-rule-field"><small>规则编码</small><span>${h(rule.code || "")}</span></div>
          <div class="review-rule-value"><small>默认等级</small><span>${h(rule.default_blocker_level || "按规则结果")}</span></div>
          <div class="review-rule-message"><small>规则说明</small><span>${h(rule.description || "")}</span></div>
          <div class="actions row-actions review-rule-actions">
            <button class="button ghost" data-action="toggle-v2-review-rule" data-code="${h(rule.code)}">${rule.enabled === false ? "启用" : "停用"}</button>
          </div>
        </div>`
      )
      .join("") || `<div class="empty-note">暂无当前生效的订单预审规则。</div>`;
  renderListPagination("#initial-review-rules-pagination", "reviewRules", pageData);
}

async function refreshV2ReviewRules() {
  v2ReviewRulesState = await api("/api/v2-review/rules");
  renderInitialReviewRules();
}

async function saveV2ReviewRules() {
  v2ReviewRulesState = await api("/api/v2-review/rules", {
    method: "PUT",
    body: JSON.stringify({
      rules: (v2ReviewRulesState.rules || []).map((rule) => ({
        code: rule.code,
        enabled: rule.enabled !== false,
      })),
    }),
  });
  renderInitialReviewRules();
}

async function refreshInitialReviewRules() {
  initialReviewState = await api("/api/initial-review/rules");
  renderInitialReviewRules();
}

async function saveInitialReviewRules() {
  initialReviewState = await api("/api/initial-review/rules", {
    method: "PUT",
    body: JSON.stringify({
      enabled: Boolean(initialReviewState.enabled),
      required_fields: initialReviewState.required_fields || [],
      rules: (initialReviewState.rules || []).filter((rule) => !isReadonlyReviewRule(rule)),
    }),
  });
  renderInitialReviewRules();
}

function findWorkflowVersion(versionId) {
  return (workflowRulesState.items || []).find((item) => item.version_id === versionId);
}

function renderTemplateWithContext(template, context) {
  return String(template || "").replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g, (raw, key) => {
    const value = context[key];
    if (value === undefined || value === null || value === "") return raw;
    return String(value);
  });
}

function workflowTemplateHasVariable(template, field) {
  const escaped = String(field || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\{\\{\\s*${escaped}\\s*\\}\\}`).test(String(template || ""));
}

function workflowFieldLabel(field) {
  const fromInitial = optionLabel(initialReviewState.field_options || [], field);
  if (fromInitial && fromInitial !== field) return fromInitial;
  const extra = {
    material_details: "物料详情描述",
    material_code: "物料编码",
    material_name: "物料名称",
    material_spec: "物料规格",
    material_quantity: "数量",
    logistics_method: "物流发货方式",
    shipping_time_requirement: "出货时间要求",
    customer_receiver_info: "客户收件信息",
    delivery_requirement: "交付要求",
    shipping_warehouse: "出货仓",
    borrow_time: "借用时间",
    return_time: "归还时间",
    sample_approval_screenshot: "样机借用审批截图",
    initiator: "发起人",
    expected_time: "期望时间",
  };
  return extra[field] || field;
}

function workflowFieldKeys() {
  return Array.from(new Set([
    ...(initialReviewState.field_options || []).map((item) => item.key).filter(Boolean),
    "customer_name",
    "product_summary",
    "quantity_text",
    "expected_delivery_date",
    "external_order_no",
    "material_details",
    "material_code",
    "material_name",
    "material_spec",
    "material_quantity",
    "logistics_method",
    "shipping_time_requirement",
    "customer_receiver_info",
    "delivery_requirement",
    "shipping_warehouse",
    "borrow_time",
    "return_time",
    "sample_approval_screenshot",
    "initiator",
    "expected_time",
  ]));
}

function stripWorkflowGeneratedRequiredFieldBlock(bodyTemplate) {
  const lines = String(bodyTemplate || "").split("\n");
  const result = [];
  const generatedLabels = new Set(workflowFieldKeys().map((field) => workflowFieldLabel(field)));
  let inGeneratedBlock = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed === "流程必填信息：") {
      inGeneratedBlock = true;
      continue;
    }
    if (inGeneratedBlock) {
      const match = trimmed.match(/^([^:：]+)[:：]\s*\{\{\s*([a-zA-Z0-9_]+)\s*\}\}\s*$/);
      if (match && generatedLabels.has(match[1])) continue;
      inGeneratedBlock = false;
    }
    result.push(line);
  }
  return result.join("\n").trim();
}

const WORKFLOW_MATERIAL_FIELDS = new Set([
  "external_order_no",
  "product_summary",
  "quantity_text",
  "material_code",
  "material_name",
  "material_spec",
]);

const WORKFLOW_LOGISTICS_FIELDS = new Set([
  "customer_name",
  "expected_delivery_date",
  "logistics_method",
  "shipping_time_requirement",
  "customer_receiver_info",
  "delivery_requirement",
  "shipping_warehouse",
  "borrow_time",
  "return_time",
  "sample_approval_screenshot",
]);

function workflowManagedPreviewLabels() {
  return new Set([
    ...workflowFieldKeys().map((field) => workflowFieldLabel(field)),
    "任务单编号",
    "版本",
    "销售人员",
    "流程类型",
  ]);
}

function stripWorkflowManagedPreviewLines(bodyTemplate) {
  const labels = workflowManagedPreviewLabels();
  const result = [];
  for (const line of String(bodyTemplate || "").split("\n")) {
    const trimmed = line.trim();
    if (trimmed.includes("原流程邮件模板")) {
      break;
    }
    const label = trimmed.split(/[:：]/, 1)[0]?.trim();
    if (label && labels.has(label)) continue;
    if (labels.has(trimmed)) continue;
    result.push(line);
  }
  return result.join("\n").trim();
}

function workflowPreviewFieldLine(field) {
  return `${workflowFieldLabel(field)}：{{${field}}}`;
}

function normalizeWorkflowRequiredFields(requiredFields) {
  const expanded = [];
  for (const rawField of requiredFields || []) {
    const field = String(rawField || "").trim();
    if (!field) continue;
    if (field === "material_details") {
      expanded.push("quantity_text", "material_name", "material_code");
      continue;
    }
    if (field === "material_quantity") {
      expanded.push("quantity_text");
      continue;
    }
    expanded.push(field);
  }
  const seen = new Set();
  return expanded.filter((field) => {
    if (seen.has(field)) return false;
    seen.add(field);
    return true;
  });
}

function stripWorkflowBoilerplate(bodyTemplate) {
  const boilerplate = new Set([
    "请根据以下信息安排生产评估和排产。",
    "请确认是否可以安排生产。如信息不足，请直接回复本邮件说明疑问点。",
    "{{bot_signature}}",
  ]);
  return String(bodyTemplate || "")
    .split("\n")
    .filter((line) => !boilerplate.has(line.trim()))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function ensureWorkflowRequiredFieldsInBody(bodyTemplate, requiredFields) {
  const body = stripWorkflowManagedPreviewLines(stripWorkflowGeneratedRequiredFieldBlock(bodyTemplate));
  const groups = workflowRequiredFieldGroups(normalizeWorkflowRequiredFields(requiredFields));
  const materialFields = groups.find((group) => group.title === "物料信息组")?.fields || [];
  const logisticsFields = groups.find((group) => group.title === "物流信息组")?.fields || [];
  const lines = ["请根据以下信息安排生产评估和排产。"];
  if (materialFields.length) {
    lines.push("");
    lines.push("物料信息：", ...materialFields.map(workflowPreviewFieldLine));
  }
  if (materialFields.length && logisticsFields.length) {
    lines.push("----------");
  }
  if (logisticsFields.length) {
    if (!materialFields.length) lines.push("");
    lines.push("物流信息：", ...logisticsFields.map(workflowPreviewFieldLine));
  }
  const closing = [
    "请确认是否可以安排生产。如信息不足，请直接回复本邮件说明疑问点。",
    "{{bot_signature}}",
  ].join("\n\n");
  return [stripWorkflowBoilerplate(body), lines.join("\n"), closing].filter(Boolean).join("\n\n");
}

function workflowRequiredFieldGroups(requiredFields) {
  const fields = normalizeWorkflowRequiredFields(requiredFields);
  const material = fields.filter((field) => WORKFLOW_MATERIAL_FIELDS.has(field));
  const logistics = fields.filter((field) => WORKFLOW_LOGISTICS_FIELDS.has(field));
  const grouped = new Set([...material, ...logistics]);
  const remaining = fields.filter((field) => !grouped.has(field));
  if (remaining.length) material.push(...remaining);
  return [
    { title: "物料信息组", fields: material },
    { title: "物流信息组", fields: logistics },
  ];
}

function renderWorkflowRequiredFieldGroups(requiredFields, readonly) {
  return workflowRequiredFieldGroups(requiredFields)
    .filter((group) => group.fields.length)
    .map(
      (group) => `
        <div class="workflow-required-group">
          <div class="workflow-required-group-title">${h(group.title)}</div>
          <div class="workflow-required-group-grid">
            ${group.fields
              .map(
                (field) => `
                  <label class="check-item">
                    <input type="checkbox" name="workflow_required_field" value="${h(field)}" checked ${readonly ? "disabled" : ""} />
                    <span>${h(workflowFieldLabel(field))}</span>
                  </label>`
              )
              .join("")}
          </div>
        </div>`
    )
    .join("") || `<div class="empty-note">当前流程未配置必填字段。</div>`;
}

function renderWorkflowPreviewBody(text) {
  const body = String(text || "");
  if (!body) return "";
  const labelPattern = new RegExp(`^(${[
    "任务单编号",
    "版本",
    "客户名称",
    "销售人员",
    "物料/规格",
    "数量",
    "期望交期",
    "订单号",
    "物料详情描述",
    "物料编码",
    "物料名称",
    "物料规格",
    "物料数量",
    "物流发货方式",
    "出货时间要求",
    "客户收件信息",
    "交付要求",
    "出货仓",
    "借用时间",
    "归还时间",
    "样机借用审批截图",
    "物料信息",
    "物流信息",
  ].map((item) => item.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})[:：]`);
  return body
    .split("\n")
    .map((line) => {
      const escaped = h(line || " ");
      const matched = line.match(labelPattern);
      if (!matched) return `<div class="workflow-preview-line">${escaped}</div>`;
      const label = matched[1];
      const rest = line.slice(matched[0].length);
      const delimiter = line.slice(label.length, matched[0].length);
      return `<div class="workflow-preview-line"><span class="workflow-preview-label">${h(label)}</span>${h(delimiter)}${h(rest)}</div>`;
    })
    .join("");
}

function workflowPreviewContext(rule) {
  const workflowName = String(rule?.workflow_name || "示例流程").trim() || "示例流程";
  return {
    task_no: "JM-RW-2026001",
    version_no: "1",
    customer_name: "示例客户",
    salesperson_name: "商务部小J",
    salesperson_email: "bot.business@jimuyida.com",
    product_summary: "示例物料A",
    quantity_text: "120套",
    expected_delivery_date: "2026-05-20",
    external_order_no: "SO-DEMO-2026001",
    workflow_name: workflowName,
    material_details: "物料编码：MAT-A001\n物料名称：示例物料A\n数量：120套",
    material_code: "MAT-A001",
    material_name: "示例物料A",
    material_spec: "标准版",
    material_quantity: "120套",
    logistics_method: "顺丰",
    shipping_time_requirement: "2026-05-18 前出货",
    customer_receiver_info: "示例省示例市示例路 88 号，张三 13800000000",
    delivery_requirement: "木箱加固",
    shipping_warehouse: "武汉仓",
    borrow_time: "2026-05-15",
    return_time: "2026-06-15",
    sample_approval_screenshot: "已附审批截图",
    initiator: "商务部小J",
    expected_time: "2026-05-20",
    bot_signature: "商务部小J",
  };
}

async function loadProductionDepartmentOptions() {
  const data = normalizeListPayload(await api("/api/departments?status=Active&page=1&page_size=100"), { page: 1, page_size: 100 });
  productionDepartmentState.items = data.items || [];
  return productionDepartmentState.items;
}

function productionMainEmailOptions() {
  const options = [];
  const seen = new Set();
  for (const department of productionDepartmentState.items || []) {
    for (const email of department.mail_to || []) {
      const value = String(email || "").trim();
      if (!value || seen.has(value.toLowerCase())) continue;
      seen.add(value.toLowerCase());
      options.push({
        value,
        label: `${department.department_name || department.department_code || "生产部门"} <${value}>`,
      });
    }
  }
  return options;
}

function updateWorkflowMailPreview(ruleOrRaw = "") {
  const subjectNode = $("#workflow-mail-preview-subject");
  const bodyNode = $("#workflow-mail-preview-body");
  if (!subjectNode || !bodyNode) return;
  let rule;
  if (typeof ruleOrRaw === "string") {
    const source = String(ruleOrRaw || "").trim();
    if (!source) {
      subjectNode.textContent = "请先选择流程规则";
      bodyNode.textContent = "请先选择流程规则";
      return;
    }
    try {
      rule = JSON.parse(source);
    } catch (error) {
      subjectNode.textContent = "JSON 解析失败";
      bodyNode.textContent = error?.message || "请检查 JSON 格式";
      return;
    }
  } else {
    rule = ruleOrRaw || {};
  }
  const context = workflowPreviewContext(rule);
  const requiredFields = normalizeWorkflowRequiredFields(
    Array.isArray(rule?.required_fields) ? rule.required_fields.map((item) => String(item || "").trim()).filter(Boolean) : []
  );
  const subjectTemplate = String(rule?.subject_template || "").trim();
  const bodyTemplate = ensureWorkflowRequiredFieldsInBody(String(rule?.body_template || "").trim(), requiredFields);
  subjectNode.textContent = subjectTemplate
    ? renderTemplateWithContext(subjectTemplate, context)
    : "（未配置 subject_template，保存后将使用系统默认主题模板）";
  bodyNode.textContent = bodyTemplate
    ? renderTemplateWithContext(bodyTemplate, context)
    : "（未配置 body_template，保存后将使用系统默认正文模板）";
  bodyNode.innerHTML = renderWorkflowPreviewBody(bodyNode.textContent);
}

function syncWorkflowRuleEditorState() {
  const form = $("#workflow-rule-editor-form");
  if (!form || !workflowRulesState.editingRules) return null;
  if (workflowRulesState.readonly) return workflowRulesState.editingRules;
  const requiredFields = normalizeWorkflowRequiredFields(
    [...form.querySelectorAll("#workflow-required-fields [name=workflow_required_field]:checked")].map((input) => input.value)
  );
  const toField = form.querySelector("[name=routing_to_names]");
  const toNames = toField?.tagName === "SELECT"
    ? [toField.value].filter(Boolean)
    : splitRoutingNames(toField?.value || "");
  const ccNames = splitRoutingNames(form.querySelector("[name=routing_cc_names]")?.value || "");
  const maxQuestionRounds = Number(form.querySelector("[name=max_question_rounds]")?.value || 0);
  const exceededMessage = String(form.querySelector("[name=conversation_exceeded_message]")?.value || "").trim();
  const reviewRules = (workflowRulesState.editingRules.review_rules || []).map((rule) => ({
    ...rule,
    enabled: rule.enabled !== false,
  }));
  const nextRules = {
    ...workflowRulesState.editingRules,
    routing: {
      ...(workflowRulesState.editingRules.routing || {}),
      to_names: toNames,
      cc_names: ccNames,
    },
    required_fields: requiredFields,
    review_rules: reviewRules,
  };
  if (maxQuestionRounds > 0) {
    nextRules.conversation_policy = {
      max_question_rounds: Math.max(1, Math.min(maxQuestionRounds, 20)),
      on_exceeded: "close_task",
      message: exceededMessage,
    };
  } else {
    nextRules.conversation_policy = {};
  }
  workflowRulesState.editingRules = nextRules;
  const hidden = form.querySelector("[name=compiled_rules_json]");
  if (hidden) hidden.value = JSON.stringify(nextRules);
  updateWorkflowMailPreview(nextRules);
  return nextRules;
}

function renderWorkflowRuleEditor() {
  const form = $("#workflow-rule-editor-form");
  const rules = workflowRulesState.editingRules || {};
  if (!form) return;
  const readonly = Boolean(workflowRulesState.readonly);
  const requiredFields = normalizeWorkflowRequiredFields(Array.isArray(rules.required_fields) ? rules.required_fields : []);
  const reviewRules = Array.isArray(rules.review_rules) ? rules.review_rules : [];
  const conversationPolicy = rules.conversation_policy && typeof rules.conversation_policy === "object" ? rules.conversation_policy : {};
  const routing = rules.routing && typeof rules.routing === "object" ? rules.routing : {};
  const toNames = Array.isArray(routing.to_names) ? routing.to_names : [];
  const ccNames = Array.isArray(routing.cc_names) ? routing.cc_names : [];
  const toSelect = form.querySelector("[name=routing_to_names]");
  const emailOptions = productionMainEmailOptions();
  const optionValues = new Set(emailOptions.map((item) => item.value));
  const selectedTo = String(toNames.find((item) => String(item || "").trim()) || (!readonly ? emailOptions[0]?.value : "") || "").trim();
  const readonlyOnlyOptions = readonly && selectedTo && !optionValues.has(selectedTo)
    ? [{ value: selectedTo, label: `${selectedTo}（未在生产邮箱列表中）` }]
    : [];
  toSelect.innerHTML = [
    ...(!readonly ? [{ value: "", label: "请选择生产部门主送邮箱" }] : []),
    ...emailOptions,
    ...readonlyOnlyOptions,
  ]
    .map((item) => `<option value="${h(item.value)}" ${selectedTo === item.value ? "selected" : ""}>${h(item.label)}</option>`)
    .join("");
  toSelect.disabled = readonly || !emailOptions.length;
  toSelect.required = !readonly;
  const routingHelp = $("#workflow-routing-help");
  if (routingHelp) {
    routingHelp.textContent = !emailOptions.length
      ? "请先在【生产邮箱】添加启用生产部门的主送邮箱。"
      : "主送人必须从生产部门主送邮箱列表中选择。";
  }
  form.querySelector("[name=routing_cc_names]").value = ccNames.join("、");
  form.querySelector("[name=routing_cc_names]").disabled = readonly;
  form.querySelector("[name=max_question_rounds]").value = conversationPolicy.max_question_rounds || "";
  form.querySelector("[name=max_question_rounds]").disabled = readonly;
  form.querySelector("[name=conversation_exceeded_message]").value = conversationPolicy.message || "";
  form.querySelector("[name=conversation_exceeded_message]").disabled = readonly;
  $("#workflow-required-fields").innerHTML = renderWorkflowRequiredFieldGroups(requiredFields, readonly);

  const existingIds = new Set(reviewRules.map((rule) => String(rule.id || "")));
  const existingSignatures = new Set(reviewRules.map((rule) => reviewRuleSignature(rule)));
  const selectableRules = (initialReviewState.rules || []).filter(
    (rule) => !isReadonlyReviewRule(rule) && !existingIds.has(String(rule.id || "")) && !existingSignatures.has(reviewRuleSignature(rule))
  );
  const selector = form.querySelector("[name=review_rule_source]");
  selector.innerHTML =
    `<option value="">选择初审面板中的自定义规则</option>` +
    selectableRules.map((rule) => `<option value="${h(rule.id)}">${h(rule.name || "未命名规则")}</option>`).join("");
  selector.disabled = readonly;
  form.querySelector('[data-action="add-workflow-review-rule"]').hidden = readonly;

  $("#workflow-review-rules-list").innerHTML =
    reviewRules
      .map(
        (rule) => `
          <div class="workflow-review-rule-item">
            <div><strong>${h(rule.name || "未命名规则")}</strong><br /><small>${h(rule.enabled === false ? "停用" : "启用")}</small></div>
            <div class="workflow-review-rule-meta">
              <span><small>字段 / 判断</small><br />${h(optionLabel(initialReviewState.field_options || [], rule.field))} · ${h(reviewOperatorLabel(rule.operator))}</span>
              <span><small>规则值</small><br />${h(rule.value || "无")}</span>
            </div>
            <p>${h(rule.message || "未填写未通过原因")}</p>
            <div class="actions row-actions">
              ${
                readonly || isReadonlyReviewRule(rule)
                  ? `<span class="status-pill">${isReadonlyReviewRule(rule) ? "系统内置 · 只读" : "只读"}</span>`
                  : `<button class="button warn" type="button" data-action="remove-workflow-review-rule" data-id="${h(rule.id)}">移除规则</button>`
              }
            </div>
          </div>`
      )
      .join("") || `<div class="empty-note">当前流程未配置专属初审规则。</div>`;
  if (readonly) {
    updateWorkflowMailPreview(rules);
  } else {
    syncWorkflowRuleEditorState();
  }
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    const chunk = bytes.subarray(offset, offset + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function openWorkflowImportModal() {
  const modal = $("#workflow-import-modal");
  if (!modal) return;
  modal.hidden = false;
  const form = $("#workflow-import-form");
  form?.querySelector("[name=workflow_file]")?.focus();
}

function closeWorkflowImportModal() {
  const modal = $("#workflow-import-modal");
  if (modal) modal.hidden = true;
}

function openInitialReviewRuleModal() {
  const modal = $("#initial-review-rule-modal");
  if (!modal) return;
  modal.hidden = false;
  $("#initial-review-rule-form [name=name]")?.focus();
}

function closeInitialReviewRuleModal() {
  const modal = $("#initial-review-rule-modal");
  if (modal) modal.hidden = true;
}

function openWorkflowRuleEditor(versionId, rules, options = {}) {
  const form = $("#workflow-rule-editor-form");
  if (!form) return;
  const readonly = Boolean(options.readonly);
  form.hidden = false;
  const modal = $("#workflow-editor-modal");
  if (modal) modal.hidden = false;
  const title = $("#workflow-editor-title");
  if (title) title.textContent = rules?.workflow_name ? `${readonly ? "查看" : "编辑"}流程：${rules.workflow_name}` : readonly ? "查看流程" : "编辑流程";
  const subtitle = $("#workflow-editor-subtitle");
  if (subtitle) subtitle.textContent = versionId ? `流程版本 ${versionId}${readonly ? "（只读）" : ""}` : readonly ? "流程规则查看" : "流程规则编辑";
  form.querySelector("[name=version_id]").value = versionId || "";
  workflowRulesState.editingRules = JSON.parse(JSON.stringify(rules || {}));
  form.querySelector("[name=compiled_rules_json]").value = JSON.stringify(workflowRulesState.editingRules);
  workflowRulesState.editingVersionId = versionId || "";
  workflowRulesState.readonly = readonly;
  form.querySelector('[data-action="save-activate"]').hidden = readonly;
  renderWorkflowRuleEditor();
}

function closeWorkflowRuleEditor() {
  const form = $("#workflow-rule-editor-form");
  if (form) {
    form.hidden = true;
    form.querySelector("[name=version_id]").value = "";
    form.querySelector("[name=compiled_rules_json]").value = "";
  }
  const modal = $("#workflow-editor-modal");
  if (modal) modal.hidden = true;
  updateWorkflowMailPreview("");
  workflowRulesState.editingVersionId = "";
  workflowRulesState.editingRules = null;
  workflowRulesState.readonly = false;
}

function renderWorkflowRules() {
  const node = $("#workflow-rules-list");
  if (!node) return;
  const rows = workflowRulesState.items || [];
  const q = tableStates.workflows.q.trim().toLowerCase();
  const status = tableStates.workflows.status;
  const statusOptions = Array.from(new Set(rows.map((row) => String(row.status || "").trim()).filter(Boolean)));
  setSelectOptions("#workflows-filter-form [name=status]", statusOptions, "全部状态", tableStates.workflows.status);
  const filteredRows = rows.filter((row) => {
    if (status && String(row.status || "") !== status) return false;
    if (!q) return true;
    const routing = row.rules?.routing || {};
    const toNames = Array.isArray(routing.to_names) ? routing.to_names : [];
    const haystack = [
      row.workflow_name,
      row.workflow_code,
      row.status,
      `V${row.version_no || ""}`,
      toNames.join(", "),
      row.approved_at || "",
      row.created_at || "",
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(q);
  });
  const pageData = paginateLocalRows(filteredRows, "workflows");
  node.innerHTML =
    (pageData.items || [])
      .map((row) => {
        const routing = row.rules?.routing || {};
        const toNames = workflowRecipientEmails(row);
        const reviewRules = Array.isArray(row.rules?.review_rules) ? row.rules.review_rules : [];
        const enabledReviewRules = reviewRules.filter((item) => item && item.enabled !== false);
        const isBuiltin = Boolean(row.is_builtin);
        const editable = !isBuiltin && Boolean(row.editable);
        const activatable = !isBuiltin && Boolean(row.activatable);
        const deactivatable = !isBuiltin && Boolean(row.deactivatable);
        const deletable = !isBuiltin && Boolean(row.deletable);
        return `
          <div class="row">
            <div><strong>${h(row.workflow_name || row.workflow_code || "未命名流程")}</strong><br /><small>${h(row.workflow_code || "-")} · V${h(row.version_no)}</small></div>
            <div><small>状态</small><br />${h(row.status)}</div>
            <div><small>收件人</small><br />${h(toNames.join(", ") || "未配置")}<br /><small>专属初审规则 ${h(enabledReviewRules.length)}/${h(reviewRules.length)}</small></div>
            <div>
              <small>${h(isBuiltin ? "内置默认流程（只读）" : formatTime(row.approved_at || row.created_at))}</small>
              <div class="actions row-actions">
                <button class="button ghost" data-action="view-workflow-version" data-id="${h(row.version_id)}">查看规则</button>
                <button class="button ghost" data-action="diff-workflow-version" data-id="${h(row.version_id)}">版本差异</button>
                ${
                  isBuiltin
                    ? ""
                    : `
                  ${activatable ? `<button class="button" data-action="activate-workflow-version" data-id="${h(row.version_id)}">启用流程</button>` : ""}
                  ${activatable ? `<button class="button ghost" data-action="rollback-workflow-version" data-id="${h(row.version_id)}">回滚到此版本</button>` : ""}
                  ${deactivatable ? `<button class="button ghost" data-action="deactivate-workflow-version" data-id="${h(row.version_id)}">停用流程</button>` : ""}
                  ${editable ? `<button class="button ghost" data-action="llm-edit-workflow-version" data-id="${h(row.version_id)}">LLM编辑</button>` : ""}
                  ${editable ? `<button class="button ghost" data-action="edit-workflow-version" data-id="${h(row.version_id)}">编辑规则</button>` : ""}
                  ${deletable ? `<button class="button warn" data-action="delete-workflow-version" data-id="${h(row.version_id)}">删除流程</button>` : ""}
                `
                }
              </div>
            </div>
          </div>`;
      })
      .join("") || `<div class="row"><div>暂无流程规则版本，请先导入流程文档。</div></div>`;
  renderListPagination("#workflows-pagination", "workflows", pageData);
}

async function refreshWorkflowRules() {
  await loadProductionDepartmentOptions();
  const data = await api("/api/workflows");
  workflowRulesState.items = data.items || [];
  renderWorkflowRules();
}

async function saveWorkflowRuleEditor() {
  const form = $("#workflow-rule-editor-form");
  if (!form || form.hidden) return;
  const versionId = form.querySelector("[name=version_id]").value;
  if (!versionId) {
      toast("缺少流程版本 ID");
      return;
    }
  const compiledRules = syncWorkflowRuleEditorState();
  if (!compiledRules) {
    toast("流程规则状态为空");
    return;
  }
  if (!(compiledRules.routing?.to_names || []).length) {
    toast("请选择主送人邮箱");
    return;
  }
  const saved = await api(`/api/workflows/versions/${versionId}`, {
    method: "PUT",
    body: JSON.stringify({ compiled_rules: compiledRules, activate: true }),
  });
  await refreshWorkflowRules();
  closeWorkflowRuleEditor();
  toast("流程规则已更新并启用");
}

function renderWorkflowChatPreview() {
  const node = $("#workflow-chat-preview");
  if (!node) return;
  const saveButton = $("#workflow-chat-save");
  if (saveButton) saveButton.disabled = !workflowChatState.compiledRule;
  if (!workflowChatState.compiledRule && !(workflowChatState.validationErrors || []).length) {
    node.classList.remove("show");
    node.innerHTML = "";
    return;
  }
  node.classList.add("show");
  node.innerHTML = renderJsonPreview({
    ready: Boolean(workflowChatState.ready),
    edit_version_id: workflowChatState.editVersionId || "",
    edit_workflow_name: workflowChatState.editWorkflowName || "",
    validation_errors: workflowChatState.validationErrors || [],
    compiled_rule: workflowChatState.compiledRule,
  });
}

function renderJsonPreview(value, indent = 0) {
  const pad = "  ".repeat(indent);
  const nextPad = "  ".repeat(indent + 1);
  if (value === null) return `<span class="json-null">null</span>`;
  if (typeof value === "string") return `<span class="json-string">${h(JSON.stringify(value))}</span>`;
  if (typeof value === "number") return `<span class="json-number">${h(String(value))}</span>`;
  if (typeof value === "boolean") return `<span class="json-boolean">${value ? "true" : "false"}</span>`;
  if (Array.isArray(value)) {
    if (!value.length) return `<span class="json-punctuation">[]</span>`;
    return [
      `<span class="json-punctuation">[</span>`,
      ...value.map((item, index) => `${nextPad}${renderJsonPreview(item, indent + 1)}${index < value.length - 1 ? '<span class="json-punctuation">,</span>' : ""}`),
      `${pad}<span class="json-punctuation">]</span>`,
    ].join("\n");
  }
  if (typeof value === "object") {
    const entries = Object.entries(value || {});
    if (!entries.length) return `<span class="json-punctuation">{}</span>`;
    return [
      `<span class="json-punctuation">{</span>`,
      ...entries.map(([key, item], index) => {
        const comma = index < entries.length - 1 ? '<span class="json-punctuation">,</span>' : "";
        return `${nextPad}<span class="json-key">${h(JSON.stringify(key))}</span><span class="json-punctuation">:</span> ${renderJsonPreview(item, indent + 1)}${comma}`;
      }),
      `${pad}<span class="json-punctuation">}</span>`,
    ].join("\n");
  }
  return `<span class="json-null">${h(String(value))}</span>`;
}

function resetWorkflowChat() {
  workflowChatState = { messages: [], compiledRule: null, validationErrors: [], ready: false, editVersionId: "", editWorkflowName: "" };
  const log = $("#workflow-chat-log");
  if (log) {
    log.innerHTML = `
      <div class="chat-message assistant">
        <small>流程助手</small>
        <p>请描述要新增或编辑的流程，我会通过多轮对话整理并生成可落库的流程规则。</p>
      </div>
    `;
  }
  const activate = $("#workflow-chat-activate");
  if (activate) activate.checked = false;
  renderWorkflowChatPreview();
}

function startWorkflowChatEdit(row) {
  if (!row || !row.version_id || !row.rules) return;
  workflowChatState = {
    messages: [],
    compiledRule: row.rules,
    validationErrors: [],
    ready: false,
    editVersionId: row.version_id,
    editWorkflowName: row.workflow_name || row.workflow_code || "",
  };
  const log = $("#workflow-chat-log");
  if (log) {
    log.innerHTML = `
      <div class="chat-message assistant">
        <small>流程助手</small>
        <p>正在编辑“${h(workflowChatState.editWorkflowName)}”。请直接说明要修改的字段、规则或邮件模板。</p>
      </div>
    `;
  }
  const activate = $("#workflow-chat-activate");
  if (activate) activate.checked = false;
  renderWorkflowChatPreview();
}

async function refreshConfig() {
  const data = await api("/api/config");
  runtimeConfigState = data.configs || {};
  startupReadinessState = data.startup_readiness || { ready: false, missing: [] };
  fillForm("#runtime-mail-form", data.configs || {});
  fillForm("#erp-config-form", data.configs || {});
  fillForm("#crm-sync-config-form", data.configs || {});
  fillForm("#oms-config-form", data.configs || {});
  fillForm("#e2e-mail-form", data.configs || {});
  if (data.model) {
    fillForm("#model-form", data.model);
  }
  const password = $("#runtime-mail-form [name=bot_email_password]");
  if (password) password.value = "";
  const baiduMapAk = $("#runtime-mail-form [name=baidu_map_ak]");
  if (baiduMapAk) baiduMapAk.value = "";
  const erpAppSec = $("#erp-config-form [name=erp_app_sec]");
  if (erpAppSec) erpAppSec.value = "";
  const crmPassword = $("#crm-sync-config-form [name=crm_password]");
  if (crmPassword) crmPassword.value = "";
  const crmApiKey = $("#crm-sync-config-form [name=crm_api_key]");
  if (crmApiKey) crmApiKey.value = "";
  const crmRequestJson = $("#crm-sync-config-form [name=crm_fxiaoke_request_json]");
  if (crmRequestJson) crmRequestJson.value = "";
  const crmDetailRequestJson = $("#crm-sync-config-form [name=crm_fxiaoke_detail_request_json]");
  if (crmDetailRequestJson) crmDetailRequestJson.value = "";
  const omsAppSecret = $("#oms-config-form [name=oms_jackyun_app_secret]");
  if (omsAppSecret) omsAppSecret.value = "";
  document.querySelectorAll("#e2e-mail-form input[type=password]").forEach((input) => {
    input.value = "";
  });
  const modelKey = $("#model-form [name=api_key]");
  if (modelKey) modelKey.value = "";
  renderSystemToggle();
}

async function refreshWeeklyReportRecipients() {
  const data = await api("/api/reports/weekly/recipients");
  $("#weekly-report-recipients-form [name=to]").value = (data.to || []).join(", ");
  $("#weekly-report-recipients-form [name=cc]").value = (data.cc || []).join(", ");
}

async function refreshJobs() {
  const data = normalizeListPayload(await api(`/api/jobs?${queryFromState(tableStates.jobs)}`), tableStates.jobs);
  const rows = data.items || [];
  setSelectOptions("#jobs-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.jobs.status);
  setSelectOptions("#jobs-filter-form [name=job_type]", data.job_type_options || [], "全部类型", tableStates.jobs.job_type);
  $("#jobs-list").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row">
          <div><strong>${h(row.job_type)}</strong><br /><small>${h(row.id)}</small></div>
          <div><small>状态</small><br />${h(row.status)}</div>
          <div><small>尝试 / 版本</small><br />${h(row.attempt_count)} / V${h(row.version ?? 0)}</div>
          <div><small>${h(row.error_message || formatTime(row.created_at))}</small></div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无入库队列任务</div></div>`;
  renderListPagination("#jobs-pagination", "jobs", data);
}

async function refreshIntegrationEvents() {
  const data = normalizeListPayload(await api(`/api/integration-events?${queryFromState(tableStates.integrationEvents)}`), tableStates.integrationEvents);
  setSelectOptions("#integration-events-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.integrationEvents.status);
  setSelectOptions("#integration-events-filter-form [name=event_type]", data.event_type_options || [], "全部事件", tableStates.integrationEvents.event_type);
  setSelectOptions("#integration-events-filter-form [name=source_system]", data.source_system_options || [], "全部系统", tableStates.integrationEvents.source_system);
  $("#integration-events-list").innerHTML =
    (data.items || []).map((row) => `
      <div class="row">
        <div>
          <strong>${h(row.event_type)}</strong><br />
          <small>${h(row.source_system)} · ${h(row.status)}</small>
        </div>
        <div>
          <strong>${h(row.biz_key)}</strong><br />
          <small>${h(String(row.payload_hash || "").slice(0, 12))}</small>
        </div>
        <div>
          <strong>${h(row.retry_count || 0)}</strong><br />
          <small>${h(formatTime(row.updated_at || row.created_at))}</small>
        </div>
        <div>
          <small>${h(row.error_message || row.trace_id || "-")}</small>
        </div>
      </div>
    `).join("") || `<div class="row"><div>暂无接口日志</div></div>`;
  renderListPagination("#integration-events-pagination", "integrationEvents", data);
}

async function refreshAgentRuns() {
  const data = normalizeListPayload(await api(`/api/agent-run-logs?${queryFromState(tableStates.agentRuns)}`), tableStates.agentRuns);
  setSelectOptions("#agent-runs-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.agentRuns.status);
  setSelectOptions("#agent-runs-filter-form [name=agent_name]", data.agent_name_options || [], "全部 Agent", tableStates.agentRuns.agent_name);
  setSelectOptions("#agent-runs-filter-form [name=task_type]", data.task_type_options || [], "全部任务", tableStates.agentRuns.task_type);
  $("#agent-runs-list").innerHTML =
    (data.items || []).map((row) => `
      <div class="row">
        <div>
          <strong>${h(row.agent_name)}</strong><br />
          <small>${h(row.task_type)} · ${h(row.status)}</small>
        </div>
        <div>
          <strong>${h(row.related_object_id || "-")}</strong><br />
          <small>${h(row.related_object_type || "未关联对象")}</small>
        </div>
        <div>
          <strong>${h(formatTime(row.finished_at || row.started_at))}</strong><br />
          <small>${h(row.finished_at ? "已结束" : "运行中")}</small>
        </div>
        <div>
          <small>${h(row.error_message || (row.output && row.output.summary) || "-")}</small>
        </div>
      </div>
    `).join("") || `<div class="row"><div>暂无 Agent 运行记录</div></div>`;
  renderListPagination("#agent-runs-pagination", "agentRuns", data);
}

async function refreshModelCalls() {
  const data = normalizeListPayload(await api(`/api/model-call-logs?${queryFromState(tableStates.modelCalls)}`), tableStates.modelCalls);
  setSelectOptions("#model-calls-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.modelCalls.status);
  setSelectOptions("#model-calls-filter-form [name=task_type]", data.task_type_options || [], "全部任务", tableStates.modelCalls.task_type);
  $("#model-calls-list").innerHTML =
    (data.items || []).map((row) => `
      <div class="row">
        <div>
          <strong>${h(row.task_type)}</strong><br />
          <small>${h(row.status)} · ${h(row.provider_config_id || "无模型配置")}</small>
        </div>
        <div>
          <strong>${h(row.related_object_id || "-")}</strong><br />
          <small>${h(row.related_object_type || "未关联对象")}</small>
        </div>
        <div>
          <strong>${h(row.latency_ms ?? "-")}</strong><br />
          <small>${h(formatTime(row.created_at))}</small>
        </div>
        <div>
          <small>${h(row.error_message || (row.output && row.output.summary) || (row.output && row.output.root_cause) || "-")}</small>
        </div>
      </div>
    `).join("") || `<div class="row"><div>暂无模型调用记录</div></div>`;
  renderListPagination("#model-calls-pagination", "modelCalls", data);
}

async function refreshAttachments() {
  const data = normalizeListPayload(await api(`/api/attachments?${queryFromState(tableStates.attachments)}`), tableStates.attachments);
  const rows = data.items || [];
  setSelectOptions("#attachments-filter-form [name=parse_status]", data.parse_status_options || [], "全部状态", tableStates.attachments.parse_status);
  setSelectOptions("#attachments-filter-form [name=content_type]", data.content_type_options || [], "全部类型", tableStates.attachments.content_type);
  $("#attachments-list").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row">
          <div><strong>${h(row.file_name)}</strong><br /><small>${h(row.mail_id)}</small></div>
          <div><small>解析</small><br />${h(row.parse_status)}</div>
          <div><small>大小</small><br />${h(row.file_size)}</div>
          <div><small>${h(row.text_preview || row.parse_error || row.archive_path || "")}</small></div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无附件</div></div>`;
  renderListPagination("#attachments-pagination", "attachments", data);
}

async function refreshMails() {
  const data = normalizeListPayload(await api(`/api/mails?${queryFromState(tableStates.mails)}`), tableStates.mails);
  const rows = data.items || [];
  setSelectOptions("#mails-filter-form [name=classification]", data.classification_options || [], "全部分类", tableStates.mails.classification);
  setSelectOptions("#mails-filter-form [name=direction]", data.direction_options || [], "全部方向", tableStates.mails.direction);
  $("#mails-list").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row clickable-row" data-mail-id="${h(row.id)}" role="button" tabindex="0" title="查看邮件详情">
          <div><strong>${h(row.subject)}</strong><br /><small>${h(row.id)}</small><br /><small>${h(row.from_address)}</small></div>
          <div><small>分类</small><br />${h(row.classification)} (${h(row.classification_confidence)})</div>
          <div><small>任务</small><br />${renderRelatedTaskLink(row)}</div>
          <div><small>收件时间</small><br />${h(formatTime(row.received_at || row.created_at))}</div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无入库邮件</div></div>`;
  renderListPagination("#mails-pagination", "mails", data);
}

async function openMailDetail(mailId) {
  const detail = await api(`/api/mails/${mailId}`);
  $("#mail-detail-title").textContent = detail.subject || "邮件详情";
  $("#mail-detail-meta").textContent = `${detail.direction || "邮件"} · 收件时间 ${formatTime(detail.received_at || detail.created_at)}`;
  $("#mail-detail-fields").innerHTML = `
    <div><small>邮件ID</small><strong>${h(detail.id || "未记录")}</strong></div>
    <div><small>发件人</small><strong>${h(detail.from_address || "未记录")}</strong></div>
    <div><small>收件人</small><strong>${h((detail.to || []).join(", ") || "未记录")}</strong></div>
    <div><small>抄送人</small><strong>${h((detail.cc || []).join(", ") || "无")}</strong></div>
    <div><small>分类</small><strong>${h(detail.classification || "未分类")} (${h(detail.classification_confidence ?? 0)})</strong></div>
    <div><small>关联任务</small><strong>${renderRelatedTaskLink(detail)}</strong></div>
    <div><small>附件</small><strong>${h((detail.attachments || []).map((item) => item.file_name).join(", ") || "无")}</strong></div>
  `;
  $("#mail-detail-body").textContent = detail.body_text || "无正文内容";
  $("#mail-detail-modal").hidden = false;
}

async function openOutboundDetail(outboundId) {
  const detail = await api(`/api/outbound-mails/${outboundId}`);
  $("#mail-detail-title").textContent = detail.subject || "外发邮件详情";
  $("#mail-detail-meta").textContent = `外发队列 · 发送时间 ${detail.sent_at ? formatTime(detail.sent_at) : "未发送"}`;
  $("#mail-detail-fields").innerHTML = `
    <div><small>外发ID</small><strong>${h(detail.id || "未记录")}</strong></div>
    <div><small>邮件类型</small><strong>${h(detail.mail_type || "未记录")}</strong></div>
    <div><small>主送</small><strong>${h((detail.to || []).join(", ") || "未记录")}</strong></div>
    <div><small>抄送人</small><strong>${h((detail.cc || []).join(", ") || "无")}</strong></div>
    <div><small>状态</small><strong>${h(detail.status || "未记录")}</strong></div>
    <div><small>排队时间</small><strong>${h(formatTime(detail.created_at) || "未记录")}</strong></div>
    <div><small>发送时间</small><strong>${h(detail.sent_at ? formatTime(detail.sent_at) : "未发送")}</strong></div>
    <div><small>关联任务</small><strong>${renderRelatedTaskLink(detail)}</strong></div>
    <div><small>关联版本</small><strong>${h(detail.related_version_id || "未关联")}</strong></div>
    <div><small>幂等键</small><strong>${h(detail.idempotency_key || "未记录")}</strong></div>
  `;
  $("#mail-detail-body").textContent = detail.body || "无正文内容";
  $("#mail-detail-modal").hidden = false;
}

function closeMailDetail() {
  $("#mail-detail-modal").hidden = true;
}

function detailField(label, value) {
  const text = value === null || value === undefined || value === "" ? "未记录" : value;
  return `<div><small>${h(label)}</small><strong>${h(text)}</strong></div>`;
}

function detailFieldHtml(label, html) {
  return `<div><small>${h(label)}</small><strong>${html || "未记录"}</strong></div>`;
}

function listOnlyValue(value, crmDetailStatus) {
  if (value !== null && value !== undefined && value !== "") return value;
  if (crmDetailStatus === "list_only") return "CRM 列表接口未返回，需同步订单详情";
  return "未记录";
}

function renderCrmAttachments(attachments = [], fallbackFiles = []) {
  const source = Array.isArray(attachments) && attachments.length
    ? attachments
    : (Array.isArray(fallbackFiles) ? fallbackFiles.map((name) => ({ file_name: name, has_download: false, parse_status: "NameOnly" })) : []);
  const normalized = [];
  const seen = new Set();
  source.forEach((item) => {
    const name = typeof item === "string" ? item : (item.file_name || item.name || item.filename || item.url || "未命名附件");
    const id = typeof item === "object" ? item.source_file_id || item.file_id || "" : "";
    const key = `${String(id).trim().toLowerCase()}|${String(name).trim().toLowerCase()}`;
    if (!key.trim() || seen.has(key)) return;
    seen.add(key);
    normalized.push(item);
  });
  if (!normalized.length) return "无";
  return `
    <div class="crm-attachment-list">
      ${normalized.map((item) => {
        const name = typeof item === "string" ? item : (item.file_name || item.name || item.filename || item.url || "未命名附件");
        const url = typeof item === "object" ? item.download_url || item.file_url || item.url || "" : "";
        const status = typeof item === "object" ? item.parse_status || "" : "";
        return `
          <div class="crm-attachment-item">
            <span>${h(name)}</span>
            ${
              url
                ? `<a class="button ghost compact-action" href="${h(url)}" target="_blank" rel="noopener noreferrer">查看/下载</a>`
                : `<small>${status === "NameOnly" ? "仅有文件名，需同步订单详情后下载" : "暂无下载地址"}</small>`
            }
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderCrmSnapshots(snapshots = []) {
  if (!Array.isArray(snapshots) || !snapshots.length) {
    return `<p class="muted-line">暂无详情快照</p>`;
  }
  return snapshots.slice(0, 6).map((snapshot) => `
    <p>
      <strong>V${h(snapshot.version || "-")}</strong>
      ${snapshot.is_latest ? " · 当前" : ""}
      · ${h(snapshot.parse_status || "")}
      <br />
      <small>${h(String(snapshot.payload_hash || "").slice(0, 12))} · ${h(formatTime(snapshot.captured_at))}</small>
    </p>
  `).join("");
}

function renderSnapshotDiff(diff = {}) {
  const changes = Array.isArray(diff.changes) ? diff.changes : [];
  if (!changes.length) {
    return `<p class="muted-line">暂无快照字段差异</p>`;
  }
  return `
    <div class="snapshot-diff-list">
      <div class="snapshot-diff-head">
        <span>字段</span>
        <span>旧值</span>
        <span>新值</span>
        <span>来源</span>
      </div>
      ${changes
        .map(
          (item) => `
            <div class="snapshot-diff-row">
              <strong>${h(item.field_label || item.field || "-")}</strong>
              <small>${h(item.old_value || "空")}</small>
              <small>${h(item.new_value || "空")}</small>
              <small>${h(item.source_path || item.source || "-")}</small>
            </div>`
        )
        .join("")}
    </div>
    <p class="muted-line">快照 V${h(diff.from_version || "-")} → V${h(diff.to_version || "-")}</p>
  `;
}

function renderDeliveryNoticeEvidence(notices = []) {
  if (!Array.isArray(notices) || !notices.length) {
    return `<p class="muted-line">暂无发货通知</p>`;
  }
  return notices.map((notice) => `
    <p>
      <strong>${h(notice.notice_no)}</strong> · ${h(notice.status)}
      ${notice.oms_order_no ? ` · OMS ${h(notice.oms_order_no)}` : ""}
      ${notice.waybill_no ? ` · 运单 ${h(notice.waybill_no)}` : ""}
      <br />
      <small>业务版本 V${h(notice.notice_version || 1)} · 锁版本 ${h(notice.version ?? 0)} · 面单 ${h(notice.print_status || "NotRequested")}${notice.print_retry_count ? `(${h(notice.print_retry_count)})` : ""} · 平台回传 ${h(notice.platform_fulfillment_status || "NotRequired")}${notice.platform_fulfillment_retry_count ? `(${h(notice.platform_fulfillment_retry_count)})` : ""}</small>
      ${notice.source_snapshot_hash ? `<br /><small>来源快照 ${h(String(notice.source_snapshot_hash).slice(0, 12))}</small>` : ""}
      ${notice.platform_fulfillment_synced_waybill_no ? `<br /><small>已回传运单 ${h(notice.platform_fulfillment_synced_waybill_no)}${notice.platform_fulfillment_synced_at ? ` · ${h(formatTime(notice.platform_fulfillment_synced_at))}` : ""}</small>` : ""}
      ${notice.print_error ? `<br /><small>面单错误：${h(notice.print_error)}</small>` : ""}
      ${notice.platform_fulfillment_error ? `<br /><small>平台回传错误：${h(notice.platform_fulfillment_error)}</small>` : ""}
    </p>
  `).join("");
}

function flowStatusText(status) {
  return {
    done: "完成",
    active: "进行中",
    blocked: "阻断",
    pending: "等待",
    cancelled: "取消",
  }[status] || status || "等待";
}

function middleOrderStatusLabel(status) {
  return {
    CRM_APPROVED: "CRM 已审批",
    IMPORTED: "已进入中台",
    VALIDATING: "预审中",
    VALIDATION_BLOCKED: "预审阻断",
    VALIDATED: "预审通过",
    DELIVERY_NOTICE_READY: "发货预览待确认",
    OMS_PENDING: "OMS 待推送",
    OMS_RETRYING: "OMS 重试中",
    OMS_BLOCKED: "OMS 阻断",
    OMS_ACCEPTED: "OMS 已接收",
    PICKING: "拣货/出库中",
    SHIPPED: "已发货",
    FULFILLMENT_ARCHIVED: "一期履约归档",
    SIGNED: "已签收",
    FINANCE_CHECKING: "财务核验中",
    FINANCE_EXCEPTION: "财务异常",
    CLOSED: "已关闭",
    CANCELLED: "已取消",
    OUT_OF_SCOPE: "不在处理范围",
  }[status] || status || "未进入中台";
}

function deliveryNoticeStatusLabel(status) {
  return {
    Created: "已创建",
    Previewed: "已生成预览",
    Confirmed: "已确认",
    Pending: "待处理",
    Pushing: "推送中",
    Pushed: "已推送",
    Accepted: "已接收",
    Failed: "失败",
    Blocked: "阻断",
    Stale: "已过期",
    Cancelled: "已取消",
    NotRequested: "未请求",
    NotRequired: "无需处理",
  }[status] || status || "-";
}

function flowBadgeClass(status) {
  if (status === "CANCELLED") return "status-pill is-muted";
  if (["VALIDATION_BLOCKED", "OMS_BLOCKED", "FINANCE_EXCEPTION"].includes(status)) return "status-pill is-danger";
  if (["OMS_PENDING", "OMS_RETRYING", "PICKING", "VALIDATING"].includes(status)) return "status-pill is-warn";
  if (status) return "status-pill is-active";
  return "status-pill is-muted";
}

function renderCrmOrderFlow(flow = {}) {
  const order = flow.middle_order;
  const currentNode = $("#crm-order-flow-current");
  const badge = $("#crm-order-flow-badge");
  const alertNode = $("#crm-order-flow-alert");
  const flowNode = $("#crm-order-flow");
  const evidenceNode = $("#crm-order-flow-evidence");
  if (!currentNode || !badge || !flowNode || !evidenceNode) return;

  if (!order) {
    currentNode.textContent = "该 CRM 订单已同步，但尚未进入中台预审流程。";
    badge.className = "status-pill is-muted";
    badge.textContent = "未进入中台";
  } else {
    currentNode.textContent = `${middleOrderStatusLabel(order.status)} · 中台订单 ${order.order_no}`;
    badge.className = flowBadgeClass(order.status);
    badge.textContent = middleOrderStatusLabel(order.status);
  }
  if (alertNode) {
    const alert = flow.risk_alert;
    alertNode.hidden = !alert;
    alertNode.innerHTML = alert
      ? `<strong>${h(alert.exception_type || "CRM 变更待处理")}</strong>
          <p>${h(alert.summary || "")}</p>
          <small>快照 V${h(alert.current_snapshot_version || "-")} → V${h(alert.latest_snapshot_version || "-")} · 发货预览 ${h(alert.preview_status || "-")} · OMS job ${h(alert.oms_job_status || "-")}${alert.oms_status ? ` · OMS 状态 ${h(alert.oms_status)}` : ""}${alert.oms_order_no ? ` · OMS ${h(alert.oms_order_no)}` : ""}</small>
          <ul>${(alert.next_actions || []).map((item) => `<li>${h(item)}</li>`).join("")}</ul>`
      : "";
  }

  flowNode.innerHTML = (flow.steps || [])
    .map((step) => `
      <div class="flow-step is-${h(step.status || "pending")}">
        <div class="flow-step-dot"></div>
        <div>
          <div class="flow-step-title">
            <strong>${h(step.label)}</strong>
            <span>${h(flowStatusText(step.status))}</span>
          </div>
          <p>${h(step.description || "")}</p>
          ${step.time ? `<small>${h(formatTime(step.time))}</small>` : ""}
        </div>
      </div>
    `)
    .join("");

  const notices = order?.delivery_notices || [];
  const exceptions = flow.exceptions || [];
  const jobs = flow.processing_jobs || [];
  const audits = flow.audit_events || [];
  const snapshots = flow.crm_snapshots || [];
  const snapshotDiff = flow.snapshot_diff || {};
  evidenceNode.innerHTML = `
    <div>
      <h4>CRM 快照</h4>
      ${renderCrmSnapshots(snapshots)}
    </div>
    <div>
      <h4>快照差异</h4>
      ${renderSnapshotDiff(snapshotDiff)}
    </div>
    <div>
      <h4>发货通知</h4>
      ${renderDeliveryNoticeEvidence(notices)}
    </div>
    <div>
      <h4>异常</h4>
      ${
        exceptions.length
          ? exceptions.map((item) => `<p><strong>${h(item.exception_type)}</strong> · ${h(item.status)} · ${h(item.summary || item.severity || "")}</p>`).join("")
          : `<p class="muted-line">暂无异常</p>`
      }
    </div>
    <div>
      <h4>队列</h4>
      ${
        jobs.length
          ? jobs.map((job) => `<p><strong>${h(job.job_type)}</strong> · ${h(job.status)} · ${h(formatTime(job.updated_at || job.created_at))}</p>`).join("")
          : `<p class="muted-line">暂无关联队列</p>`
      }
    </div>
    <div>
      <h4>审计</h4>
      ${
        audits.length
          ? audits.slice(0, 5).map((event) => `<p><strong>${h(event.event_type)}</strong> · ${h(formatTime(event.created_at))}</p>`).join("")
          : `<p class="muted-line">暂无中台审计</p>`
      }
    </div>
  `;
}

function renderCrmOmsExtractionAlert(row) {
  const extraction = row?.raw?.oms_field_extraction || {};
  if (!extraction.manual_review_required) return "";
  const errors = Array.isArray(extraction.validation_errors) ? extraction.validation_errors : [];
  return `
    <div class="crm-extraction-alert">
      <strong>需人工审查确认</strong>
      <span>${h(errors.length ? errors.join("、") : "收货联系人、联系方式电话或收货地址未通过自动校验")}</span>
    </div>
  `;
}

function activateCrmOrderDetailTab(tabName = "order") {
  $("#crm-order-detail-tabs")?.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabName);
  });
  document.querySelectorAll("[id^='crm-order-detail-'][id$='-tab']").forEach((panel) => {
    const active = panel.id === `crm-order-detail-${tabName}-tab`;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
  if (tabName === "flow") {
    renderCrmOrderFlow(currentCrmOrderDetailFlow || {});
  }
}

function crmItemValue(item, keys) {
  for (const key of keys) {
    const value = item?.[key];
    if (value !== null && value !== undefined && value !== "") return value;
  }
  const raw = item?.raw || {};
  for (const key of keys) {
    const value = raw?.[key];
    if (value !== null && value !== undefined && value !== "") return value;
  }
  return "";
}

function renderCrmOrderProducts(items = [], currency = "") {
  if (!Array.isArray(items) || !items.length) {
    return `<div class="empty-note">CRM 订单产品未同步到本地，请先重试详情同步。</div>`;
  }
  return `
    <div class="crm-order-product-table">
      <div class="crm-order-product-head">
        <span>物料编码</span>
        <span>产品名称 / 规格</span>
        <span>数量</span>
        <span>单价</span>
        <span>总价</span>
        <span>折扣</span>
      </div>
      ${items
        .map((item) => {
          const sku = crmItemValue(item, ["sku_code"]);
          const skuStatus = item?.sku_match_status || "";
          const skuConfidence = item?.sku_match_confidence || "";
          const skuMeta = sku
            ? [skuStatus === "matched" ? "已匹配物料中心" : "", skuConfidence ? `置信度 ${skuConfidence}%` : ""].filter(Boolean).join(" · ")
            : "未匹配，需人工确认";
          const name = crmItemValue(item, ["product_name", "name", "产品名称", "商品名称", "货物名称"]);
          const spec = crmItemValue(item, ["specification", "model", "规格型号", "规格", "型号", "主要规格/详细配置"]);
          const quantity = crmItemValue(item, ["quantity", "qty", "数量"]);
          const unitPrice = crmItemValue(item, ["unit_price", "price", "销售单价", "单价"]);
          const lineAmount = crmItemValue(item, ["line_amount", "amount", "销售订单金额", "小计", "总价"]);
          const discount = crmItemValue(item, ["discount", "discount_amount", "折扣", "优惠金额"]);
          return `
            <div class="crm-order-product-row">
              <div><strong>${h(sku || "未匹配，需人工确认")}</strong><small>${h(skuMeta)}</small></div>
              <div><strong>${h(name || "-")}</strong><small>${h(spec || "未记录规格")}</small></div>
              <div>${h(quantity || "-")}</div>
              <div>${h(formatAmount(unitPrice, currency))}</div>
              <div>${h(formatAmount(lineAmount, currency))}</div>
              <div>${h(discount || "未记录")}</div>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

async function deleteCrmOrderLocal(id, orderNo = "该订单") {
  if (!id) return;
  const ok = window.confirm(
    `确认删除本地 CRM 订单“${orderNo}”？\n\n这只会删除本系统中的 CRM 镜像、订单产品、附件记录、快照、中台流程、发货预览、队列任务和相关异常，不会删除纷享销客线上的订单。删除后可重新同步并从头走流程。`
  );
  if (!ok) return;
  const result = await api(`/api/crm/orders/${id}`, { method: "DELETE" });
  const deleted = result.deleted || {};
  const total = Object.values(deleted).reduce((sum, value) => sum + Number(value || 0), 0);
  toast(`已删除本地订单：${result.crm_order_no || orderNo}，清理 ${total} 条关联记录`);
  if (currentCrmOrderDetailId === id) {
    closeCrmOrderDetail();
  }
  await refreshCrmOrders();
  await refreshExceptions();
  await refreshJobs();
}

async function openCrmOrderDetail(orderId) {
  currentCrmOrderDetailId = orderId;
  currentCrmOrderDetailFlow = null;
  const modal = $("#crm-order-detail-modal");
  modal.hidden = false;
  $("#crm-order-detail-title").textContent = "订单详情";
  $("#crm-order-detail-meta").textContent = "CRM 销售订单 · 正在加载";
  $("#crm-order-detail-summary").innerHTML = `<div class="empty-note">正在读取订单详情...</div>`;
  activateCrmOrderDetailTab("order");
  $("#crm-order-detail-fields").innerHTML = "";
  $("#crm-order-detail-delivery").innerHTML = "";
  $("#crm-order-detail-party").innerHTML = "";
  $("#crm-order-detail-products").innerHTML = "";
  $("#crm-order-detail-raw").textContent = "";

  try {
    const row = await api(`/api/crm/orders/${orderId}`);
    $("#crm-order-detail-title").textContent = row.crm_order_no || row.crm_order_id || "订单详情";
    $("#crm-order-detail-meta").innerHTML = `${h(row.source_system || "CRM")} · 同步时间 ${row.synced_at ? h(formatTime(row.synced_at)) : "未同步"} · ${h(row.crm_detail_message || "")}${
      ` <button class="button ghost mini" data-action="retry-crm-detail-sync" data-id="${h(row.id)}">强制重试详情同步</button>`
    } <button class="button warn mini" data-action="delete-crm-order-local" data-id="${h(row.id)}" data-no="${h(row.crm_order_no || row.crm_order_id || "")}">删除本地订单</button>`;
    $("#crm-order-detail-summary").innerHTML = [
      ["订单金额", formatAmountHtml(row.order_amount, row.currency), row.currency || "CNY"],
      ["已回款", formatAmountHtml(row.received_amount, row.currency), "CRM 回款金额"],
      ["待回款", formatAmountHtml(row.receivable_amount, row.currency), "订单金额 - 已回款"],
      ["生命周期", h(row.life_status || "-"), row.approval_status || "审批状态未记录"],
    ]
      .map(([label, value, hint]) => `<div class="metric"><span>${h(label)}</span><strong>${value}</strong><small>${h(hint)}</small></div>`)
      .join("");
    currentCrmOrderDetailFlow = row.flow || {};
    if ($("#crm-order-detail-tabs button[data-tab='flow']")?.classList.contains("active")) {
      renderCrmOrderFlow(currentCrmOrderDetailFlow);
    }
    $("#crm-order-detail-fields").innerHTML = [
      detailField("CRM 订单 ID", row.crm_order_id),
      detailField("销售订单编号", row.crm_order_no),
      detailField("商机", row.opportunity_name),
      detailField("下单日期", row.order_date),
      detailField("结算方式", row.settlement_method),
      detailField("币种", row.currency),
      detailField("订单金额", formatAmount(row.order_amount, row.currency)),
      detailField("已回款金额", formatAmount(row.received_amount, row.currency)),
      detailField("待回款金额", formatAmount(row.receivable_amount, row.currency)),
      detailField("开票金额", formatAmount(row.invoice_amount, row.currency)),
      detailField("商品金额", formatAmount(row.product_amount, row.currency)),
      detailField("生命周期", row.life_status),
      detailField("审批状态", row.approval_status),
    ].join("");
    $("#crm-order-detail-delivery").innerHTML = [
      renderCrmOmsExtractionAlert(row),
      detailField("物流状态", row.logistics_status),
      detailField("发货状态", listOnlyValue(row.shipment_status, row.crm_detail_status)),
      detailField("开票状态", listOnlyValue(row.invoice_status, row.crm_detail_status)),
      detailField("收货联系人", listOnlyValue(row.receipt_contact, row.crm_detail_status)),
      detailField("联系方式电话", listOnlyValue(row.receipt_phone, row.crm_detail_status)),
      detailField("收货地址", listOnlyValue(row.receipt_address, row.crm_detail_status)),
      detailField("交付日期", listOnlyValue(row.delivery_date, row.crm_detail_status)),
      detailFieldHtml("附件", renderCrmAttachments(row.attachments, row.attachment_files)),
      detailField("备注", listOnlyValue(row.remark, row.crm_detail_status)),
    ].join("");
    $("#crm-order-detail-party").innerHTML = [
      detailField("客户名称", row.customer_name),
      detailField("客户 ID", row.customer_id),
      detailField("销售", listOnlyValue(row.sales_user_name, row.crm_detail_status)),
      detailField("部门", row.owner_department),
      detailField("销售用户 ID", row.sales_user_id),
      detailField("商机 ID", row.opportunity_id),
    ].join("");
    $("#crm-order-detail-products").innerHTML = renderCrmOrderProducts(row.order_items, row.currency);
    $("#crm-order-detail-raw").textContent = JSON.stringify(row.raw || row, null, 2);
  } catch (error) {
    notifyError(error, ["CRM 订单", "读取订单详情失败"]);
    $("#crm-order-detail-meta").textContent = "CRM 销售订单 · 读取失败";
    $("#crm-order-detail-summary").innerHTML = `<div class="empty-note">读取失败：${h(error.message || "未知错误")}</div>`;
    currentCrmOrderDetailFlow = null;
    $("#crm-order-detail-fields").innerHTML = "";
    $("#crm-order-detail-delivery").innerHTML = "";
    $("#crm-order-detail-party").innerHTML = "";
    $("#crm-order-detail-products").innerHTML = "";
    $("#crm-order-detail-raw").textContent = "";
  }
}

function closeCrmOrderDetail() {
  $("#crm-order-detail-modal").hidden = true;
  currentCrmOrderDetailId = "";
  currentCrmOrderDetailFlow = null;
}

async function refreshOps() {
  const [usage, auditsPayload, backupsPayload] = await Promise.all([
    api("/api/storage/usage"),
    api(`/api/audit-events?${queryFromState(tableStates.audit)}`),
    api(`/api/backups?${queryFromState(tableStates.backups)}`),
  ]);
  const audits = normalizeListPayload(auditsPayload, tableStates.audit);
  const backups = normalizeListPayload(backupsPayload, tableStates.backups);
  setSelectOptions("#audit-filter-form [name=event_type]", audits.event_type_options || [], "全部事件", tableStates.audit.event_type);
  setSelectOptions("#audit-filter-form [name=related_object_type]", audits.related_object_type_options || [], "全部对象", tableStates.audit.related_object_type);
  setSelectOptions("#backups-filter-form [name=status]", backups.status_options || [], "全部状态", tableStates.backups.status);
  setSelectOptions("#backups-filter-form [name=backup_type]", backups.backup_type_options || [], "全部类型", tableStates.backups.backup_type);
  $("#storage-usage").innerHTML = `
    <div class="row">
      <div><strong>存储占用</strong><br /><small>${h(usage.attachment_files)} 个附件文件</small></div>
      <div><small>已用</small><br />${h(formatBytes(usage.attachment_bytes))}</div>
      <div><small>预算</small><br />${h(formatBytes(usage.storage_budget_bytes))}</div>
      <div><small>${h(Math.round((usage.attachment_bytes / Math.max(usage.storage_budget_bytes, 1)) * 100))}%</small></div>
    </div>`;
  $("#audit-list").innerHTML =
    (audits.items || [])
      .map(
        (row) => `
        <div class="row">
          <div><strong>${h(row.event_type)}</strong><br /><small>${h(row.actor)}</small></div>
          <div><small>${h(row.related_object_type)}</small><br />${h(row.related_object_id)}</div>
          <div><small>${h(formatTime(row.created_at))}</small></div>
          <div><small>${h(JSON.stringify(row.detail).slice(0, 160))}</small></div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无审计事件</div></div>`;
  renderListPagination("#audit-pagination", "audit", audits);
  $("#backup-list").innerHTML =
    (backups.items || [])
      .map(
        (row) => `
        <div class="row">
          <div><strong>${h(row.backup_type)}</strong><br /><small>${h(row.id)}</small></div>
          <div><small>状态</small><br />${h(row.status)}</div>
          <div><small>${h(formatTime(row.created_at))}</small></div>
          <div><small>${h(row.storage_ref)}</small></div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无备份记录</div></div>`;
  renderListPagination("#backups-pagination", "backups", backups);
}

function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value || 0);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function summarizeExceptionDetail(detail) {
  if (!detail || typeof detail !== "object") return "";
  const parts = [];
  if (detail.exception?.summary) parts.push(String(detail.exception.summary));
  if (detail.message) parts.push(String(detail.message));
  if (detail.action_hint) parts.push(`处理建议：${detail.action_hint}`);
  if (detail.source_mail_id) parts.push(`来源邮件：${detail.source_mail_id}`);
  if (detail.exception_types?.length) parts.push(`异常：${detail.exception_types.join("、")}`);
  if (detail.exceptions?.length && !detail.exception_types?.length) {
    parts.push(`异常：${detail.exceptions.map((item) => item.exception_type).filter(Boolean).join("、")}`);
  }
  if (detail.missing_fields?.length) parts.push(`缺失：${detail.missing_fields.join("、")}`);
  if (detail.risk_flags?.length) parts.push(`风险：${detail.risk_flags.join("、")}`);
  if (detail.requirement_id) parts.push(`需求：${detail.requirement_id}`);
  return parts.join("；") || JSON.stringify(detail);
}

function exceptionDiagnosis(detail = {}) {
  return detail && typeof detail === "object" ? detail.ai_diagnosis || null : null;
}

function exceptionMaterials(detail = {}) {
  const validation = detail && typeof detail === "object" ? detail.validation || {} : {};
  return Array.isArray(validation.missing_materials) ? validation.missing_materials : [];
}

function slaPillClass(status) {
  if (status === "overdue") return "is-danger";
  if (status === "due_soon") return "is-warn";
  if (status === "resolved") return "is-active";
  return "is-muted";
}

function statusPillClass(status) {
  if (status === "Resolved" || status === "Closed") return "is-active";
  if (status === "Assigned") return "is-warn";
  if (status === "Open") return "is-danger";
  return "is-muted";
}

function renderExceptionActions(row) {
  const id = h(row.id);
  const isClosed = ["Resolved", "Closed"].includes(row.status);
  const highRisk = row.requires_confirmation ? "true" : "false";
  return `
    <button class="button ghost" data-action="view-exception-context" data-id="${id}">上下文</button>
    <button class="button ghost" data-action="assign-exception" data-id="${id}">分派</button>
    <button class="button ghost" data-action="diagnose-exception" data-id="${id}">诊断</button>
    ${
      isClosed
        ? `<button class="button ghost" data-action="reopen-exception" data-id="${id}">重开</button>`
        : `<button class="button ghost" data-action="resolve-exception" data-id="${id}" data-high-risk="${highRisk}">关闭</button>`
    }
    <button class="button ghost" data-action="patch-exception" data-id="${id}">补字段</button>
  `;
}

function diagnoseExceptionStream(id) {
  if (!window.EventSource) return Promise.resolve(false);
  return new Promise((resolve, reject) => {
    const source = new EventSource(`/api/exceptions/${encodeURIComponent(id)}/diagnose-stream`);
    let completed = false;
    source.addEventListener("loading", (event) => {
      const data = JSON.parse(event.data || "{}");
      toast(data.message || "AI 诊断启动");
    });
    source.addEventListener("partial", (event) => {
      const data = JSON.parse(event.data || "{}");
      toast(data.message || "AI 诊断处理中");
    });
    source.addEventListener("done", () => {
      completed = true;
      source.close();
      toast("诊断已生成");
      resolve(true);
    });
    source.addEventListener("error", (event) => {
      source.close();
      if (completed) return;
      const data = event.data ? JSON.parse(event.data) : {};
      reject(new Error(data.message || "AI 流式诊断失败"));
    });
  });
}

function omsReplayNoticeCandidate(order = {}) {
  const notices = Array.isArray(order.delivery_notices) ? order.delivery_notices : [];
  return notices.find((notice) => ["Blocked", "Retrying"].includes(notice.status)) || notices.find((notice) => notice.id) || null;
}

function renderExceptionContextPanel(data = {}) {
  const exception = data.exception || {};
  const order = data.middle_order || {};
  const crm = data.crm_order || {};
  const diagnosis = data.diagnosis || {};
  const jobs = data.processing_jobs || [];
  const audits = data.audit_events || [];
  const feedback = data.feedback || [];
  const actions = data.next_actions || [];
  const snapshotDiff = data.snapshot_diff || {};
  const replayGate = data.oms_replay || {};
  return `
    <div class="exception-context-grid">
      <section>
        <h3>${h(exception.exception_type || "异常")}</h3>
        <p>${h(summarizeExceptionDetail(exception.detail || {}))}</p>
        <div class="exception-materials">
          <span>${h(exception.status || "-")}</span>
          <span>${h(exception.severity || "-")}</span>
          <span>${h(exception.sla_status || "-")}</span>
        </div>
      </section>
      <section>
        <h3>${crm.crm_order_no ? `<a class="exception-order-link" style="font-weight: 600; color: var(--accent); cursor: pointer;" onclick="navigateToCrmOrder('${h(crm.crm_order_no)}')">${h(order.order_no || crm.crm_order_no)}</a>` : h(order.order_no || crm.crm_order_no || "订单")}</h3>
        <p>${h(order.customer_name || crm.customer_name || "-")} · ${h(order.status || "-")}</p>
        <small>${h(order.channel_code || "")}${order.shop_code ? ` · ${h(order.shop_code)}` : ""}${order.platform_order_no ? ` · ${h(order.platform_order_no)}` : ""}</small>
      </section>
      <section>
        <h3>AI 诊断</h3>
        ${
          diagnosis.summary
            ? `<p>${h(diagnosis.summary)}</p><small>责任建议：${h(diagnosis.suggested_owner || "-")} · 置信度 ${h(diagnosis.confidence ?? "-")}</small>`
            : `<p class="muted-line">暂无诊断</p>`
        }
        <div class="exception-context-actions">
          <button class="button ghost" data-action="diagnosis-feedback" data-id="${h(exception.id)}" data-feedback="accepted">采纳</button>
          <button class="button ghost" data-action="diagnosis-feedback" data-id="${h(exception.id)}" data-feedback="modified">修改</button>
          <button class="button ghost" data-action="diagnosis-feedback" data-id="${h(exception.id)}" data-feedback="rejected">驳回</button>
        </div>
      </section>
    </div>
    ${
      diagnosis.address_correction
        ? `
        <div class="exception-context-section address-correction-card" style="margin-bottom: 20px; background: var(--surface-soft); padding: 16px; border-radius: 8px; border: 1px dashed var(--warn);">
          <h3 style="display: flex; align-items: center; gap: 8px; color: var(--warn); font-size: 15px; margin-bottom: 8px;">
            <span>💡 AI 地址智能修复建议</span>
          </h3>
          <p class="correction-reason" style="margin: 4px 0 12px; font-size: 13px; color: var(--ink);">
            <strong>修正原因：</strong>${h(diagnosis.address_correction.reason || "智能分析建议修正。")}
          </p>
          <table class="correction-comparison-table" style="width: 100%; border-collapse: collapse; margin-bottom: 15px;">
            <thead>
              <tr style="border-bottom: 2px solid var(--line); font-size: 13px; color: var(--muted);">
                <th style="padding: 6px 12px; text-align: left; font-weight: 600;">字段</th>
                <th style="padding: 6px 12px; text-align: left; font-weight: 600;">原始值</th>
                <th style="padding: 6px 12px; text-align: left; font-weight: 600;">修正后建议值</th>
              </tr>
            </thead>
            <tbody style="font-size: 13px;">
              ${
                diagnosis.address_correction.receipt_address
                  ? `<tr style="border-bottom: 1px solid var(--line);">
                      <td style="padding: 8px 12px; font-weight: 500;">收货地址</td>
                      <td style="padding: 8px 12px; text-decoration: line-through; color: var(--muted);">${h(order.receipt_address || crm.receipt_address || "空")}</td>
                      <td style="padding: 8px 12px; font-weight: 600; color: var(--warn);">${h(diagnosis.address_correction.receipt_address)}</td>
                    </tr>`
                  : ""
              }
              ${
                diagnosis.address_correction.receipt_contact
                  ? `<tr style="border-bottom: 1px solid var(--line);">
                      <td style="padding: 8px 12px; font-weight: 500;">收货人</td>
                      <td style="padding: 8px 12px; text-decoration: line-through; color: var(--muted);">${h(order.receipt_contact || crm.receipt_contact || "空")}</td>
                      <td style="padding: 8px 12px; font-weight: 600; color: var(--warn);">${h(diagnosis.address_correction.receipt_contact)}</td>
                    </tr>`
                  : ""
              }
              ${
                diagnosis.address_correction.receipt_phone
                  ? `<tr style="border-bottom: 1px solid var(--line);">
                      <td style="padding: 8px 12px; font-weight: 500;">联系电话</td>
                      <td style="padding: 8px 12px; text-decoration: line-through; color: var(--muted);">${h(order.receipt_phone || crm.receipt_phone || "空")}</td>
                      <td style="padding: 8px 12px; font-weight: 600; color: var(--warn);">${h(diagnosis.address_correction.receipt_phone)}</td>
                    </tr>`
                  : ""
              }
            </tbody>
          </table>
          <div style="display: flex; gap: 10px;">
            <button class="button success" style="background-color: #16a34a; color: white;" data-action="apply-address-correction" data-exception-id="${h(exception.id)}">一键应用地址修复</button>
          </div>
        </div>
        `
        : ""
    }
    <div class="exception-context-section">
      <h3>下一步</h3>
      <ul>${actions.map((item) => `<li>${h(item)}</li>`).join("") || "<li>暂无建议</li>"}</ul>
      ${
        replayGate.ready && replayGate.notice_id
          ? `<div class="exception-context-actions">
              <button class="button" data-action="replay-oms-notice" data-id="${h(replayGate.notice_id)}" data-exception-id="${h(exception.id || "")}" data-notice-no="${h(replayGate.notice_no || "")}">填写修复证据并重放 OMS</button>
            </div>`
          : replayGate.reason
            ? `<p class="muted-line">OMS 重放暂不可用：${h(replayGate.reason)}</p>`
          : ""
      }
    </div>
    <div class="exception-context-section">
      <h3>CRM 快照差异</h3>
      ${renderSnapshotDiff(snapshotDiff)}
    </div>
    <div class="exception-context-grid">
      <section>
        <h3>队列</h3>
        ${jobs.slice(0, 6).map((job) => `<p><strong>${h(job.job_type)}</strong> · ${h(job.status)}<br /><small>${h(job.error_message || formatTime(job.created_at) || "")}</small></p>`).join("") || `<p class="muted-line">暂无关联队列</p>`}
      </section>
      <section>
        <h3>审计</h3>
        ${audits.slice(0, 6).map((event) => `<p><strong>${h(event.event_type)}</strong><br /><small>${h(event.actor || "")} · ${h(formatTime(event.created_at))}</small></p>`).join("") || `<p class="muted-line">暂无关联审计</p>`}
      </section>
      <section>
        <h3>反馈</h3>
        ${feedback.slice(-5).reverse().map((item) => `<p><strong>${h(item.feedback)}</strong> · ${h(item.actor || "")}<br /><small>${h(item.note || "")}</small></p>`).join("") || `<p class="muted-line">暂无反馈</p>`}
      </section>
    </div>
  `;
}

async function openExceptionContext(id) {
  $("#exception-context-modal").hidden = false;
  $("#exception-context-title").textContent = "异常上下文";
  $("#exception-context-body").innerHTML = `<div class="empty-note">正在读取异常上下文...</div>`;
  try {
    const data = await api(`/api/exceptions/${id}/context`);
    $("#exception-context-title").textContent = data.exception?.exception_type || "异常上下文";
    $("#exception-context-body").innerHTML = renderExceptionContextPanel(data);
  } catch (error) {
    $("#exception-context-body").innerHTML = `<div class="empty-note">读取失败：${h(messageFromError(error))}</div>`;
  }
}

function formatTime(value) {
  if (!value) return "";
  let normalized = value;
  if (typeof value === "string") {
    const text = value.trim();
    const looksLikeDateTime = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(text);
    const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text);
    if (looksLikeDateTime && !hasTimezone) {
      normalized = `${text.replace(" ", "T")}Z`;
    }
  }
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}

function formatAmount(value, currency = "") {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return `${value}${currency ? ` ${currency}` : ""}`;
  return `${number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency ? ` ${currency}` : ""}`;
}

function formatAmountHtml(value, currency = "") {
  const text = formatAmount(value, currency);
  if (!currency || text === "-") return h(text);
  const suffix = ` ${currency}`;
  if (!text.endsWith(suffix)) return h(text);
  return `${h(text.slice(0, -suffix.length))}<span class="metric-unit">${h(currency)}</span>`;
}

function workflowStatusLabel(status) {
  return {
    done: "已完成",
    current: "进行中",
    pending: "待处理",
  }[status] || status || "";
}

function compactDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (detail.subject) return detail.subject;
  if (detail.question) return detail.reply ? `${detail.question} / ${detail.reply}` : detail.question;
  if (detail.task_no) return detail.task_no;
  if (detail.missing_fields?.length) return `缺失：${detail.missing_fields.join("、")}`;
  if (detail.reasons?.length) return detail.reasons.join("、");
  return JSON.stringify(detail);
}

async function openWorkflow(id, taskType = "production") {
  const endpoint = taskType === "logistics" ? `/api/logistics-tasks/${id}/workflow` : `/api/tasks/${id}/workflow`;
  const data = await api(endpoint);
  const task = data.task || {};
  $("#workflow-task-no").textContent = task.task_no || (taskType === "logistics" ? "物流任务" : "生产任务");
  $("#workflow-title").textContent = `${task.customer_name || "未识别客户"} · ${task.product_summary || "未识别物料"}`;
  $("#workflow-steps").innerHTML = (data.steps || [])
    .map(
      (step) => `
        <div class="workflow-step ${h(step.status)}">
          <strong>${h(step.title)}</strong>
          <small>${h(workflowStatusLabel(step.status))}</small>
          <small>${h(step.detail || "")}</small>
          <small>${h(formatTime(step.created_at))}</small>
        </div>`
    )
    .join("");
  $("#workflow-timeline").innerHTML =
    (data.timeline || [])
      .map(
        (item) => `
          <div class="timeline-item">
            <div><small>${h(formatTime(item.created_at))}</small></div>
            <div><strong>${h(item.title)}</strong><br /><small>${h(item.status || item.type)}</small></div>
            <div><small>${h(compactDetail(item.detail))}</small></div>
          </div>`
      )
      .join("") || `<div class="timeline-item"><div>暂无流转记录</div></div>`;
  $("#workflow-modal").hidden = false;
}

function closeWorkflow() {
  $("#workflow-modal").hidden = true;
}

async function openWeeklyReportPreview() {
  const data = await api("/api/reports/weekly/preview");
  const periods = data.periods || {};
  const reportingPeriod = data.reporting_period || periods.week || {};
  const weekStats = periods.week?.task_stats || {};
  const monthStats = periods.month?.task_stats || {};
  $("#weekly-preview-time").textContent = `生成时间：${h(data.generated_at_label || formatTime(data.generated_at))}`;
  $("#weekly-preview-title").textContent = data.subject || "商务生产任务单周报";
  $("#weekly-preview-meta").innerHTML = `
    <div><small>上报周期</small><strong>${h(reportingPeriod.range_label || "未识别")}</strong></div>
    <div><small>主送</small><strong>${h((data.to || []).join(", ") || "未配置")}</strong></div>
    <div><small>抄送</small><strong>${h((data.cc || []).join(", ") || "无")}</strong></div>
    <div><small>本周需求</small><strong>${h(weekStats.demand_total ?? 0)}</strong></div>
    <div><small>本月已确认</small><strong>${h(monthStats.confirmed_total ?? 0)}</strong></div>
  `;
  $("#weekly-preview-body").textContent = data.body || "";
  $("#weekly-preview-modal").hidden = false;
}

function closeWeeklyReportPreview() {
  $("#weekly-preview-modal").hidden = true;
}

async function refreshExceptions() {
  const data = normalizeListPayload(await api(`/api/exceptions?${queryFromState(tableStates.exceptions)}`), tableStates.exceptions);
  const rows = data.items || [];
  setExceptionStatusOptions(data.status_options || []);
  setSelectOptions("#exceptions-filter-form [name=severity]", data.severity_options || [], "全部级别", tableStates.exceptions.severity);
  setSelectOptions("#exceptions-filter-form [name=exception_type]", data.exception_type_options || [], "全部类型", tableStates.exceptions.exception_type);
  $("#exceptions-list").innerHTML =
    rows
      .map(
        (row) => {
          const diagnosis = exceptionDiagnosis(row.detail);
          const materials = exceptionMaterials(row.detail);
          const crmOrderNo = row.crm_order_no || row.order_no || "";
          const orderNoDisplay = crmOrderNo
            ? `<a class="exception-order-link" style="font-weight: 600; color: var(--accent); cursor: pointer;" onclick="navigateToCrmOrder('${h(crmOrderNo)}')">${h(crmOrderNo)}</a>`
            : `<small>${h(row.id)}</small>`;
          return `
          <div class="row exception-row">
            <div>
              <strong>${h(row.exception_type)}</strong><br />
              ${orderNoDisplay}<br />
              <span class="status-pill ${statusPillClass(row.status)}">${h(row.status)}</span>
              <span class="status-pill ${slaPillClass(row.sla_status)}">SLA ${h(row.sla_status || "none")}</span>
            </div>
            <div>
              <small>级别 / 负责人</small><br />
              <strong>${h(row.severity)}</strong><br />
              <small>${h(row.assignee || "未分派")}</small>
            </div>
            <div>
              <small>截止 / 更新</small><br />
              <small>${h(formatTime(row.due_at) || "未设置")}</small><br />
              <small>${h(formatTime(row.updated_at || row.created_at))}</small>
            </div>
            <div>
              <small>${h(summarizeExceptionDetail(row.detail))}</small>
              ${
                materials.length
                  ? `<div class="exception-materials">${materials.slice(0, 4).map((item) => `<span>${h(item)}</span>`).join("")}</div>`
                  : ""
              }
              ${
                diagnosis
                  ? `<div class="exception-diagnosis">
                      <strong>${h(diagnosis.summary || "诊断建议")}</strong>
                      <small>责任建议：${h(diagnosis.suggested_owner || "-")} · 置信度 ${h(diagnosis.confidence ?? "-")}</small>
                      <ul>${(diagnosis.recommended_actions || []).slice(0, 3).map((item) => `<li>${h(item)}</li>`).join("")}</ul>
                    </div>`
                  : `<div class="exception-diagnosis is-empty"><small>暂无诊断，点击“诊断”生成处理建议</small></div>`
              }
              <div class="actions row-actions">
                ${renderExceptionActions(row)}
              </div>
            </div>
          </div>`;
        }
      )
      .join("") || `<div class="row"><div>暂无异常</div></div>`;
  renderListPagination("#exceptions-pagination", "exceptions", data);
}

async function loadTemplate() {
  const template = await api("/api/templates/production-task");
  $("#template-form [name=subject_template]").value = template.subject_template;
  $("#template-form [name=body_template]").value = template.body_template;
}

function renderCrmOrderSummary(summary = {}, v2Summary = {}) {
  const node = $("#crm-orders-summary");
  if (!node) return;
  const lastRun = summary.last_run || {};
  const labels = [
    ["订单总数", h(summary.total_orders || 0), "已去重入库的 CRM 销售订单"],
    ["中台订单", h(v2Summary.total_orders || 0), `STP ${h(v2Summary.stp_rate || 0)}% · 异常 ${h(v2Summary.open_exceptions || 0)}`],
    ["订单金额", formatAmountHtml(summary.total_order_amount, "CNY"), "当前库内销售订单金额合计"],
    ["已回款", formatAmountHtml(summary.total_received_amount, "CNY"), "CRM 已回款金额合计"],
    ["待回款", formatAmountHtml(summary.total_receivable_amount, "CNY"), "订单金额减已回款的估算值"],
    ["最近同步", h(lastRun.status || "-"), lastRun.finished_at ? formatTime(lastRun.finished_at) : "尚无同步记录"],
  ];
  node.innerHTML = labels
    .map(([label, value, hint]) => `<div class="metric"><span>${h(label)}</span><strong>${value}</strong><small>${h(hint)}</small></div>`)
    .join("");
}

function renderCrmSyncRuns(runs = []) {
  const node = $("#crm-sync-runs-list");
  if (!node) return;
  node.innerHTML = runs.length
    ? runs
        .map(
          (run) => {
            const detail = run.detail || {};
            const stage = detail.stage ? `${detail.stage}${detail.stage_at ? ` · ${formatTime(detail.stage_at)}` : ""}` : "";
            return `
            <div class="row crm-sync-run-row">
              <div><strong>${h(run.status || "")}</strong><br /><small>${h(run.trigger || "")} · ${h(formatTime(run.started_at))}</small>${stage ? `<br /><small>${h(stage)}</small>` : ""}</div>
              <div><small>合计 ${h(run.total_count || 0)} / 新增 ${h(run.created_count || 0)} / 更新 ${h(run.updated_count || 0)}</small><br /><small>未变 ${h(run.unchanged_count || 0)}</small></div>
              <div><small>${h(run.error_message || "")}</small></div>
            </div>
          `;
          }
        )
        .join("")
    : `<div class="row"><div>暂无同步记录</div></div>`;
}

function crmContactConfidenceHtml(row = {}) {
  const confidence = normalizePercent(row.contact_extraction_confidence);
  const needsReview = Boolean(row.contact_extraction_manual_review_required);
  const pillClass = needsReview ? "is-danger" : confidence === null ? "is-muted" : confidence >= 80 ? "is-active" : "is-warn";
  const label = confidence === null ? "未识别" : `${confidence}%`;
  const suffix = needsReview ? " · 人工确认" : "";
  return `<span class="status-pill crm-contact-confidence ${pillClass}" title="LLM 从附件识别收货联系人、电话、地址的置信度">LLM ${h(label)}${h(suffix)}</span>`;
}

async function refreshCrmOrders() {
  let listPayload, summary, v2Summary;
  try {
    [listPayload, summary, v2Summary] = await Promise.all([
      api(`/api/crm/orders?${queryFromState(tableStates.crmOrders)}`),
      api("/api/crm/sync/summary"),
      api("/api/v2/order-dashboard"),
    ]);
  } catch (error) {
    notifyError(error, ["CRM 订单", "列表刷新失败"]);
    return;
  }
  const data = normalizeListPayload(listPayload, tableStates.crmOrders);
  renderCrmOrderSummary(data.summary || summary || {}, v2Summary || {});
  renderCrmSyncRuns((summary && summary.runs) || []);
  setSelectOptions("#crm-orders-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.crmOrders.status);
  const rows = data.items || [];
  $("#crm-orders-list").innerHTML = rows.length
    ? rows
        .map(
          (row) => `
            <div class="row crm-order-row clickable-row" data-crm-order-id="${h(row.id)}" role="button" tabindex="0" aria-label="查看订单 ${h(row.crm_order_no || row.crm_order_id || "详情")} 的完整流程">
              <div>
                <strong>${h(row.crm_order_no || row.crm_order_id || "未编号")}</strong><br />
                <small>${h(row.customer_name || "未识别客户")}</small>
              </div>
              <div>
                <strong>${h(row.opportunity_name || "-")}</strong><br />
                <small>${h(row.sales_user_name || "")}${row.owner_department ? ` · ${h(row.owner_department)}` : ""}</small>
              </div>
              <div>
                <strong>${h(formatAmount(row.order_amount, row.currency))}</strong><br />
                <small>回款 ${h(formatAmount(row.received_amount, row.currency))}</small>
              </div>
              <div>
                <strong>${h(row.order_date || "-")}</strong><br />
                <small>${h(row.life_status || "")}${row.approval_status ? ` · ${h(row.approval_status)}` : ""}</small>
              </div>
              <div>
                <small>${h(row.synced_at ? formatTime(row.synced_at) : "")}</small><br />
                ${crmContactConfidenceHtml(row)}
                <br />
                <button class="button ghost compact-action" type="button" data-action="view-crm-order" data-id="${h(row.id)}">详情</button>
                <button class="button ghost compact-action" type="button" data-action="queue-crm-v2" data-id="${h(row.id)}">入中台</button>
                <button class="button ghost compact-action" type="button" data-action="process-crm-v2" data-id="${h(row.id)}">立即预审</button>
                <button class="button warn compact-action" type="button" data-action="delete-crm-order-local" data-id="${h(row.id)}" data-no="${h(row.crm_order_no || row.crm_order_id || "")}">删除</button>
              </div>
            </div>
          `
        )
        .join("")
    : `<div class="row"><div>暂无 CRM 订单</div></div>`;
  renderListPagination("#crm-orders-pagination", "crmOrders", data);
}

async function refreshAll() {
  await refreshDashboard();
  const refreshers = [
    refreshSkills(),
    refreshDepartments(),
    refreshLogisticsDepartments(),
    refreshTasks(),
    refreshLogisticsTasks(),
    refreshOutbound(),
    refreshExceptions(),
    refreshInitialReviewRules(),
    refreshV2ReviewRules(),
    refreshWorkflowRules(),
    refreshConfig(),
    refreshWeeklyReportRecipients(),
    refreshJobs(),
    refreshIntegrationEvents(),
    refreshAgentRuns(),
    refreshModelCalls(),
    refreshAttachments(),
    refreshMails(),
    refreshOps(),
    loadTemplate(),
    refreshProductsSpu(),
    refreshProductsSku(),
    refreshProductsInventory(),
    refreshProductsPricing(),
    refreshProductsPromotions(),
    refreshProductReviewReadiness(),
    refreshCrmOrders(),
  ];
  const results = await Promise.allSettled(refreshers);
  const failed = results.find((result) => result.status === "rejected");
  if (failed) {
    notifyError(failed.reason, ["系统", "局部刷新失败"]);
  }
}

$("#crm-orders-list")?.addEventListener("click", async (event) => {
  if (event.target.closest("button, a, input, select, textarea")) return;
  const row = event.target.closest("[data-crm-order-id]");
  if (!row) return;
  await guardedAction(["CRM 订单", "查看完整流程"], async () => {
    await openCrmOrderDetail(row.dataset.crmOrderId);
  });
});

$("#crm-orders-list")?.addEventListener("keydown", async (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  const row = event.target.closest("[data-crm-order-id]");
  if (!row) return;
  event.preventDefault();
  await guardedAction(["CRM 订单", "查看完整流程"], async () => {
    await openCrmOrderDetail(row.dataset.crmOrderId);
  });
});

const defaultTableStates = JSON.parse(JSON.stringify(tableStates));
const tableRefreshers = {
  workflows: refreshWorkflowRules,
  departments: refreshDepartments,
  logisticsDepartments: refreshLogisticsDepartments,
  logisticsTasks: refreshLogisticsTasks,
  mails: refreshMails,
  outbound: refreshOutbound,
  exceptions: refreshExceptions,
  jobs: refreshJobs,
  integrationEvents: refreshIntegrationEvents,
  agentRuns: refreshAgentRuns,
  modelCalls: refreshModelCalls,
  attachments: refreshAttachments,
  audit: refreshOps,
  backups: refreshOps,
  reviewRules: refreshV2ReviewRules,
  productsSpu: refreshProductsSpu,
  productsSku: refreshProductsSku,
  productsPricing: refreshProductsPricing,
  productsInventory: refreshProductsInventory,
  productsFinishedInventory: refreshProductsFinishedInventory,
  productsPromotions: refreshProductsPromotions,
  productsReview: refreshProductReviewReadiness,
  crmOrders: refreshCrmOrders,
};

const clearListConfigs = {
  exceptions: { label: "异常列表", endpoint: "/api/exceptions/clear" },
  jobs: { label: "入库队列", endpoint: "/api/jobs/clear" },
  attachments: { label: "附件列表", endpoint: "/api/attachments/clear" },
  audit: { label: "审计列表", endpoint: "/api/audit-events/clear" },
  backups: { label: "备份列表", endpoint: "/api/backups/clear" },
};

async function refreshTable(key) {
  const refresher = tableRefreshers[key];
  if (refresher) await refresher();
}

async function clearManagedList(key) {
  const config = clearListConfigs[key];
  if (!config) return;
  const adminPassword = window.prompt(`请输入管理员密码确认清空${config.label}。此操作不可恢复。`);
  if (adminPassword === null) return;
  if (!adminPassword.trim()) {
    toast("管理员密码不能为空");
    return;
  }
  const ok = window.confirm(`确认清空${config.label}？`);
  if (!ok) return;
  try {
    const result = await api(config.endpoint, {
      method: "POST",
      body: JSON.stringify({ admin_password: adminPassword }),
    });
    const state = tableStates[key];
    if (state) state.page = 1;
    toast(`已清空 ${result.cleared || 0} 条${config.label}记录`);
    await refreshTable(key);
  } catch (error) {
    notifyError(error, [config.label, "清空失败"]);
  }
}

document.querySelectorAll("[data-table-filter]").forEach((form) => {
  const key = form.dataset.tableFilter;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const state = tableStates[key];
    if (!state) return;
    const data = new FormData(form);
    for (const field of form.querySelectorAll("[name]")) {
      state[field.name] = String(data.get(field.name) || "").trim();
    }
    state.page = 1;
    if (key === "productsInventory") syncFinishedInventoryFilters(true);
    await refreshTable(key);
  });
  const reset = form.querySelector("[data-filter-reset]");
  if (reset) {
    reset.addEventListener("click", async () => {
      const state = tableStates[key];
      if (!state) return;
      const pageSize = state.page_size;
      Object.assign(state, defaultTableStates[key] || {}, { page: 1, page_size: pageSize });
      if (key === "productsInventory") Object.assign(tableStates.productsFinishedInventory, defaultTableStates.productsFinishedInventory || {}, { page: 1 });
      form.reset();
      await refreshTable(key);
    });
  }
});

document.querySelectorAll("[data-clear-list]").forEach((button) => {
  button.addEventListener("click", async () => {
    await clearManagedList(button.dataset.clearList);
  });
});

document.querySelectorAll("[data-table-pagination]").forEach((pagination) => {
  const key = pagination.dataset.tablePagination;
  pagination.addEventListener("click", async (event) => {
    const target = event.target.closest("button[data-page]");
    if (!target || target.disabled || !tableStates[key]) return;
    tableStates[key].page = Number(target.dataset.page || 1);
    await refreshTable(key);
  });
  pagination.addEventListener("change", async (event) => {
    if (!event.target.matches("[data-page-size]") || !tableStates[key]) return;
    tableStates[key].page_size = Number(event.target.value || 10);
    tableStates[key].page = 1;
    await refreshTable(key);
  });
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(form.entries())),
      skipAuthRedirect: true,
    });
    showApp(data);
    toast("已登录");
    await refreshAll();
  } catch (error) {
    toast(error.message || "登录失败");
  }
});

const demoList = $("#demo-account-list");
if (demoList) {
  demoList.addEventListener("click", (event) => {
    const li = event.target.closest("li");
    if (!li) return;
    const username = li.dataset.user;
    const password = li.dataset.pass;
    const usernameInput = $("#login-username");
    const passwordInput = $("#login-password");
    if (usernameInput) usernameInput.value = username;
    if (passwordInput) passwordInput.value = password;
  });
}

$("#logout").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST", skipAuthRedirect: true });
  } finally {
    showLogin();
    toast("已退出");
  }
});

$("#order-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await api("/api/demo/order", {
    method: "POST",
    body: JSON.stringify(Object.fromEntries(form.entries())),
  });
  toast("已生成任务单草稿");
  await refreshAll();
});

$("#department-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await api("/api/departments", {
    method: "POST",
    body: JSON.stringify({
      department_code: form.get("department_code"),
      department_name: form.get("department_name"),
      mail_to: splitEmails(form.get("mail_to")),
      mail_cc: splitEmails(form.get("mail_cc")),
    }),
  });
  toast("生产部门邮箱已保存");
  await refreshAll();
});

$("#departments-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-action]");
  if (!target) return;
  const row = (productionDepartmentState.items || []).find((item) => item.id === target.dataset.id);
  if (!row) return;
  if (target.dataset.action === "delete-department") {
    const ok = window.confirm(`确认删除生产部门“${row.department_name || row.department_code}”？如果这是最后一个生产主送邮箱，系统会进入不可启动状态。`);
    if (!ok) return;
    try {
      const result = await api(`/api/departments/${target.dataset.id}`, { method: "DELETE" });
      toast(result.bot_disabled ? "生产部门已删除，系统因配置不完整已停用" : "生产部门已删除");
      await refreshAll();
    } catch (error) {
      notifyError(error, ["生产邮箱", "删除失败"]);
    }
    return;
  }
  if (target.dataset.action !== "edit-department") return;
  const form = $("#department-form");
  form.querySelector("[name=department_code]").value = row.department_code || "";
  form.querySelector("[name=department_name]").value = row.department_name || "";
  form.querySelector("[name=mail_to]").value = (row.mail_to || []).join(", ");
  form.querySelector("[name=mail_cc]").value = (row.mail_cc || []).join(", ");
  form.querySelector("[name=department_name]")?.focus();
});

$("#logistics-department-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await api("/api/logistics-departments", {
    method: "POST",
    body: JSON.stringify({
      department_code: form.get("department_code"),
      department_name: form.get("department_name"),
      mail_to: splitEmails(form.get("mail_to")),
      mail_cc: splitEmails(form.get("mail_cc")),
    }),
  });
  toast("物流部门邮箱已保存");
  await refreshAll();
});

$("#logistics-departments-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-action]");
  if (!target) return;
  const row = (logisticsDepartmentState.items || []).find((item) => item.id === target.dataset.id);
  if (!row) return;
  if (target.dataset.action === "delete-logistics-department") {
    const ok = window.confirm(`确认删除物流部门“${row.department_name || row.department_code}”？`);
    if (!ok) return;
    try {
      await api(`/api/logistics-departments/${target.dataset.id}`, { method: "DELETE" });
      toast("物流部门已删除");
      await refreshAll();
    } catch (error) {
      notifyError(error, ["物流邮箱", "删除失败"]);
    }
    return;
  }
  if (target.dataset.action !== "edit-logistics-department") return;
  const form = $("#logistics-department-form");
  form.querySelector("[name=department_code]").value = row.department_code || "";
  form.querySelector("[name=department_name]").value = row.department_name || "";
  form.querySelector("[name=mail_to]").value = (row.mail_to || []).join(", ");
  form.querySelector("[name=mail_cc]").value = (row.mail_cc || []).join(", ");
  form.querySelector("[name=department_name]")?.focus();
});

$("#template-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await api("/api/templates/production-task", {
    method: "PUT",
    body: JSON.stringify(Object.fromEntries(form.entries())),
  });
  toast("模板新版本已保存");
  await refreshAll();
});

$("#initial-review-rule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.name || !data.field || !data.operator) {
    toast("请填写规则名称、字段和判断方式");
    return;
  }
  const candidateRule = {
    id: reviewRuleId(),
    name: data.name,
    field: data.field,
    operator: data.operator,
    value: data.value || "",
    message: data.message || `${optionLabel(initialReviewState.field_options || [], data.field)} 未通过初审规则：${data.name}`,
    enabled: true,
  };
  const candidateSignature = reviewRuleSignature(candidateRule);
  if ((initialReviewState.rules || []).some((rule) => !isReadonlyReviewRule(rule) && reviewRuleSignature(rule) === candidateSignature)) {
    toast("规则已存在，已忽略重复添加");
    return;
  }
  initialReviewState.rules = [
    ...(initialReviewState.rules || []),
    candidateRule,
  ];
  await saveInitialReviewRules();
  form.reset();
  closeInitialReviewRuleModal();
  toast("自定义初审规则已添加");
});

$("#initial-review-rules-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.action === "toggle-v2-review-rule") {
    const code = target.dataset.code;
    v2ReviewRulesState.rules = (v2ReviewRulesState.rules || []).map((rule) =>
      rule.code === code ? { ...rule, enabled: rule.enabled === false } : rule
    );
    await saveV2ReviewRules();
    toast("订单预审规则已更新");
    return;
  }
  const id = target.dataset.id;
  if (target.dataset.action === "delete-review-rule") {
    if ((initialReviewState.rules || []).some((rule) => String(rule.id || "") === id && isReadonlyReviewRule(rule))) {
      toast("系统内置规则只能查看，不能删除");
      return;
    }
    initialReviewState.rules = (initialReviewState.rules || []).filter((rule) => rule.id !== id);
  }
  if (target.dataset.action === "toggle-review-rule") {
    if ((initialReviewState.rules || []).some((rule) => String(rule.id || "") === id && isReadonlyReviewRule(rule))) {
      toast("系统内置规则只能查看，不能停用");
      return;
    }
    initialReviewState.rules = (initialReviewState.rules || []).map((rule) =>
      rule.id === id ? { ...rule, enabled: rule.enabled === false } : rule
    );
  }
  await saveInitialReviewRules();
  toast("初审规则已更新");
});

$("#workflow-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const input = form.querySelector("[name=message]");
  const button = form.querySelector("button[type=submit]");
  const message = String(input.value || "").trim();
  if (!message) {
    toast("请输入流程描述或补充信息");
    return;
  }
  appendChatMessage("user", "你", message, "#workflow-chat-log");
  workflowChatState.messages.push({ role: "user", content: message });
  input.value = "";
  button.disabled = true;
  try {
    const result = await api("/api/workflows/chat/generate", {
      method: "POST",
      body: JSON.stringify({
        messages: workflowChatState.messages,
        current_rule: workflowChatState.compiledRule || undefined,
        edit_version_id: workflowChatState.editVersionId || undefined,
      }),
    });
    const reply = String(result.reply || "已更新流程草稿。");
    appendChatMessage("assistant", "流程助手", reply, "#workflow-chat-log");
    workflowChatState.messages.push({ role: "assistant", content: reply });
    workflowChatState.ready = Boolean(result.ready);
    workflowChatState.validationErrors = Array.isArray(result.validation_errors) ? result.validation_errors : [];
    workflowChatState.editVersionId = String(result.edit_version_id || workflowChatState.editVersionId || "");
    workflowChatState.editWorkflowName = String(result.edit_workflow_name || workflowChatState.editWorkflowName || "");
    if (result.compiled_rule && typeof result.compiled_rule === "object") {
      workflowChatState.compiledRule = result.compiled_rule;
    }
    if (result.next_question && String(result.next_question).trim() && !reply.includes(String(result.next_question).trim())) {
      const nextQuestion = String(result.next_question).trim();
      appendChatMessage("assistant", "流程助手", nextQuestion, "#workflow-chat-log");
      workflowChatState.messages.push({ role: "assistant", content: nextQuestion });
    }
    renderWorkflowChatPreview();
    const notification = String(result.notification || "").trim();
    if (workflowChatState.ready) {
      toast(notification || "流程草稿已就绪，可直接保存");
    } else if (workflowChatState.validationErrors.length) {
      toast(`草稿已更新，仍有 ${workflowChatState.validationErrors.length} 条校验提示`);
    } else {
      toast("流程草稿已更新");
    }
  } catch (error) {
    appendChatMessage("error", "错误", error.message || "流程对话生成失败", "#workflow-chat-log");
    toast(error.message || "流程对话生成失败");
  } finally {
    button.disabled = false;
    input.focus();
  }
});

$("#workflow-chat-reset").addEventListener("click", () => {
  resetWorkflowChat();
  toast("流程对话已清空");
});

$("#workflow-chat-save").addEventListener("click", async () => {
  if (!workflowChatState.compiledRule) {
    toast("请先完成流程对话并生成草稿");
    return;
  }
  const activate = $("#workflow-chat-activate")?.checked || false;
  const result = await api("/api/workflows/chat/save", {
    method: "POST",
    body: JSON.stringify({
      compiled_rule: workflowChatState.compiledRule,
      activate,
      edit_version_id: workflowChatState.editVersionId || undefined,
    }),
  });
  const resultNode = $("#workflow-import-result");
  resultNode.classList.add("show");
  resultNode.textContent = JSON.stringify(result, null, 2);
  await refreshWorkflowRules();
  const validationErrors = Array.isArray(result.validation_errors) ? result.validation_errors : [];
  if (!activate && Array.isArray(result.created_versions) && result.created_versions.length) {
    const versionId = result.created_versions[0].id;
    const row = findWorkflowVersion(versionId);
    if (row) openWorkflowRuleEditor(row.version_id, row.rules);
  }
  if (validationErrors.length) {
    toast("流程存在校验问题，请查看结果并编辑原流程");
    return;
  }
  if (workflowChatState.editVersionId) {
    toast(activate ? "已有流程已保存并启用" : "已有流程已保存为草稿");
  } else {
    toast(activate ? "对话生成流程已保存并启用" : "对话生成流程已保存为草稿");
  }
});

$("#workflow-import-open")?.addEventListener("click", openWorkflowImportModal);

$("#workflow-simulate-open")?.addEventListener("click", async () => {
  const subject = window.prompt("模拟邮件主题", "采购订单-JM-CGDD-2026001 测试客户 2026-05-20");
  if (subject === null) return;
  const body = window.prompt(
    "模拟邮件正文",
    "客户名称：测试客户\n物料：积木展示架 A1\n数量：120套\n期望交期：2026-05-20\n订单号：SIM-001"
  );
  if (body === null) return;
  const fromAddress = window.prompt("销售发件人", "sales@jimuyida.com");
  if (fromAddress === null) return;
  const result = await api("/api/workflows/simulate", {
    method: "POST",
    body: JSON.stringify({ from_address: fromAddress, subject, body_text: body }),
  });
  const workflow = result.workflow?.workflow_name || result.workflow_match?.detail?.workflow_name || "未命中流程";
  const review = result.would_create_task ? "会创建任务" : "不会创建任务";
  const reasons = (result.exceptions || []).map((item) => item.detail?.message || item.exception_type).filter(Boolean).join("\n");
  window.alert(`模拟结果：${review}\n分类：${result.classification || "-"}\n命中流程：${workflow}${reasons ? `\n\n异常/原因：\n${reasons}` : ""}`);
});

$("#workflow-import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const selectedFile = form.get("workflow_file") instanceof File ? form.get("workflow_file") : null;
  const payload = {
    raw_text: String(form.get("raw_text") || "").trim(),
    prefer_llm: $("#workflow-import-form [name=prefer_llm]").checked,
    auto_publish: $("#workflow-import-form [name=auto_publish]").checked,
  };
  if (selectedFile && selectedFile.size > 0) {
    payload.file_name = selectedFile.name;
    payload.file_content_base64 = arrayBufferToBase64(await selectedFile.arrayBuffer());
  }
  if (!payload.file_content_base64 && !payload.raw_text) {
    toast("请选择流程文档或粘贴流程文本");
    return;
  }
  const resultNode = $("#workflow-import-result");
  resultNode.classList.add("show");
  resultNode.textContent = "正在导入并生成流程规则...";
  const result = await api("/api/workflows/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  resultNode.textContent = JSON.stringify(result, null, 2);
  await refreshWorkflowRules();
  const validationErrors = Array.isArray(result.validation_errors) ? result.validation_errors : [];
  if (!payload.auto_publish && Array.isArray(result.created_versions) && result.created_versions.length) {
    const versionId = result.created_versions[0].id;
    const row = findWorkflowVersion(versionId);
    if (row) {
      closeWorkflowImportModal();
      openWorkflowRuleEditor(row.version_id, row.rules);
    }
  }
  if (validationErrors.length) {
    toast("流程存在重复或校验问题，请查看导入结果并编辑原流程");
    return;
  }
  toast(payload.auto_publish ? "流程规则已导入并启用" : "流程规则已导入为草稿，请人工复核后启用");
});

$("#workflow-rules-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const versionId = target.dataset.id;
  if (!versionId) return;
  const row = findWorkflowVersion(versionId);
  if (target.dataset.action === "view-workflow-version") {
    openWorkflowRuleEditor(versionId, row?.rules || {}, { readonly: true });
    return;
  }
  if (target.dataset.action === "diff-workflow-version") {
    const diff = await api(`/api/workflows/versions/${versionId}/diff`);
    const lines = (diff.changes || []).map((item) => `- ${item.field}`).join("\n");
    window.alert(lines ? `版本差异：\n${lines}` : "该版本与上一版本无差异。");
    return;
  }
  if (target.dataset.action === "edit-workflow-version") {
    if (!row || !row.editable) {
      toast("流程启用中，需先停用后再编辑");
      return;
    }
    openWorkflowRuleEditor(versionId, row?.rules || {});
    return;
  }
  if (target.dataset.action === "llm-edit-workflow-version") {
    if (!row || !row.editable) {
      toast("流程启用中，需先停用后再用 LLM 编辑");
      return;
    }
    startWorkflowChatEdit(row);
    toast("已载入已有流程，后续对话将编辑该流程");
    return;
  }
  if (target.dataset.action === "activate-workflow-version") {
    if (!workflowRecipientEmails(row).length) {
      toast("流程收件人为空，不能启用。请先停用编辑并选择生产部门主送邮箱。");
      return;
    }
    try {
      await api(`/api/workflows/versions/${versionId}/activate`, { method: "POST" });
      toast("流程规则已启用");
      await refreshWorkflowRules();
    } catch (error) {
      notifyError(error, ["流程", "启用失败"]);
    }
    return;
  }
  if (target.dataset.action === "rollback-workflow-version") {
    if (!workflowRecipientEmails(row).length) {
      toast("流程收件人为空，不能启用。请先编辑并选择生产部门主送邮箱。");
      return;
    }
    const ok = window.confirm("确认将该历史版本启用为当前流程版本？现有 Active 版本会自动归档。");
    if (!ok) return;
    try {
      await api(`/api/workflows/versions/${versionId}/rollback`, { method: "POST" });
      toast("已回滚到指定流程版本");
      await refreshWorkflowRules();
    } catch (error) {
      notifyError(error, ["流程", "回滚启用失败"]);
    }
    return;
  }
  if (target.dataset.action === "deactivate-workflow-version") {
    await api(`/api/workflows/versions/${versionId}/deactivate`, { method: "POST" });
    if ($("#workflow-rule-editor-form [name=version_id]")?.value === versionId) closeWorkflowRuleEditor();
    toast("流程规则已停用");
    await refreshWorkflowRules();
    return;
  }
  if (target.dataset.action === "delete-workflow-version") {
    if (!row || !row.deletable) {
      toast("流程启用中，需先停用后再删除");
      return;
    }
    const ok = window.confirm("删除后将无法恢复，确认删除该流程版本？");
    if (!ok) return;
    await api(`/api/workflows/versions/${versionId}`, { method: "DELETE" });
    if ($("#workflow-rule-editor-form [name=version_id]")?.value === versionId) closeWorkflowRuleEditor();
    toast("流程规则已删除");
    await refreshWorkflowRules();
  }
});

$("#workflow-rule-editor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveWorkflowRuleEditor();
});

$("#workflow-rule-editor-form").addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  if (action === "add-workflow-review-rule") {
    syncWorkflowRuleEditorState();
    const select = $("#workflow-rule-editor-form [name=review_rule_source]");
    const sourceId = select?.value || "";
    const sourceRule = (initialReviewState.rules || []).find((rule) => String(rule.id || "") === sourceId);
    if (!sourceRule || !workflowRulesState.editingRules) {
      toast("请选择要添加的初审规则");
      return;
    }
    const sourceSignature = reviewRuleSignature(sourceRule);
    const exists = (workflowRulesState.editingRules.review_rules || []).some(
      (rule) => String(rule.id || "") === sourceId || reviewRuleSignature(rule) === sourceSignature
    );
    if (exists) {
      toast("该规则已在当前流程中");
      return;
    }
    workflowRulesState.editingRules.review_rules = [
      ...(workflowRulesState.editingRules.review_rules || []),
      { ...sourceRule, enabled: sourceRule.enabled !== false },
    ];
    renderWorkflowRuleEditor();
    toast("已添加到当前流程规则");
    return;
  }
  if (action === "remove-workflow-review-rule") {
    syncWorkflowRuleEditorState();
    if (!workflowRulesState.editingRules) return;
    const id = target.dataset.id || "";
    if ((workflowRulesState.editingRules.review_rules || []).some((rule) => String(rule.id || "") === id && isReadonlyReviewRule(rule))) {
      toast("系统内置规则只能查看，不能移除");
      return;
    }
    workflowRulesState.editingRules.review_rules = (workflowRulesState.editingRules.review_rules || []).filter(
      (rule) => String(rule.id || "") !== id
    );
    renderWorkflowRuleEditor();
    toast("已从当前流程移除规则");
    return;
  }
  if (action === "save-activate") {
    if (workflowRulesState.readonly) return;
    await saveWorkflowRuleEditor();
    return;
  }
  if (action === "cancel-edit") {
    closeWorkflowRuleEditor();
  }
});

function handleWorkflowRuleEditorLiveChange(event) {
  if (event.target.matches("[name=workflow_required_field], [name=routing_to_names], [name=routing_cc_names], [name=max_question_rounds], [name=conversation_exceeded_message]")) {
    syncWorkflowRuleEditorState();
  }
}

$("#workflow-rule-editor-form")?.addEventListener("change", handleWorkflowRuleEditorLiveChange);
$("#workflow-rule-editor-form")?.addEventListener("input", handleWorkflowRuleEditorLiveChange);

$("#runtime-mail-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const values = Object.fromEntries(form.entries());
  if (!values.bot_email_password) delete values.bot_email_password;
  if (!values.baidu_map_ak) delete values.baidu_map_ak;
  if (values.mail_auto_worker_interval_seconds && Number(values.mail_auto_worker_interval_seconds) < 60) {
    toast("Worker 执行周期不能低于 60 秒");
    return;
  }
  if (values.mail_rate_limit_interval_seconds && Number(values.mail_rate_limit_interval_seconds) < 60) {
    toast("邮箱登录/发信间隔不能低于 60 秒");
    return;
  }
  values.llm_fallback_enabled = $("#runtime-mail-form [name=llm_fallback_enabled]").checked;
  values.crm_attachment_llm_allow_external_sensitive = $("#runtime-mail-form [name=crm_attachment_llm_allow_external_sensitive]").checked;
  try {
    await api("/api/config/mail", {
      method: "PUT",
      body: JSON.stringify(values),
    });
    toast("邮箱与运行参数已保存");
    await refreshAll();
  } catch (error) {
    notifyError(error, ["接入配置", "保存失败"]);
  }
});

async function saveErpConfig() {
  const form = new FormData($("#erp-config-form"));
  const values = Object.fromEntries(form.entries());
  if (!values.erp_app_sec) delete values.erp_app_sec;
  values.erp_enabled = $("#erp-config-form [name=erp_enabled]").checked;
  values.erp_material_sync_enabled = $("#erp-config-form [name=erp_material_sync_enabled]").checked;
  await api("/api/config/erp", {
    method: "PUT",
    body: JSON.stringify(values),
  });
}

$("#erp-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveErpConfig();
    toast("ERP 配置已保存");
    await refreshAll();
  } catch (error) {
    notifyError(error, ["接入配置", "ERP 配置保存失败"]);
  }
});

async function saveCrmConfig() {
  const formNode = $("#crm-sync-config-form");
  const form = new FormData(formNode);
  const values = Object.fromEntries(form.entries());
  if (!values.crm_password) delete values.crm_password;
  if (!values.crm_api_key) delete values.crm_api_key;
  if (!values.crm_fxiaoke_request_json) delete values.crm_fxiaoke_request_json;
  if (!values.crm_fxiaoke_detail_request_json) delete values.crm_fxiaoke_detail_request_json;
  if (!values.v2_crm_phase1_scope_json) delete values.v2_crm_phase1_scope_json;
  if (!values.crm_sync_min_order_date) delete values.crm_sync_min_order_date;
  values.crm_sync_enabled = formNode.elements.crm_sync_enabled.checked;
  values.v2_crm_phase1_scope_enabled = formNode.elements.v2_crm_phase1_scope_enabled.checked;
  await api("/api/config/crm", {
    method: "PUT",
    body: JSON.stringify(values),
  });
}

$("#crm-sync-config-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveCrmConfig();
    toast("CRM 接入配置已保存");
    await refreshAll();
  } catch (error) {
    notifyError(error, ["系统接入", "CRM 接入配置保存失败"]);
  }
});

async function runCrmBrowserAction(endpoint, body, pendingText, successText) {
  const resultNode = $("#crm-sync-result");
  if (resultNode) {
    resultNode.classList.add("show");
    resultNode.textContent = pendingText;
  }
  try {
    if (endpoint !== "/api/crm/browser/stop") {
      await saveCrmConfig();
    }
    const result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify(body || {}),
    });
    if (resultNode) resultNode.textContent = JSON.stringify(result, null, 2);
    toast(successText || "CRM 浏览器操作完成");
    await refreshConfig();
  } catch (error) {
    notifyError(error, ["系统接入", "CRM 浏览器操作失败"]);
    if (resultNode) resultNode.textContent = messageFromError(error);
  }
}

$("#crm-browser-start")?.addEventListener("click", async () => {
  await runCrmBrowserAction(
    "/api/crm/browser/start",
    { mode: "headless" },
    "正在后台启动 CRM 专用浏览器...",
    "CRM 后台专用浏览器已启动"
  );
});

$("#crm-browser-login")?.addEventListener("click", async () => {
  await runCrmBrowserAction(
    "/api/crm/browser/start",
    { mode: "headed" },
    "正在切换到可视人工登录模式；登录完成后可再启动无头模式接管...",
    "已切换到人工登录模式"
  );
});

$("#crm-browser-stop")?.addEventListener("click", async () => {
  await runCrmBrowserAction(
    "/api/crm/browser/stop",
    {},
    "正在停止 CRM 专用浏览器...",
    "CRM 专用浏览器已停止"
  );
});

async function saveOmsConfig() {
  const formNode = $("#oms-config-form");
  const form = new FormData(formNode);
  const values = Object.fromEntries(form.entries());
  if (!values.oms_jackyun_app_secret) delete values.oms_jackyun_app_secret;
  if (!values.oms_customer_query_payload_json) delete values.oms_customer_query_payload_json;
  values.oms_enabled = formNode.elements.oms_enabled.checked;
  values.oms_mock_success = formNode.elements.oms_mock_success.checked;
  values.oms_auto_confirm_delivery_notice = formNode.elements.oms_auto_confirm_delivery_notice.checked;
  await api("/api/config/oms", {
    method: "PUT",
    body: JSON.stringify(values),
  });
}

$("#oms-config-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveOmsConfig();
    toast("OMS 接入配置已保存");
    await refreshAll();
  } catch (error) {
    notifyError(error, ["系统接入", "OMS 接入配置保存失败"]);
  }
});

async function runCrmSyncAction(endpoint, pendingText, crumbParts) {
  const resultNode = $("#crm-sync-result");
  if (resultNode) {
    resultNode.classList.add("show");
    resultNode.textContent = pendingText;
  }
  try {
    await saveCrmConfig();
    const result = await api(endpoint, { method: "POST" });
    if (resultNode) resultNode.textContent = JSON.stringify(result, null, 2);
    if (result?.busy) {
      toast(result.message || "当前有 CRM 同步任务正在进行，请稍后重试。");
      return;
    }
    toast(result.message || "CRM 同步已触发");
    await refreshCrmOrders();
  } catch (error) {
    notifyError(error, crumbParts || ["系统接入", "CRM 同步失败"]);
    if (resultNode) resultNode.textContent = messageFromError(error);
  }
}

$("#crm-sync-queue")?.addEventListener("click", async () => {
  await runCrmSyncAction("/api/crm/sync/queue", "正在投递 CRM 订单同步任务...", ["系统接入", "CRM 同步投递"]);
});

$("#crm-sync-run")?.addEventListener("click", async () => {
  await runCrmSyncAction("/api/crm/sync/run", "正在直接同步 CRM 订单，可能需要几十秒...", ["系统接入", "CRM 同步执行"]);
});

$("#crm-sync-test")?.addEventListener("click", async () => {
  await runCrmSyncAction("/api/crm/sync/test-connection", "正在执行 CRM 接入测试，验证列表、详情、附件和发货字段映射...", ["系统接入", "CRM 连接测试"]);
});

$("#test-oms-connection")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const resultNode = $("#oms-test-result");
  button.disabled = true;
  if (resultNode) {
    resultNode.classList.add("show");
    resultNode.textContent = "正在保存配置并测试 OMS OpenAPI...";
  }
  try {
    await saveOmsConfig();
    const result = await api("/api/oms/jackyun/test-connection", { method: "POST" });
    if (resultNode) resultNode.textContent = JSON.stringify(result, null, 2);
    toast(result.ok ? "OMS 连接成功" : "OMS 连接失败");
    await refreshConfig();
  } catch (error) {
    notifyError(error, ["系统接入", "OMS 连接测试失败"]);
    if (resultNode) resultNode.textContent = messageFromError(error);
  } finally {
    button.disabled = false;
  }
});

async function runOmsStatusPollAction(asyncJob = false) {
  const resultNode = $("#oms-status-poll-result");
  if (resultNode) {
    resultNode.classList.add("show");
    resultNode.textContent = asyncJob ? "正在投递 OMS 状态拉取任务..." : "正在拉取 OMS 状态...";
  }
  try {
    await saveOmsConfig();
    const result = await api(`/api/v2/oms/status-poll?limit=50${asyncJob ? "&async_job=true" : ""}`, { method: "POST" });
    if (resultNode) resultNode.textContent = JSON.stringify(result, null, 2);
    toast(asyncJob ? "OMS 状态拉取已入队" : `OMS 状态拉取完成：检查 ${result.checked || 0}，更新 ${result.updated || 0}`);
    await Promise.all([refreshJobs(), refreshCrmOrders()]);
  } catch (error) {
    notifyError(error, ["系统接入", "OMS 状态拉取失败"]);
    if (resultNode) resultNode.textContent = messageFromError(error);
  }
}

$("#poll-oms-status")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await runOmsStatusPollAction(false);
  } finally {
    button.disabled = false;
  }
});

$("#queue-oms-status-poll")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await runOmsStatusPollAction(true);
  } finally {
    button.disabled = false;
  }
});

$("#test-erp-connection").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const resultNode = $("#erp-test-result");
  button.disabled = true;
  resultNode.classList.add("show");
  resultNode.textContent = "正在保存配置并测试金蝶登录鉴权...";
  try {
    await saveErpConfig();
    const result = await api("/api/erp/test-connection", { method: "POST" });
    resultNode.textContent = JSON.stringify(result, null, 2);
    toast(result.ok ? "ERP 连接成功" : "ERP 连接失败");
    await refreshAll();
  } catch (error) {
    notifyError(error, ["接入配置", "ERP 连接测试失败"]);
    resultNode.textContent = messageFromError(error);
  } finally {
    button.disabled = false;
  }
});

$("#erp-query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#run-erp-query");
  const resultNode = $("#erp-query-result");
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  values.limit = Number(values.limit || 20);
  values.start_row = Number(values.start_row || 0);
  button.disabled = true;
  resultNode.classList.add("show");
  resultNode.textContent = "正在调用金蝶只读查询...";
  try {
    const result = await api("/api/erp/query", {
      method: "POST",
      body: JSON.stringify(values),
    });
    resultNode.textContent = JSON.stringify(result, null, 2);
    toast(result.ok ? `ERP 查询成功，返回 ${(result.items || []).length} 行` : "ERP 查询失败");
  } catch (error) {
    notifyError(error, ["接入配置", "ERP 查询失败"]);
    resultNode.textContent = messageFromError(error);
  } finally {
    button.disabled = false;
  }
});

$("#erp-material-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const resultNode = $("#erp-material-search-result");
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  const params = new URLSearchParams({
    q: values.q || "",
    limit: values.limit || "20",
    include_erp: form.elements.include_erp.checked ? "true" : "false",
  });
  resultNode.classList.add("show");
  resultNode.textContent = "正在查询物料...";
  try {
    const result = await api(`/api/erp/materials?${params.toString()}`);
    resultNode.textContent = JSON.stringify(result, null, 2);
    toast(`物料查询返回 ${result.total || 0} 条`);
  } catch (error) {
    notifyError(error, ["接入配置", "ERP 物料查询失败"]);
    resultNode.textContent = messageFromError(error);
  }
});

$("#erp-inventory-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const resultNode = $("#erp-inventory-result");
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const params = new URLSearchParams({
    material_code: values.material_code || "",
    warehouse_code: values.warehouse_code || "",
    limit: values.limit || "50",
  });
  resultNode.classList.add("show");
  resultNode.textContent = "正在查询 ERP 即时库存...";
  try {
    const result = await api(`/api/erp/inventory?${params.toString()}`);
    resultNode.textContent = JSON.stringify(result, null, 2);
    toast(result.ok ? `库存查询返回 ${result.total || 0} 条` : "库存查询失败");
  } catch (error) {
    notifyError(error, ["接入配置", "ERP 库存查询失败"]);
    resultNode.textContent = messageFromError(error);
  }
});

$("#e2e-mail-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const values = Object.fromEntries(form.entries());
  if (!values.e2e_sales_password) delete values.e2e_sales_password;
  if (!values.e2e_production_password) delete values.e2e_production_password;
  await api("/api/config/mail", {
    method: "PUT",
    body: JSON.stringify(values),
  });
  toast("端到端测试账号已保存");
  await refreshAll();
});

$("#run-e2e-mail").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const resultNode = $("#e2e-mail-result");
  button.disabled = true;
  resultNode.classList.add("show");
  resultNode.textContent = "正在运行真实腾讯企业邮箱端到端测试。IMAP/SMTP 登录和单账号发信都已限制为至少 60 秒一次，必要时测试会等待下一次窗口...";
  try {
    const result = await api("/api/e2e/tencent-mail/run", { method: "POST" });
    resultNode.textContent = formatE2EResult(result);
    toast("端到端测试完成");
    await refreshAll();
  } catch (error) {
    resultNode.textContent = error.message || "端到端测试失败";
    toast(error.message || "端到端测试失败");
  } finally {
    button.disabled = false;
  }
});

$("#model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const values = Object.fromEntries(form.entries());
  if (!values.api_key) delete values.api_key;
  await api("/api/model-providers/active", {
    method: "PUT",
    body: JSON.stringify(values),
  });
  toast("模型服务配置已保存");
  await refreshAll();
});

$("#weekly-report-recipients-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await api("/api/reports/weekly/recipients", {
    method: "PUT",
    body: JSON.stringify({
      to: splitEmails(form.get("to")),
      cc: splitEmails(form.get("cc")),
    }),
  });
  toast("周报收件人已保存");
  await refreshAll();
});

$("#preview-weekly-report").addEventListener("click", openWeeklyReportPreview);

$("#enqueue-weekly-report").addEventListener("click", async () => {
  await guardedAction(["周报", "生成邮件"], async () => {
    const result = await api("/api/reports/weekly/enqueue", { method: "POST" });
    toast(`周报邮件已进入外发队列：${result.status}`);
    await refreshAll();
  });
});

$("#system-toggle")?.addEventListener("click", async () => {
  const enabled = configEnabled(runtimeConfigState.bot_enabled, true);
  const nextEnabled = !enabled;
  if (nextEnabled && startupReadinessState.ready === false) {
    toast(`系统启动前配置不完整：缺少 ${(startupReadinessState.missing || []).join("、")}`);
    return;
  }
  try {
    await api("/api/config/mail", {
      method: "PUT",
      body: JSON.stringify({ bot_enabled: nextEnabled }),
    });
    runtimeConfigState = { ...runtimeConfigState, bot_enabled: String(nextEnabled) };
    await refreshConfig();
    renderSystemToggle();
    toast(nextEnabled ? "机器人已启动" : "机器人已暂停");
  } catch (error) {
    notifyError(error, ["系统", nextEnabled ? "启动失败" : "暂停失败"]);
  }
});

$("#business-data-clear")?.addEventListener("click", async () => {
  const adminPassword = window.prompt("请输入管理员密码确认清空系统中录入的流程、任务和初审规则数据。此操作不可恢复。");
  if (adminPassword === null) return;
  if (!adminPassword.trim()) {
    toast("管理员密码不能为空");
    return;
  }
  const ok = window.confirm("确认清空所有录入的流程、任务和初审规则数据？系统内置流程和内置初审规则会保留。");
  if (!ok) return;
  try {
    const result = await api("/api/system/business-data/clear", {
      method: "POST",
      body: JSON.stringify({ admin_password: adminPassword }),
    });
    const cleared = result.cleared || {};
    toast(`已清空：任务 ${cleared.task_count || 0} 条，流程 ${cleared.workflow_definition_count || 0} 条，初审规则 ${cleared.initial_review_rule_count || 0} 条`);
    await refreshAll();
  } catch (error) {
    notifyError(error, ["系统", "清空数据失败"]);
  }
});

$("#dashboard-insights")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-dashboard-period]");
  if (!button) return;
  dashboardViewState.period = button.dataset.dashboardPeriod || "day";
  refreshDashboard();
});

$("#outbound-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (target?.dataset.action === "jump-task") {
    event.preventDefault();
    event.stopPropagation();
    await guardedAction(["外发", "跳转任务"], async () => jumpToTask(target.dataset.taskId, target.dataset.taskNo));
    return;
  }
  if (target?.dataset.action === "jump-logistics-task") {
    event.preventDefault();
    event.stopPropagation();
    await guardedAction(["外发", "跳转物流任务"], async () => jumpToLogisticsTask(target.dataset.taskId, target.dataset.taskNo));
    return;
  }
  if (target?.dataset.action === "retry-outbound") {
    await guardedAction(["外发", "重新入队"], async () => {
      await api(`/api/outbound-mails/${target.dataset.id}/retry`, { method: "POST" });
      toast("已重新加入外发队列");
      await refreshAll();
    });
    return;
  }
  if (event.target.closest("button,a")) return;
  const row = event.target.closest("[data-outbound-id]");
  if (!row) return;
  await guardedAction(["外发", "详情"], async () => openOutboundDetail(row.dataset.outboundId));
});

$("#outbound-list").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target.closest("button,a")) return;
  const row = event.target.closest("[data-outbound-id]");
  if (!row) return;
  event.preventDefault();
  await guardedAction(["外发", "详情"], async () => openOutboundDetail(row.dataset.outboundId));
});

$("#cancel-pending-outbound")?.addEventListener("click", async () => {
  const state = tableStates.outbound;
  const scopeParts = [
    state.q ? `关键词：${state.q}` : "",
    state.status ? `状态：${state.status}` : "状态：Pending",
    state.mail_type ? `类型：${state.mail_type}` : "",
    state.recipient ? `收件人：${state.recipient}` : "",
  ].filter(Boolean);
  const confirmed = window.confirm(
    `确认批量取消当前筛选条件下的 Pending 外发任务？\n${scopeParts.join("；") || "全部 Pending 外发任务"}`
  );
  if (!confirmed) return;
  await guardedAction(["外发", "批量取消Pending"], async () => {
    const result = await api("/api/outbound-mails/cancel-pending", {
      method: "POST",
      body: JSON.stringify({
        q: state.q,
        status: state.status,
        mail_type: state.mail_type,
        recipient: state.recipient,
        limit: 5000,
      }),
    });
    toast(`已取消 ${result.cancelled || 0} 条 Pending 外发任务`);
    await refreshAll();
  });
});

$("#clear-outbound-queue")?.addEventListener("click", async () => {
  if (!confirm("确定要清空全部待发送外发队列吗？\n所有 Pending 和 Failed 任务将被标记为 Cancelled。此操作需要管理员密码。")) return;
  
  const adminPassword = prompt("请输入管理员密码以执行清空操作：");
  if (!adminPassword) return;

  await guardedAction(["外发", "清空队列"], async () => {
    const result = await api("/api/outbound-mails/clear-queue", {
      method: "POST",
      body: JSON.stringify({ admin_password: adminPassword }),
    });
    toast(`已清空 ${result.cleared} 个外发任务`);
    refreshOutbound();
  });
});


$("#notify-outbound-alerts")?.addEventListener("click", async () => {
  await guardedAction(["外发", "发送告警通知"], async () => {
    const result = await api("/api/outbound-mails/diagnostics/notify", { method: "POST" });
    toast(result.queued ? `告警通知已入队：${result.status}` : result.reason || "当前没有需要通知的外发告警");
    await refreshAll();
  });
});

$("#test-model").addEventListener("click", async () => {
  await guardedAction(["接入", "测试模型"], async () => {
    await api("/api/model-providers/test", { method: "POST" });
    toast("模型服务连通性正常");
    await refreshAll();
  });
});

$("#model-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const input = form.querySelector("[name=message]");
  const button = form.querySelector("button[type=submit]");
  const message = input.value.trim();
  if (!message) {
    toast("请输入测试内容");
    return;
  }
  appendChatMessage("user", "你", message);
  input.value = "";
  button.disabled = true;
  try {
    const result = await api("/api/model-providers/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    appendChatMessage("assistant", "模型", result.reply || JSON.stringify(result.raw).slice(0, 1200));
    toast("模型对话测试完成");
  } catch (error) {
    appendChatMessage("error", "错误", error.message || "模型调用失败");
    toast(error.message || "模型调用失败");
  } finally {
    button.disabled = false;
    input.focus();
  }
});

$("#cleanup-preview").addEventListener("click", async () => {
  const result = await api("/api/cleanup/preview", { method: "POST" });
  toast(`可清理 ${result.mail_count} 封非目标邮件，附件 ${result.attachment_count} 个`);
  await refreshAll();
});

$("#cleanup-run").addEventListener("click", async () => {
  const ok = window.confirm("确认执行清理？有效订单邮件不会被自动清理。");
  if (!ok) return;
  const result = await api("/api/cleanup/run", { method: "POST" });
  toast(`已清理 ${result.mail_count} 封邮件，删除文件 ${result.removed_files} 个`);
  await refreshAll();
});

$("#backup-run").addEventListener("click", async () => {
  const result = await api("/api/backups/run", { method: "POST" });
  toast(`备份完成：${result.status}`);
  await refreshAll();
});

$("#task-filter-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  taskQueryState = {
    ...taskQueryState,
    q: String(form.get("q") || "").trim(),
    status: String(form.get("status") || "").trim(),
    customer: String(form.get("customer") || "").trim(),
    product: String(form.get("product") || "").trim(),
    salesperson: String(form.get("salesperson") || "").trim(),
    order_no: String(form.get("order_no") || "").trim(),
    delivery: String(form.get("delivery") || "").trim(),
    page: 1,
  };
  await refreshTasks();
});

$("#task-filter-reset").addEventListener("click", async () => {
  $("#task-filter-form").reset();
  taskQueryState = { q: "", status: "", customer: "", product: "", salesperson: "", order_no: "", delivery: "", page: 1, page_size: taskQueryState.page_size };
  await refreshTasks();
});

$("#task-list-clear")?.addEventListener("click", async () => {
  const adminPassword = window.prompt("请输入管理员密码确认清空任务列表。此操作不可恢复。");
  if (adminPassword === null) return;
  if (!adminPassword.trim()) {
    toast("管理员密码不能为空");
    return;
  }
  const ok = window.confirm("确认清空所有任务？该操作会删除任务、任务版本、问答记录和对应需求草稿。");
  if (!ok) return;
  try {
    const result = await api("/api/tasks/clear", {
      method: "POST",
      body: JSON.stringify({ admin_password: adminPassword }),
    });
    toast(`已清空 ${result.cleared || 0} 条任务`);
    taskQueryState.page = 1;
    await refreshAll();
  } catch (error) {
    notifyError(error, ["任务列表", "清空失败"]);
  }
});

$("#task-pagination").addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-task-page]");
  if (!target || target.disabled) return;
  taskQueryState.page = Number(target.dataset.taskPage || 1);
  await refreshTasks();
});

$("#task-pagination").addEventListener("change", async (event) => {
  if (event.target.id !== "task-page-size") return;
  taskQueryState.page_size = Number(event.target.value || 10);
  taskQueryState.page = 1;
  await refreshTasks();
});

$("#mails-list")?.addEventListener("click", async (event) => {
  const jumpButton = event.target.closest("[data-action='jump-task']");
  if (jumpButton) {
    event.preventDefault();
    event.stopPropagation();
    await guardedAction(["邮件", "跳转任务"], async () => jumpToTask(jumpButton.dataset.taskId, jumpButton.dataset.taskNo));
    return;
  }
  const logisticsJumpButton = event.target.closest("[data-action='jump-logistics-task']");
  if (logisticsJumpButton) {
    event.preventDefault();
    event.stopPropagation();
    await guardedAction(["邮件", "跳转物流任务"], async () => jumpToLogisticsTask(logisticsJumpButton.dataset.taskId, logisticsJumpButton.dataset.taskNo));
    return;
  }
  const row = event.target.closest("[data-mail-id]");
  if (!row) return;
  await guardedAction(["邮件", "详情"], async () => openMailDetail(row.dataset.mailId));
});

$("#mail-detail-fields")?.addEventListener("click", async (event) => {
  const jumpButton = event.target.closest("[data-action='jump-task']");
  const logisticsJumpButton = event.target.closest("[data-action='jump-logistics-task']");
  if (!jumpButton && !logisticsJumpButton) return;
  event.preventDefault();
  if (logisticsJumpButton) {
    await guardedAction(["邮件", "跳转物流任务"], async () => jumpToLogisticsTask(logisticsJumpButton.dataset.taskId, logisticsJumpButton.dataset.taskNo));
    return;
  }
  await guardedAction(["邮件", "跳转任务"], async () => jumpToTask(jumpButton.dataset.taskId, jumpButton.dataset.taskNo));
});

$("#mails-list")?.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target.closest("button,a")) return;
  const row = event.target.closest("[data-mail-id]");
  if (!row) return;
  event.preventDefault();
  await guardedAction(["邮件", "详情"], async () => openMailDetail(row.dataset.mailId));
});

$("#tasks").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const id = target.dataset.id;
  const action = target.dataset.action;
  if (action === "workflow") {
    await openWorkflow(id);
    return;
  }
  if (action === "manual-close-task") {
    const note = window.prompt("关闭说明", "商务人工强制关闭");
    if (note === null) return;
    await api(`/api/tasks/${id}/manual-close`, {
      method: "POST",
      body: JSON.stringify({ note: note.trim() }),
    });
    toast("任务已手动关闭，并已通知销售和生产");
    await refreshAll();
  }
});

$("#logistics-tasks-list")?.addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const id = target.dataset.id;
  const action = target.dataset.action;
  if (action === "workflow") {
    await openWorkflow(id, "logistics");
    return;
  }
  if (action === "manual-close-logistics-task") {
    const note = window.prompt("关闭说明", "商务人工强制关闭物流任务");
    if (note === null) return;
    await api(`/api/logistics-tasks/${id}/manual-close`, {
      method: "POST",
      body: JSON.stringify({ note: note.trim() }),
    });
    toast("物流任务已手动关闭，并已通知销售和物流");
    await refreshAll();
  }
});

$("#workflow-close").addEventListener("click", closeWorkflow);
$("#workflow-modal").addEventListener("click", (event) => {
  if (event.target.id === "workflow-modal") closeWorkflow();
});

$("#workflow-editor-close")?.addEventListener("click", closeWorkflowRuleEditor);
$("#workflow-editor-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "workflow-editor-modal") closeWorkflowRuleEditor();
});

$("#workflow-import-close")?.addEventListener("click", closeWorkflowImportModal);
$("#workflow-import-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "workflow-import-modal") closeWorkflowImportModal();
});

$("#initial-review-rule-open")?.addEventListener("click", openInitialReviewRuleModal);
$("#initial-review-rule-close")?.addEventListener("click", closeInitialReviewRuleModal);
$("#initial-review-rule-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "initial-review-rule-modal") closeInitialReviewRuleModal();
});

$("#weekly-preview-close").addEventListener("click", closeWeeklyReportPreview);
$("#weekly-preview-modal").addEventListener("click", (event) => {
  if (event.target.id === "weekly-preview-modal") closeWeeklyReportPreview();
});

$("#mail-detail-close")?.addEventListener("click", closeMailDetail);
$("#mail-detail-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "mail-detail-modal") closeMailDetail();
});

$("#crm-order-detail-close")?.addEventListener("click", closeCrmOrderDetail);
$("#crm-order-detail-tabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  activateCrmOrderDetailTab(button.dataset.tab || "order");
});
$("#crm-order-detail-modal")?.addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("button[data-action='delete-crm-order-local']");
  if (deleteButton) {
    const id = deleteButton.dataset.id || currentCrmOrderDetailId;
    const orderNo = deleteButton.dataset.no || "该订单";
    if (!id) return;
    deleteButton.disabled = true;
    deleteButton.textContent = "删除中...";
    try {
      await deleteCrmOrderLocal(id, orderNo);
    } catch (error) {
      notifyError(error, ["CRM", "删除本地订单失败"]);
      deleteButton.disabled = false;
      deleteButton.textContent = "删除本地订单";
    }
    return;
  }
  const actionButton = event.target.closest("button[data-action='retry-crm-detail-sync']");
  if (actionButton) {
    const id = actionButton.dataset.id || currentCrmOrderDetailId;
    if (!id) return;
    actionButton.disabled = true;
    actionButton.textContent = "同步中...";
    try {
      const result = await api(`/api/crm/orders/${id}/retry-detail-sync`, { method: "POST" });
      if (result?.busy) {
        toast(result.message || "当前有 CRM 同步任务正在进行，请稍后重试。");
        actionButton.disabled = false;
        actionButton.textContent = "重试详情同步";
        return;
      }
      toast("CRM 订单详情已重新同步");
      await openCrmOrderDetail(id);
      await refreshCrmOrders();
    } catch (error) {
      notifyError(error, ["CRM", "详情同步重试失败"]);
      actionButton.disabled = false;
      actionButton.textContent = "重试详情同步";
    }
    return;
  }
  if (event.target.id === "crm-order-detail-modal") closeCrmOrderDetail();
});

$("#inventory-detail-close")?.addEventListener("click", closeInventoryDetail);
$("#inventory-detail-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "inventory-detail-modal") closeInventoryDetail();
});

$("#inventory-classification-close")?.addEventListener("click", closeInventoryClassificationDiagnostics);
$("#inventory-classification-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "inventory-classification-modal") closeInventoryClassificationDiagnostics();
});

$("#exception-context-close")?.addEventListener("click", () => {
  $("#exception-context-modal").hidden = true;
});
$("#exception-context-modal")?.addEventListener("click", (event) => {
  if (event.target.id === "exception-context-modal") $("#exception-context-modal").hidden = true;
});

$("#copilot-open")?.addEventListener("click", openCopilotDrawer);
$("#copilot-close")?.addEventListener("click", closeCopilotDrawer);
$("#copilot-drawer")?.addEventListener("click", (event) => {
  if (event.target.id === "copilot-drawer") closeCopilotDrawer();
  if (event.target.closest("a")) closeCopilotDrawer();
});

$("#exception-context-body")?.addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.action === "replay-oms-notice") {
    const evidence = window.prompt(`填写 OMS 重放修复证据${target.dataset.noticeNo ? `（${target.dataset.noticeNo}）` : ""}`, "");
    if (evidence === null) return;
    if (!evidence.trim()) {
      toast("请先填写修复证据");
      return;
    }
    await api(`/api/v2/delivery-notices/${target.dataset.id}/replay-oms`, {
      method: "POST",
      body: JSON.stringify({ repair_evidence: evidence.trim(), actor: "operator" }),
    });
    toast("OMS 重放已入队");
    if (target.dataset.exceptionId) await openExceptionContext(target.dataset.exceptionId);
    await refreshAll();
    return;
  }
  if (target.dataset.action === "apply-address-correction") {
    if (!window.confirm("确定要一键应用此 AI 地址修复建议吗？这将修改 CRM 订单收货信息并重新验证订单。")) return;
    try {
      const res = await api(`/api/exceptions/${target.dataset.exceptionId}/apply-address-correction`, {
        method: "POST"
      });
      if (res.success) {
        toast(res.message || "地址修复应用成功！");
        await openExceptionContext(target.dataset.exceptionId);
        await refreshAll();
      } else {
        toast(res.message || "应用修复失败");
      }
    } catch (e) {
      toast("应用失败：" + messageFromError(e));
    }
    return;
  }
  if (target.dataset.action !== "diagnosis-feedback") return;
  const note = window.prompt("反馈说明", "");
  if (note === null) return;
  const id = target.dataset.id;
  await api(`/api/exceptions/${id}/diagnosis-feedback`, {
    method: "POST",
    body: JSON.stringify({ feedback: target.dataset.feedback, note, actor: "operator" }),
  });
  toast("诊断反馈已记录");
  await openExceptionContext(id);
  await refreshExceptions();
});



$("#exceptions-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const id = target.dataset.id;
  const action = target.dataset.action;
  if (action === "view-exception-context") {
    await openExceptionContext(id);
    return;
  }
  if (action === "resolve-exception") {
    const isHighRisk = target.dataset.highRisk === "true";
    let actor = "operator";
    let confirm_risk = false;
    if (isHighRisk) {
      confirm_risk = window.confirm("这是高危异常。确认已完成下游核对、责任人身份校验，并继续关闭？");
      if (!confirm_risk) return;
      actor = window.prompt("责任人身份（姓名/邮箱）", "");
      if (actor === null) return;
      if (!actor.trim() || ["operator", "system", "admin", "manager"].includes(actor.trim().toLowerCase())) {
        toast("请输入明确责任人身份");
        return;
      }
    }
    const note = window.prompt("关闭说明", isHighRisk ? "" : "已人工处理");
    if (note === null) return;
    if (isHighRisk && note.trim().length < 6) {
      toast("高危异常需填写明确处理备注");
      return;
    }
    await api(`/api/exceptions/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({
        note,
        actor,
        confirm_risk,
        resolution_evidence: isHighRisk ? { ui_action: "manual_close_high_risk", confirm_risk: true } : null,
      }),
    });
    toast("异常已关闭");
  }
  if (action === "assign-exception") {
    const assignee = window.prompt("分派给", "");
    if (assignee === null) return;
    if (!assignee.trim()) {
      toast("请输入负责人");
      return;
    }
    const note = window.prompt("分派备注", "") || "";
    await api(`/api/exceptions/${id}/assign`, {
      method: "POST",
      body: JSON.stringify({ assignee, note, actor: "operator" }),
    });
    toast("异常已分派");
  }
  if (action === "diagnose-exception") {
    try {
      const streamed = await diagnoseExceptionStream(id);
      if (!streamed) {
        await api(`/api/exceptions/${id}/diagnose`, { method: "POST" });
        toast("诊断已生成");
      }
    } catch (error) {
      await api(`/api/exceptions/${id}/diagnose`, { method: "POST" });
      toast(error.message ? `流式失败，已同步生成：${error.message}` : "流式失败，已同步生成");
    }
  }
  if (action === "reopen-exception") {
    const note = window.prompt("重开原因", "需要继续处理");
    if (note === null) return;
    await api(`/api/exceptions/${id}/reopen`, {
      method: "POST",
      body: JSON.stringify({ note, actor: "operator" }),
    });
    toast("异常已重开");
  }
  if (action === "patch-exception") {
    const customer_name = window.prompt("客户名称，留空则不修改", "");
    if (customer_name === null) return;
    const product_summary = window.prompt("物料/规格，留空则不修改", "");
    if (product_summary === null) return;
    const quantity_text = window.prompt("数量，留空则不修改", "");
    if (quantity_text === null) return;
    const expected_delivery_date = window.prompt("期望交期，留空则不修改", "");
    if (expected_delivery_date === null) return;
    const payload = { clear_risk_flags: true };
    if (customer_name) payload.customer_name = customer_name;
    if (product_summary) payload.product_summary = product_summary;
    if (quantity_text) payload.quantity_text = quantity_text;
    if (expected_delivery_date) payload.expected_delivery_date = expected_delivery_date;
    const result = await api(`/api/exceptions/${id}/apply-requirement-patch`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(result.task ? "已补字段并生成任务单草稿" : "字段仍不完整，异常保持待处理");
  }
  await refreshAll();
});

window.addEventListener("hashchange", () => setActivePage());

function navigateToCrmOrder(crmOrderNo) {
  tableStates.crmOrders.q = crmOrderNo;
  tableStates.crmOrders.page = 1;
  // 同步搜索框的值
  const searchInput = document.querySelector("#crm-orders-filter-form [name=q]");
  if (searchInput) searchInput.value = crmOrderNo;
  window.location.hash = "crm-orders";
  refreshCrmOrders();
}

function formatE2EResult(result) {
  const lines = [
    `测试结果：${result.ok ? "通过" : "失败"}`,
    `测试编号：${result.test_id}`,
    `销售邮箱：${result.sales_email}`,
    `生产邮箱：${result.production_email}`,
    `任务单：${result.task_no}`,
    "",
    "步骤：",
    ...result.steps.map((step) => `- ${step.name}: ${step.status} ${step.detail || ""}`),
  ];
  return lines.join("\n");
}

// ==========================================
// Product Management Logic
// ==========================================

function productLookupKey(value) {
  return String(value || "").trim().toLowerCase();
}

function cacheProductSpuRows(rows = []) {
  window._productSpuRows = window._productSpuRows || {};
  window._productSpuRowsByCode = window._productSpuRowsByCode || {};
  rows.forEach((row) => {
    window._productSpuRows[row.id] = row;
    window._productSpuRowsByCode[productLookupKey(row.spu_id)] = row;
  });
}

function cacheProductSkuRows(rows = []) {
  window._productSkuRows = window._productSkuRows || {};
  window._productSkuRowsByCode = window._productSkuRowsByCode || {};
  rows.forEach((row) => {
    if (row.binding_status && row.binding_status !== "ok") return;
    const skuUuid = row.sku_uuid || row.id;
    if (!skuUuid || !row.sku_id) return;
    const cached = { ...row, id: skuUuid };
    window._productSkuRows[skuUuid] = cached;
    window._productSkuRowsByCode[productLookupKey(row.sku_id)] = cached;
  });
}

function productPayloadTotal(data, rows = []) {
  const total = data?.total ?? data?.total_count ?? data?.count;
  const number = Number(total);
  return Number.isFinite(number) ? number : rows.length;
}

function renderProductCenterSummary() {
  const node = $("#product-tab-metrics");
  if (!node) return;
  const v = (item) => item === null || item === undefined ? 0 : Number(item) || 0;
  const sku = v(productCenterState.sku);
  const low = v(productCenterState.materialLowStock) + v(productCenterState.finishedLowStock);
  const zero = v(productCenterState.materialZeroStock) + v(productCenterState.finishedZeroStock);
  node.innerHTML = [
    `<span class="metric-pill"><strong>${h(sku)}</strong> SKU</span>`,
    low ? `<span class="metric-pill warn"><strong>${h(low)}</strong> 低库存</span>` : "",
    zero ? `<span class="metric-pill danger"><strong>${h(zero)}</strong> 零库存</span>` : "",
  ].filter(Boolean).join("");
}

async function refreshProductsSpu() {
  const data = await api(`/api/products/spu?${queryFromState(tableStates.productsSpu)}`);
  const rows = data.items || [];
  cacheProductSpuRows(rows);
  productCenterState.spu = productPayloadTotal(data, rows);
  renderProductCenterSummary();
  $("#products-spu-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div><strong>${h(row.spu_id)}</strong><br /><small>${h(row.name)}</small></div>
      <div><small>${h(row.brand || "-")}</small><br /><small>${h(row.category || "-")} · 别名 ${h((row.review_aliases || []).length)}</small></div>
      <div><small>${h(formatTime(row.created_at))}</small><br /><a href="#" class="link" data-action="edit-product-aliases" data-id="${h(row.id)}">维护预审别名</a></div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>暂无 SPU 数据</div></div>`;
  renderListPagination("#products-spu-pagination", "productsSpu", data);
}

async function refreshProductSpuSuggestions(q = "") {
  const node = $("#products-spu-suggestions");
  if (!node) return;
  const requestSeq = ++productSpuSuggestSeq;
  const params = new URLSearchParams({
    q: String(q || "").trim(),
    page: "1",
    page_size: "50",
  });
  const data = await api(`/api/products/spu?${params}`);
  if (requestSeq !== productSpuSuggestSeq) return;
  cacheProductSpuRows(data.items || []);
  node.innerHTML = (data.items || []).map((row) => {
    const code = row.spu_id || "";
    const name = row.name || "";
    const aliases = (row.review_aliases || []).slice(0, 2).join(" / ");
    const label = [code, name, aliases ? `别名 ${aliases}` : ""].filter(Boolean).join(" · ");
    return `<option value="${h(code)}" label="${h(label)}">${h(label)}</option>`;
  }).join("");
}

function queueProductSpuSuggestions(value = "") {
  window.clearTimeout(productSpuSuggestTimer);
  productSpuSuggestTimer = window.setTimeout(() => {
    guardedAction(["成品 SPU", "联想"], async () => refreshProductSpuSuggestions(value));
  }, 160);
}

async function resolveProductSpuLookup(form) {
  const hidden = form.elements.spu_uuid;
  const input = form.elements.spu_lookup;
  if (!input) return hidden?.value || "";
  const value = String(input.value || "").trim();
  if (!value) throw new Error("请选择所属成品 SPU");
  const cached = window._productSpuRowsByCode?.[productLookupKey(value)];
  if (cached) {
    hidden.value = cached.id;
    input.value = cached.spu_id;
    return cached.id;
  }
  const data = await api(`/api/products/spu?${new URLSearchParams({ q: value, page: "1", page_size: "1" })}`);
  const row = (data.items || [])[0];
  if (!row) throw new Error(`未找到成品 SPU：${value}`);
  cacheProductSpuRows([row]);
  hidden.value = row.id;
  input.value = row.spu_id;
  return row.id;
}

async function refreshProductSkuSuggestions(q = "") {
  const node = $("#products-sku-suggestions");
  if (!node) return;
  const requestSeq = ++productSkuSuggestSeq;
  const params = new URLSearchParams({
    q: String(q || "").trim(),
    page: "1",
    page_size: "50",
  });
  const data = await api(`/api/products/sku?${params}`);
  if (requestSeq !== productSkuSuggestSeq) return;
  cacheProductSkuRows(data.items || []);
  node.innerHTML = (data.items || []).map((row) => {
    const sku = row.sku_id || "";
    const spu = [row.spu_id || "", row.spu_name || ""].filter(Boolean).join(" · ");
    const aliases = (row.review_aliases || []).slice(0, 2).join(" / ");
    const label = [sku, spu, aliases ? `别名 ${aliases}` : ""].filter(Boolean).join(" · ");
    return `<option value="${h(sku)}" label="${h(label)}">${h(label)}</option>`;
  }).join("");
}

function queueProductSkuSuggestions(value = "") {
  window.clearTimeout(productSkuSuggestTimer);
  productSkuSuggestTimer = window.setTimeout(() => {
    guardedAction(["成品 SKU", "联想"], async () => refreshProductSkuSuggestions(value));
  }, 160);
}

async function resolveProductSkuLookup(form) {
  const hidden = form.elements.sku_uuid;
  const input = form.elements.sku_lookup;
  if (!input) return hidden?.value || "";
  const value = String(input.value || "").trim();
  if (!value) throw new Error("请选择成品 SKU");
  const cached = window._productSkuRowsByCode?.[productLookupKey(value)];
  if (cached) {
    hidden.value = cached.id;
    input.value = cached.sku_id;
    return cached.id;
  }
  const data = await api(`/api/products/sku?${new URLSearchParams({ q: value, page: "1", page_size: "1" })}`);
  const row = (data.items || [])[0];
  if (!row) throw new Error(`未找到成品 SKU：${value}`);
  cacheProductSkuRows([row]);
  hidden.value = row.id;
  input.value = row.sku_id;
  return row.id;
}

function formatProductReviewPrice(value) {
  if (value === null || value === undefined || value === "") return "未识别";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return `${(number / 100).toFixed(2)} 元`;
}

function formatProductMoney(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return `${(number / 100).toFixed(2)} 元`;
}

function centsToAmountInput(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return (number / 100).toFixed(2);
}

function amountInputToCents(value, label) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  const number = Number(text);
  if (!Number.isFinite(number) || number < 0) {
    throw new Error(`${label}请输入有效金额`);
  }
  return Math.round(number * 100);
}

function formatPromotionDiscount(row) {
  if (!row) return "-";
  if (row.discount_type === "fixed_amount") return formatProductMoney(row.discount_value);
  return `${h(row.discount_value)}%`;
}

function promotionDiscountInputValue(row) {
  if (!row) return "";
  if (row.discount_type === "fixed_amount") return centsToAmountInput(row.discount_value);
  return row.discount_value ?? "";
}

function productReviewStatusLabel(status) {
  if (status === "Pass") return "通过";
  if (status === "Warning") return "提醒";
  if (status === "Exception") return "异常";
  return status || "未审查";
}

function productReviewStatusClass(status) {
  if (status === "Pass") return "is-active";
  if (status === "Warning") return "is-warn";
  if (status === "Exception") return "is-danger";
  return "is-muted";
}

function productReviewIssueLabel(type) {
  if (type === "missing_price") return "缺价格规则";
  if (type === "incomplete_price") return "价格不完整";
  if (type === "duplicate_alias") return "重复别名";
  if (type === "missing_alias") return "缺人工别名";
  if (type === "invalid_promotion") return "促销未绑定";
  if (type === "duplicate_promotion") return "促销重复";
  return type || "待处理";
}

async function refreshProductReviewReadiness() {
  const summaryNode = $("#products-review-readiness-summary");
  const listNode = $("#products-review-readiness-list");
  if (!summaryNode || !listNode) return;
  const channel = $("#products-review-preview-form input[name='channel']")?.value || "default";
  summaryNode.innerHTML = `<div class="metric"><small>体检</small><strong>...</strong></div>`;
  listNode.innerHTML = `<div class="review-preview-row"><div class="empty-note">正在检查预审准备度...</div></div>`;
  const params = new URLSearchParams({ channel, limit: "20" });
  const data = await api(`/api/products/review-readiness?${params}`);
  const summary = data.summary || {};
  summaryNode.innerHTML = `
    <div class="metric ${summary.blocker_count ? "danger" : ""}"><small>准备度</small><strong>${h(summary.score ?? 0)}</strong></div>
    <div class="metric ${summary.blocker_count ? "warn" : ""}"><small>阻断项</small><strong>${h(summary.blocker_count || 0)}</strong></div>
    <div class="metric"><small>建议项</small><strong>${h(summary.warning_count || 0)}</strong></div>
    <div class="metric"><small>成品 SKU</small><strong>${h(summary.finished_sku_count || 0)}</strong></div>
  `;
  const issues = data.issues || [];
  window._productSpuRows = window._productSpuRows || {};
  issues.forEach((issue) => {
    if (issue.spu_uuid) {
      window._productSpuRows[issue.spu_uuid] = {
        id: issue.spu_uuid,
        spu_id: issue.spu_id,
        name: issue.product_name,
        review_aliases: issue.review_aliases || [],
      };
    }
  });
  listNode.innerHTML = issues.map(issue => `
    <div class="review-preview-row">
      <div>
        <strong>${h(productReviewIssueLabel(issue.issue_type))}</strong>
        <br /><small>${h(issue.message || "-")}</small>
      </div>
      <div><strong>${h(issue.sku_id || issue.spu_id || issue.alias || "-")}</strong><br /><small>${h(issue.product_name || issue.channel || "-")}</small></div>
      <div>
        <span class="status-pill ${issue.severity === "blocker" ? "is-danger" : "is-warn"}">${h(issue.severity === "blocker" ? "需处理" : "建议")}</span>
        ${issue.action === "configure_pricing" && issue.sku_uuid ? `<br /><button class="button ghost compact-action" type="button" data-action="quick-new-pricing" data-sku-uuid="${h(issue.sku_uuid)}" data-sku-id="${h(issue.sku_id || "")}" data-channel="${h(issue.channel || "default")}">配置价格规则</button>` : ""}
        ${(issue.action === "configure_alias" || issue.action === "review_alias") && issue.spu_uuid ? `<br /><button class="button ghost compact-action" type="button" data-action="edit-product-aliases" data-id="${h(issue.spu_uuid)}">维护预审别名</button>` : ""}
        ${issue.action === "configure_promotion" ? `<br /><button class="button ghost compact-action" type="button" data-action="goto-promotions" data-q="${h(issue.promotion_name || issue.promotion_id || "")}">维护促销规则</button>` : ""}
      </div>
    </div>
  `).join("") || `<div class="review-preview-row"><div class="empty-note">当前渠道未发现会影响预审的体检项。</div></div>`;
}

function renderProductReviewPreview(result) {
  const node = $("#products-review-preview-result");
  if (!node) return;
  const items = result.items || [];
  const summary = result.summary || {};
  if (!items.length) {
    const suggestions = result.suggestions || [];
    const aliasCandidate = result.alias_candidate || "";
    node.innerHTML = `
      <div class="product-review-summary">
        <div class="metric warn"><small>匹配结果</small><strong>0</strong></div>
        <div class="metric"><small>候选成品</small><strong>${h(suggestions.length)}</strong></div>
        <div class="metric"><small>建议别名</small><strong>${h(aliasCandidate || "-")}</strong></div>
      </div>
      <div class="empty-note">未匹配到成品库存中的 SKU。可以从候选成品中选择一个，确认后把当前叫法维护为预审别名。</div>
      ${suggestions.length ? `
        <div class="review-preview-list">
          ${suggestions.map(item => `
            <div class="review-preview-row">
              <div><strong>${h(item.spu_id)}</strong><br /><small>${h(item.name || "-")}</small></div>
              <div><strong>${h(item.sku_id || "-")}</strong><br /><small>相似：${h(item.matched_alias || "-")}</small></div>
              <div>
                <button class="button ghost" type="button" data-action="use-review-alias-suggestion" data-id="${h(item.id)}" data-alias="${h(item.suggested_alias || aliasCandidate)}">维护为别名</button>
                <br /><small>当前别名 ${h((item.review_aliases || []).length)} 个</small>
              </div>
            </div>
          `).join("")}
        </div>
      ` : ""}
    `;
    return;
  }
  const riskFlags = summary.risk_flags || [];
  node.innerHTML = `
    <div class="product-review-summary">
      <div class="metric"><small>匹配 SKU</small><strong>${h(summary.matched_count || items.length)}</strong></div>
      <div class="metric ${riskFlags.length ? "warn" : ""}"><small>风险提示</small><strong>${h(riskFlags.length)}</strong></div>
      <div class="metric"><small>渠道</small><strong>${h(result.channel || "default")}</strong></div>
    </div>
    ${riskFlags.length ? `<div class="review-risk-list">${riskFlags.map(flag => `<span>${h(flag)}</span>`).join("")}</div>` : ""}
    <div class="review-preview-list">
      ${items.map(item => {
        const review = item.review || {};
        const flags = review.risk_flags || [];
        return `
          <div class="review-preview-row">
            <div>
              <strong>${h(item.sku_id || item.sku_code || "未识别 SKU")}</strong>
              <br /><small>${h(item.product_name || item.spu_id || "-")}</small>
              <br /><small>${h(item.match_source === "product_alias" ? "别名匹配" : "SKU 编码匹配")}：${h(item.match_alias || "-")}</small>
            </div>
            <div><strong>${h(formatProductReviewPrice(item.unit_price))}</strong><br /><small>识别单价</small></div>
            <div>
              <span class="status-pill ${h(productReviewStatusClass(review.status))}">${h(productReviewStatusLabel(review.status))}</span>
              <br /><small>${flags.length ? h(flags.join("；")) : "价格/促销规则未发现风险"}</small>
              ${item.sku_uuid ? `<br /><button class="button ghost compact-action" type="button" data-action="quick-new-pricing" data-sku-uuid="${h(item.sku_uuid)}" data-sku-id="${h(item.sku_id || item.sku_code || "")}" data-channel="${h(result.channel || "default")}" data-unit-price="${h(item.unit_price || "")}" data-pricing-configured="${item.pricing_configured ? "true" : "false"}">${item.pricing_configured ? "调整价格规则" : "配置价格规则"}</button>` : ""}
            </div>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function openProductAliasModal(spuId) {
  const row = window._productSpuRows?.[spuId];
  if (!row) return;
  const form = $("#product-alias-form");
  if (!form) return;
  form.spu_uuid.value = row.id || "";
  form.spu_label.value = `${row.spu_id || ""} · ${row.name || ""}`;
  form.aliases.value = (row.review_aliases || []).join("\n");
  $("#product-alias-title").textContent = `维护预审别名 · ${row.spu_id || ""}`;
  openModal("#product-alias-modal");
}

function openProductAliasSuggestion(spuId, alias) {
  const row = window._productSpuRows?.[spuId];
  if (!row) return;
  openProductAliasModal(spuId);
  const form = $("#product-alias-form");
  const current = String(form.aliases.value || "")
    .split(/\n+/)
    .map(item => item.trim())
    .filter(Boolean);
  const normalized = (value) => String(value || "").trim().toLowerCase().replace(/[\s_\-/,，、:：;；|()（）\[\]【】"“”'‘’]+/g, "");
  if (alias && !current.some(item => normalized(item) === normalized(alias))) {
    form.aliases.value = [...current, alias].join("\n");
  }
  form.aliases.focus();
}

function openProductPricingModal({ skuUuid, skuId = "", channel = "default", unitPrice = "", pricingConfigured = false } = {}) {
  const form = $("#product-pricing-form");
  if (!form || !skuUuid) return;
  form.reset();
  form.elements.sku_uuid.value = skuUuid;
  form.elements.sku_lookup.value = skuId || skuUuid;
  form.elements.channel.value = channel || "default";
  if (unitPrice && !pricingConfigured) {
    form.elements.map_price.value = centsToAmountInput(unitPrice);
    form.elements.tier_a_price.value = centsToAmountInput(unitPrice);
  }
  $("#product-pricing-title").textContent = `${pricingConfigured ? "调整" : "配置"}渠道价格${skuId ? ` · ${skuId}` : ""}`;
  openModal("#product-pricing-modal");
}

$("#products-spu-filter-form input[list='products-spu-suggestions']")?.addEventListener("focus", (event) => {
  queueProductSpuSuggestions(event.currentTarget.value);
});

$("#products-spu-filter-form input[list='products-spu-suggestions']")?.addEventListener("input", (event) => {
  queueProductSpuSuggestions(event.currentTarget.value);
});

$("#product-sku-form input[list='products-spu-suggestions']")?.addEventListener("focus", (event) => {
  queueProductSpuSuggestions(event.currentTarget.value);
});

$("#product-sku-form input[list='products-spu-suggestions']")?.addEventListener("input", (event) => {
  $("#product-sku-form").elements.spu_uuid.value = "";
  queueProductSpuSuggestions(event.currentTarget.value);
});

document.querySelectorAll('input[list="products-sku-suggestions"]').forEach((input) => {
  input.addEventListener("focus", () => queueProductSkuSuggestions(input.value));
  input.addEventListener("input", () => {
    const form = input.closest("form");
    if (form?.elements?.sku_uuid) form.elements.sku_uuid.value = "";
    queueProductSkuSuggestions(input.value);
  });
});

$("#products-review-readiness-refresh")?.addEventListener("click", async () => {
  await guardedAction(["物料中心", "刷新预审体检"], async () => refreshProductReviewReadiness());
});

$("#sync-oms-materials")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  toast("正在从 OMS 同步物料，请稍候...");
  try {
    const result = await api("/api/products/oms-sync", { method: "POST" });
    if (result.ok) {
      let msg = `OMS 物料同步完成：${result.total || 0} 条`;
      if (result._debug) {
        const dbg = result._debug;
        if (dbg.first_row_keys) msg += ` | OMS 字段: ${dbg.first_row_keys.slice(0, 8).join(', ')}`;
        if (dbg.data_block_keys) msg += ` | data keys: ${dbg.data_block_keys.join(', ')}`;
      }
      toast(msg);
      if (result._debug) {
        const debugNode = $("#oms-sync-debug");
        if (debugNode) {
          debugNode.textContent = JSON.stringify(result._debug, null, 2);
          debugNode.classList.add("show");
        }
      }
    } else {
      toast(result.skipped || "OMS 物料同步未执行");
    }
    await refreshProductsSku();
  } catch (error) {
    notifyError(error, ["物料中心", "OMS 物料同步失败"]);
  } finally {
    button.disabled = false;
  }
});

async function refreshProductsSku() {
  const data = await api(`/api/products/sku?${queryFromState(tableStates.productsSku)}`);
  const rows = data.items || [];
  cacheProductSkuRows(rows);

  // 伪装 SPU 数据缓存，以复用原别名编辑器
  rows.forEach(row => {
    if (row.spu_uuid) {
      const fakeSpu = {
        id: row.spu_uuid,
        spu_id: row.spu_id || "",
        name: row.spu_name || "",
        brand: row.brand || "",
        category: row.category || "",
        review_aliases: row.review_aliases || []
      };
      window._productSpuRows = window._productSpuRows || {};
      window._productSpuRows[row.spu_uuid] = fakeSpu;
      window._productSpuRowsByCode = window._productSpuRowsByCode || {};
      window._productSpuRowsByCode[productLookupKey(row.spu_id)] = fakeSpu;
    }
  });

  productCenterState.sku = productPayloadTotal(data, rows);
  renderProductCenterSummary();
  $("#products-sku-list").innerHTML = rows.map(row => {
    let attrs = row.attributes || {};
    if (typeof attrs === "string") {
      try { attrs = JSON.parse(attrs); } catch (_) { attrs = {}; }
    }
    const enName = attrs.oms_en_name || "";
    const aliases = row.review_aliases || [];
    const model = row.model || "-";
    const brand = row.brand || "-";
    return `
    <div class="row product-row">
      <div><strong>${h(row.spu_name || row.sku_id)}</strong><br /><small><a href="#" class="link" data-action="view-product-detail" data-sku-id="${h(row.sku_id)}">${h(row.sku_id)}</a></small></div>
      <div>${enName ? h(enName) : `<small>-</small>`}</div>
      <div>${aliases.length ? aliases.map(a => `<span class="alias-tag">${h(a)}</span>`).join("") : `<small>-</small>`}</div>
      <div><small>${h(model)} · ${h(brand)}</small></div>
      <div>
        <a href="#" class="link" data-action="edit-product-aliases" data-id="${h(row.spu_uuid)}">别名</a> |
        <a href="#" class="link" data-action="quick-new-pricing" data-sku-uuid="${h(row.id)}" data-sku-id="${h(row.sku_id)}">价格</a> |
        <a href="#" class="link" data-action="view-product-detail" data-sku-id="${h(row.sku_id)}">详情</a>
      </div>
    </div>
  `}).join("") || `<div class="row product-row product-empty-row"><div>暂无物料数据</div></div>`;
  renderListPagination("#products-sku-pagination", "productsSku", data);
}

function inventoryAlertLabel(level) {
  if (level === "zero") return "无库存";
  if (level === "low") return "低库存";
  return "正常";
}

function inventoryMeasureLabel(type) {
  if (type === "length") return "按长度计物料";
  if (type === "weight") return "按重量计物料";
  if (type === "other") return "其他非统计类";
  return "可计数物料";
}

async function refreshInventoryWarehouseSuggestions(q = "") {
  const node = $("#inventory-warehouse-suggestions");
  if (!node) return;
  const requestSeq = ++inventoryWarehouseSuggestSeq;
  const params = new URLSearchParams({ q: String(q || "").trim(), limit: "50" });
  const data = await api(`/api/products/inventory/warehouses?${params}`);
  if (requestSeq !== inventoryWarehouseSuggestSeq) return;
  node.innerHTML = (data.items || []).map((row) => {
    const code = row.warehouse_code || "";
    const name = row.warehouse_name || "";
    const label = row.label || [code, name].filter(Boolean).join(" · ");
    return `<option value="${h(code)}" label="${h(label)}">${h(label)}</option>`;
  }).join("");
}

function queueInventoryWarehouseSuggestions(value = "") {
  window.clearTimeout(inventoryWarehouseSuggestTimer);
  inventoryWarehouseSuggestTimer = window.setTimeout(() => {
    guardedAction(["库存管理", "仓库联想"], async () => refreshInventoryWarehouseSuggestions(value));
  }, 160);
}

document.querySelectorAll('input[list="inventory-warehouse-suggestions"]').forEach((input) => {
  input.addEventListener("focus", () => queueInventoryWarehouseSuggestions(input.value));
  input.addEventListener("input", () => queueInventoryWarehouseSuggestions(input.value));
});

function syncFinishedInventoryFilters(resetPage = false) {
  const source = tableStates.productsInventory;
  const target = tableStates.productsFinishedInventory;
  if (!source || !target) return;
  for (const key of ["q", "warehouse_code", "low_stock_only", "threshold"]) {
    target[key] = source[key] || "";
  }
  target.countable_only = "false";
  target.measure_type = "";
  target.inventory_scope = "finished";
  if (resetPage) target.page = 1;
}

function renderInventorySummary(selector, summary, totalLabel) {
  const node = $(selector);
  if (!node) return;
  node.innerHTML = `
    <div class="metric"><small>库存记录</small><strong>${h(summary.total_rows || 0)}</strong></div>
    <div class="metric warn"><small>预警记录</small><strong>${h(summary.low_stock_count || 0)}</strong></div>
    <div class="metric danger"><small>零库存记录</small><strong>${h(summary.zero_stock_count || 0)}</strong></div>
    <div class="metric"><small>${h(totalLabel)}</small><strong>${h(summary.total_base_qty || 0)}</strong></div>
  `;
}

function renderInventoryTypeRows(listSelector, rows, emptyText, tableKey, itemLabel = "物料") {
  const node = $(listSelector);
  if (!node) return;
  node.innerHTML = rows.map(row => `
    <div class="row product-row clickable-row" role="button" tabindex="0" title="查看该类型下的库存明细" data-action="open-inventory-detail" data-table-key="${h(tableKey)}" data-material-type="${h(row.material_type)}" data-parent-category="${h(row.parent_category || "")}">
      <div><strong>${h(row.material_type)}</strong><br /><small>大类：${h(row.parent_category || "未分类")}</small></div>
      <div><strong>${h(row.material_count)} 个${h(itemLabel)}</strong><br /><small>${h(row.inventory_row_count)} 条库存记录 / ${h(row.warehouse_count)} 个仓库</small></div>
      <div><strong>${h(row.base_qty)}</strong><br /><small>辅助数量 ${h(row.qty)}</small></div>
      <div><span class="status-pill ${row.alert_level === "ok" ? "is-active" : "is-warn"}">${h(inventoryAlertLabel(row.alert_level))}</span><br /><small>无库存记录 ${h(row.zero_stock_count)} / 低库存记录 ${h(row.low_stock_count)} · ${h(formatTime(row.synced_at))}</small></div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>${h(emptyText)}</div></div>`;
}

async function refreshProductsInventory() {
  // 1. Fetch unique warehouses to populate dropdown if empty
  const selectNode = $("#inventory-warehouse-select");
  if (selectNode && selectNode.options.length <= 1) {
    try {
      const whData = await api("/api/products/inventory/warehouses?limit=100");
      const items = whData.items || [];
      selectNode.innerHTML = '<option value="">全部仓库</option>' + items.map(wh => `
        <option value="${h(wh.warehouse_code)}">${h(wh.warehouse_name)}</option>
      `).join("");
      // Restore selected value if set in state
      selectNode.value = tableStates.productsInventory.warehouse_code || "";
    } catch (e) {
      notifyError(e, ["物料中心", "加载仓库列表失败"]);
    }
  }

  // 2. Fetch inventory list
  const state = tableStates.productsInventory;
  const params = new URLSearchParams({
    q: state.q || "",
    warehouse_code: state.warehouse_code || "",
    countable_only: "false",
    inventory_scope: "",
    page: state.page || 1,
    page_size: state.page_size || 20
  });
  
  try {
    const data = await api(`/api/products/inventory?${params.toString()}`);
    const rows = data.items || [];
    const summary = data.summary || {};
    
    // Update top summary metrics in productCenterState
    productCenterState.materialLowStock = Number(summary.low_stock_count || 0);
    productCenterState.materialZeroStock = Number(summary.zero_stock_count || 0);
    productCenterState.finishedLowStock = 0;
    productCenterState.finishedZeroStock = 0;
    renderProductCenterSummary();
    
    // Render inventory statistics cards
    const summaryNode = $("#products-inventory-summary");
    if (summaryNode) {
      summaryNode.innerHTML = `
        <div class="metric"><small>库存记录</small><strong>${h(summary.total_rows || 0)}</strong></div>
        <div class="metric warn"><small>预警记录</small><strong>${h(summary.low_stock_count || 0)}</strong></div>
        <div class="metric danger"><small>零库存记录</small><strong>${h(summary.zero_stock_count || 0)}</strong></div>
        <div class="metric"><small>库存数量合计</small><strong>${h(summary.total_base_qty || 0)}</strong></div>
      `;
    }
    
    // Render inventory rows
    const listNode = $("#products-inventory-list");
    if (listNode) {
      listNode.innerHTML = rows.map(row => `
        <div class="row product-row" style="display: grid; grid-template-columns: 1.2fr 1.8fr 1.8fr 1fr 1fr 1fr 1fr 1.2fr 1.5fr; gap: 8px; align-items: center; border-bottom: 1px solid var(--line); padding: 8px 0;">
          <div style="word-break: break-all;"><strong>${h(row.material_code)}</strong></div>
          <div style="word-break: break-word;">${h(row.material_name)}</div>
          <div style="word-break: break-word;">${h(row.english_name || "-")}</div>
          <div>${h(row.model || "-")}</div>
          <div>${h(row.warehouse_name)}</div>
          <div style="font-weight: 600;">${h(row.base_qty)}</div>
          <div style="color: var(--muted);">${h(row.in_transit_qty)}</div>
          <div>
            <span class="status-pill ${row.warning_status === '正常' ? 'is-active' : 'is-warn'}">
              ${h(row.warning_status)}
            </span>
          </div>
          <div style="color: var(--muted); font-size: 11px;">${h(formatTime(row.synced_at))}</div>
        </div>
      `).join("") || `<div class="row product-row product-empty-row"><div>暂无库存数据，请先导入海外库存 Excel</div></div>`;
    }
    
    renderListPagination("#products-inventory-pagination", "productsInventory", data);
  } catch (error) {
    notifyError(error, ["物料中心", "加载库存数据失败"]);
  }
}

async function refreshProductsFinishedInventory() {
  // No-op in restructured inventory
}

async function openInventoryDetail(materialType, parentCategory = "", page = 1, tableKey = "productsInventory") {
  const modal = $("#inventory-detail-modal");
  if (!modal || !materialType) return;
  const sourceState = tableStates[tableKey] || tableStates.productsInventory;
  inventoryDetailState = {
    table_key: tableKey,
    material_type: materialType,
    parent_category: parentCategory,
    q: "",
    warehouse_code: sourceState.warehouse_code || "",
    stock_status: sourceState.low_stock_only ? "low" : "",
    page,
    page_size: inventoryDetailState?.page_size || 100,
  };
  $("#inventory-detail-title").textContent = materialType;
  $("#inventory-detail-meta").textContent = parentCategory ? `库存管理 · ${parentCategory}` : "库存管理";
  $("#inventory-detail-summary").innerHTML = `<div class="empty-note">正在加载库存明细...</div>`;
  $("#inventory-detail-list").innerHTML = "";
  $("#inventory-detail-pagination").innerHTML = "";
  syncInventoryDetailFilterForm();
  modal.hidden = false;
  await refreshInventoryDetail();
}

function syncInventoryDetailFilterForm() {
  const form = $("#inventory-detail-filter-form");
  if (!form || !inventoryDetailState) return;
  form.q.value = inventoryDetailState.q || "";
  form.warehouse_code.value = inventoryDetailState.warehouse_code || "";
  form.stock_status.value = inventoryDetailState.stock_status || "";
}

async function refreshInventoryDetail() {
  if (!inventoryDetailState) return;
  const sourceState = tableStates[inventoryDetailState.table_key] || tableStates.productsInventory;
  const params = new URLSearchParams({
    material_type: inventoryDetailState.material_type,
    parent_category: inventoryDetailState.parent_category || "",
    measure_type: sourceState.measure_type || "",
    inventory_scope: sourceState.inventory_scope || "",
    threshold: sourceState.threshold || "1",
    page: String(inventoryDetailState.page || 1),
    page_size: String(inventoryDetailState.page_size || 100),
  });
  if (sourceState.countable_only !== undefined) params.set("countable_only", sourceState.countable_only);
  if (inventoryDetailState.q) params.set("q", inventoryDetailState.q);
  if (inventoryDetailState.warehouse_code) params.set("warehouse_code", inventoryDetailState.warehouse_code);
  if (inventoryDetailState.stock_status) params.set("stock_status", inventoryDetailState.stock_status);
  const data = await api(`/api/products/inventory/type-items?${params}`);
  inventoryDetailState.page = data.page || 1;
  inventoryDetailState.page_size = data.page_size || inventoryDetailState.page_size || 100;
  const summary = data.summary || {};
  $("#inventory-detail-summary").innerHTML = `
    <div class="metric"><small>库存记录</small><strong>${h(summary.inventory_row_count || 0)}</strong></div>
    <div class="metric"><small>物料 / 仓库</small><strong>${h(summary.material_count || 0)} / ${h(summary.warehouse_count || 0)}</strong></div>
    <div class="metric"><small>库存合计</small><strong>${h(summary.base_qty || 0)}</strong></div>
    <div class="metric warn"><small>无库存 / 低库存</small><strong>${h(summary.zero_stock_count || 0)} / ${h(summary.low_stock_count || 0)}</strong></div>
  `;
  $("#inventory-detail-list").innerHTML = (data.items || []).map(row => `
    <div class="row inventory-detail-row ${Number(row.base_qty || 0) <= 0 ? "is-zero-stock" : ""}">
      <div><strong>${h(row.material_code)}</strong><br /><small>${h(row.material_name)}</small></div>
      <div><strong>${h(row.base_qty)}</strong><br /><small>辅助数量 ${h(row.qty)}</small></div>
      <div><strong>${h(row.warehouse_name || row.warehouse_code)}</strong><br /><small>${h(row.warehouse_code)}</small></div>
      <div><span class="status-pill ${row.alert_level === "ok" ? "is-active" : "is-warn"}">${h(inventoryAlertLabel(row.alert_level))}</span><br /><small>${h(formatTime(row.synced_at))}</small></div>
    </div>
  `).join("") || `<div class="row inventory-detail-row product-empty-row"><div>该中类下暂无库存明细</div></div>`;
  renderInventoryDetailPagination(data);
}

function renderInventoryDetailPagination(data) {
  const node = $("#inventory-detail-pagination");
  if (!node || !inventoryDetailState) return;
  const total = data.total || 0;
  const page = data.page || 1;
  const totalPages = data.total_pages || 1;
  node.innerHTML = `
    <div class="pagination-summary">共 ${h(total)} 条 · 第 ${h(page)} / ${h(totalPages)} 页</div>
    <div class="pagination-controls">
      <button class="button ghost" type="button" data-inventory-detail-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <button class="button ghost" type="button" data-inventory-detail-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      <label>每页
        <select data-inventory-detail-page-size>
          ${[50, 100, 200, 500].map((size) => `<option value="${size}" ${Number(inventoryDetailState.page_size) === size ? "selected" : ""}>${size}</option>`).join("")}
        </select>
      </label>
    </div>
  `;
}

function closeInventoryDetail() {
  const modal = $("#inventory-detail-modal");
  if (modal) modal.hidden = true;
  inventoryDetailState = null;
}

function closeInventoryClassificationDiagnostics() {
  const modal = $("#inventory-classification-modal");
  if (modal) modal.hidden = true;
}

function renderInventoryClassificationCounter(selector, rows, emptyText = "暂无数据") {
  const node = $(selector);
  if (!node) return;
  node.innerHTML = rows.map((row) => `
    <div class="classification-counter-row">
      <div><strong>${h(row.label)}</strong><br /><small>${h(row.note || "")}</small></div>
      <strong>${h(row.value)}</strong>
    </div>
  `).join("") || `<div class="empty-note">${h(emptyText)}</div>`;
}

function renderInventoryClassificationDiagnostics(data) {
  const summary = data?.summary || {};
  const scopes = summary.scope_counts || {};
  const measures = summary.measure_counts || {};
  const finishedMeasures = measures.finished || {};
  const materialMeasures = measures.non_finished || {};
  $("#inventory-classification-meta").textContent = `规则版本 V${data?.rules?.version || 1}`;
  $("#inventory-classification-summary").innerHTML = `
    <div class="metric"><small>库存记录</small><strong>${h(summary.total_inventory_rows || 0)}</strong></div>
    <div class="metric"><small>成品 / 材料</small><strong>${h(scopes.finished || 0)} / ${h(scopes.non_finished || 0)}</strong></div>
    <div class="metric"><small>成品可计数</small><strong>${h(finishedMeasures.countable || 0)}</strong></div>
    <div class="metric ${summary.suspicious_sample_count ? "warn" : ""}"><small>疑似误分</small><strong>${h(summary.suspicious_sample_count || 0)}</strong></div>
  `;
  const measureRows = [];
  for (const scope of [
    ["成品库存", finishedMeasures],
    ["材料库存", materialMeasures],
  ]) {
    for (const type of ["countable", "length", "weight", "other"]) {
      if (!scope[1][type]) continue;
      measureRows.push({
        label: `${scope[0]} · ${inventoryMeasureLabel(type)}`,
        value: scope[1][type],
      });
    }
  }
  renderInventoryClassificationCounter("#inventory-classification-measures", measureRows);
  const reasonRows = Object.entries(summary.reason_counts || {})
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
    .slice(0, 12)
    .map(([key, value]) => {
      const [type, reason] = String(key).split(":");
      return { label: inventoryMeasureLabel(type), note: reason || key, value };
    });
  renderInventoryClassificationCounter("#inventory-classification-reasons", reasonRows);

  const suspicious = data?.suspicious_samples || [];
  $("#inventory-classification-suspicious").innerHTML = suspicious.map((row) => `
    <div class="classification-suspicious-row">
      <div><strong>${h(row.material_code)}</strong><br /><small>${h(row.category || "未分类")}</small></div>
      <div><strong>${h(row.material_name)}</strong></div>
      <div><strong>${h(inventoryMeasureLabel(row.measure_type))}</strong></div>
      <div><small>${h(row.reason || "-")}</small><br /><small>${h(row.matched || "")}</small></div>
    </div>
  `).join("") || `<div class="empty-note">未发现疑似误分样本</div>`;

  const rules = data?.rules || {};
  const ruleSummary = {
    version: rules.version || 1,
    finished_categories: rules.finished_categories || [],
    countable_category_keywords: rules.countable_category_keywords || [],
    length_category_keywords: rules.length_category_keywords || [],
    weight_category_keywords: rules.weight_category_keywords || [],
    other_category_keywords: rules.other_category_keywords || [],
  };
  $("#inventory-classification-rules").textContent = JSON.stringify(ruleSummary, null, 2);
}

function bindInventoryListOpen(selector) {
  $(selector)?.addEventListener("click", async (event) => {
    const row = event.target.closest("[data-action='open-inventory-detail']");
    if (!row) return;
    await guardedAction(["库存管理", "查看明细"], async () => openInventoryDetail(row.dataset.materialType, row.dataset.parentCategory || "", 1, row.dataset.tableKey || "productsInventory"));
  });

  $(selector)?.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const row = event.target.closest("[data-action='open-inventory-detail']");
    if (!row) return;
    event.preventDefault();
    await guardedAction(["库存管理", "查看明细"], async () => openInventoryDetail(row.dataset.materialType, row.dataset.parentCategory || "", 1, row.dataset.tableKey || "productsInventory"));
  });
}

bindInventoryListOpen("#products-inventory-list");
bindInventoryListOpen("#products-finished-inventory-list");

$("#inventory-detail-pagination")?.addEventListener("click", async (event) => {
  const target = event.target.closest("button[data-inventory-detail-page]");
  if (!target || target.disabled || !inventoryDetailState) return;
  inventoryDetailState.page = Number(target.dataset.inventoryDetailPage || 1);
  await guardedAction(["库存管理", "明细翻页"], refreshInventoryDetail);
});

$("#inventory-detail-pagination")?.addEventListener("change", async (event) => {
  if (!event.target.matches("[data-inventory-detail-page-size]") || !inventoryDetailState) return;
  inventoryDetailState.page_size = Number(event.target.value || 100);
  inventoryDetailState.page = 1;
  await guardedAction(["库存管理", "明细分页"], refreshInventoryDetail);
});

$("#inventory-detail-filter-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!inventoryDetailState) return;
  const form = new FormData(event.currentTarget);
  inventoryDetailState.q = String(form.get("q") || "").trim();
  inventoryDetailState.warehouse_code = String(form.get("warehouse_code") || "").trim();
  inventoryDetailState.stock_status = String(form.get("stock_status") || "").trim();
  inventoryDetailState.page = 1;
  await guardedAction(["库存管理", "明细筛选"], refreshInventoryDetail);
});

$("[data-inventory-detail-reset]")?.addEventListener("click", async () => {
  if (!inventoryDetailState) return;
  inventoryDetailState.q = "";
  inventoryDetailState.warehouse_code = "";
  inventoryDetailState.stock_status = "";
  inventoryDetailState.page = 1;
  syncInventoryDetailFilterForm();
  await guardedAction(["库存管理", "重置明细筛选"], refreshInventoryDetail);
});

$("#inventory-classification-diagnostics")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const modal = $("#inventory-classification-modal");
  button.disabled = true;
  if (modal) modal.hidden = false;
  $("#inventory-classification-meta").textContent = "正在检查";
  $("#inventory-classification-summary").innerHTML = `<div class="empty-note">正在检查库存分类规则...</div>`;
  $("#inventory-classification-measures").innerHTML = "";
  $("#inventory-classification-reasons").innerHTML = "";
  $("#inventory-classification-suspicious").innerHTML = "";
  $("#inventory-classification-rules").textContent = "";
  try {
    const result = await api("/api/products/inventory/classification-rules");
    renderInventoryClassificationDiagnostics(result);
    const suspiciousCount = result?.summary?.suspicious_sample_count || 0;
    toast(suspiciousCount ? `分类诊断完成：发现 ${suspiciousCount} 条疑似样本` : "分类诊断完成：未发现疑似误分样本");
  } catch (error) {
    notifyError(error, ["库存管理", "分类诊断失败"]);
    $("#inventory-classification-summary").innerHTML = `<div class="empty-note">${h(messageFromError(error))}</div>`;
  } finally {
    button.disabled = false;
  }
});

$("#sync-erp-inventory")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const resultNode = $("#erp-inventory-sync-result");
  button.disabled = true;
  resultNode.classList.add("show");
  resultNode.textContent = "正在从 ERP 同步库存...";
  try {
    const result = await api("/api/products/inventory/erp-sync", { method: "POST" });
    resultNode.textContent = JSON.stringify(result, null, 2);
    toast(result.ok ? `ERP 库存同步完成：${result.total || 0} 条` : "ERP 库存同步失败");
    await refreshProductsInventory();
  } catch (error) {
    notifyError(error, ["物料中心", "ERP 库存同步失败"]);
    resultNode.textContent = messageFromError(error);
  } finally {
    button.disabled = false;
  }
});

async function refreshProductsPricing() {
  const data = await api(`/api/pricing?${queryFromState(tableStates.productsPricing)}`);
  const rows = data.items || [];
  cacheProductSkuRows(rows);
  $("#products-pricing-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div><strong>${h(row.channel)}</strong> / <a href="#" class="link" data-action="goto-sku" data-sku="${h(row.sku_id)}">${h(row.sku_id)}</a><br /><small>${h(row.spu_name || row.spu_id || row.currency)}</small></div>
      <div>
        <small>A: ${h(formatProductMoney(row.tier_a_price))} | B: ${h(formatProductMoney(row.tier_b_price))} | C: ${h(formatProductMoney(row.tier_c_price))}</small>
        ${row.promo_start_time ? `<br/><small class="text-secondary">${h(formatTime(row.promo_start_time))} 至 ${h(formatTime(row.promo_end_time))}</small>` : ""}
      </div>
      <div><strong>${h(formatProductMoney(row.map_price))}</strong></div>
      <div><small>${h(formatTime(row.updated_at))}</small></div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>暂无定价数据</div></div>`;
  renderListPagination("#products-pricing-pagination", "productsPricing", data);
}

async function refreshProductsPromotions() {
  const data = await api(`/api/promotions?${queryFromState(tableStates.productsPromotions)}`);
  const rows = data.items || [];
  cacheProductSkuRows(rows);
  $("#products-promotions-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div>
        <strong>${h(row.name)}</strong><br />
        <small>${h(row.sku_id || "未绑定")} · ${h(row.spu_name || row.channel || "通用")}</small>
        ${row.binding_valid === false ? `<br /><span class="status-pill is-danger">${h(row.binding_label || "需绑定成品 SKU")}</span>` : ""}
      </div>
      <div><small>${h(row.discount_type === 'percentage' ? '比例折扣' : '固定减免')}</small><br /><strong>${formatPromotionDiscount(row)}</strong></div>
      <div><small>${h(row.start_time ? formatTime(row.start_time) : "不限")} - ${h(row.end_time ? formatTime(row.end_time) : "不限")}</small></div>
      <div>
        <small>${row.is_active ? "生效中" : "已停用"}</small><br/>
        <div class="row-actions">
          <a href="#" class="link" data-action="edit-promotion" data-id="${row.id}">编辑</a>
          <a href="#" class="link" data-action="toggle-promotion" data-id="${row.id}" data-active="${row.is_active}">
            ${row.is_active ? "失效" : "生效"}
          </a>
          <a href="#" class="link text-danger" data-action="delete-promotion" data-id="${row.id}">删除</a>
        </div>
      </div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>暂无促销数据</div></div>`;
  
  // Store rule data in a global map to avoid escaping issues in HTML attributes
  window._promotionRules = window._promotionRules || {};
  rows.forEach(r => { window._promotionRules[r.id] = r; });

  renderListPagination("#products-promotions-pagination", "productsPromotions", data);
}

function activateScopedTab(prefix, tabsNode, activeButton) {
  const tabName = activeButton.dataset.tab;
  tabsNode.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button === activeButton));
  document.querySelectorAll(`[id^='${prefix}-'][id$='-tab']`).forEach((el) => {
    el.hidden = true;
    el.classList.remove("is-active");
  });
  const activeTab = $(`#${prefix}-${tabName}-tab`);
  if (activeTab) {
    activeTab.hidden = false;
    activeTab.classList.add("is-active");
  }
  return tabName;
}

// Product Tab Logic
$("#products-tabs")?.addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  const tabName = activateScopedTab("products", e.currentTarget, e.target);
  if (tabName === "inventory") {
    guardedAction(["物料中心", "库存管理"], async () => { await refreshProductsInventory(); await refreshProductsFinishedInventory(); });
  }
  if (tabName === "review") {
    guardedAction(["物料中心", "预审体检"], async () => refreshProductReviewReadiness());
  }
});

$("#integration-tabs")?.addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  activateScopedTab("integration", e.currentTarget, e.target);
});

// Product Modals
function openModal(id) {
  const modal = $(id);
  if (modal) modal.hidden = false;
}
function closeModal(id) {
  const modal = $(id);
  if (modal) modal.hidden = true;
}

document.addEventListener("click", async (e) => {
  const target = e.target;
  if (target.dataset.action === "new-spu") openModal("#product-spu-modal");
  if (target.dataset.action === "new-sku") {
    $("#product-sku-form")?.reset();
    openModal("#product-sku-modal");
  }
  if (target.dataset.action === "new-pricing") {
    $("#product-pricing-form")?.reset();
    openModal("#product-pricing-modal");
  }
  if (target.dataset.action === "new-promotion") {
    e.preventDefault();
    editingPromotionId = null;
    $("#product-promotion-title").innerText = "新增促销规则";
    $("#product-promotion-form").reset();
    openModal("#product-promotion-modal");
  }
  if (target.dataset.action === "import-excel") {
    $("#product-import-preview-container").hidden = true;
    openModal("#product-import-modal");
  }

  if (target.dataset.action === "goto-spu") {
    e.preventDefault();
    const spuId = target.dataset.spu;
    tableStates.productsSpu.q = spuId;
    tableStates.productsSpu.page = 1;
    $("#products-spu-filter-form [name=q]").value = spuId;
    // SPU tab 没有可见按钮，直接操作 DOM 切换
    document.querySelectorAll("#products-tabs button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll("[id^='products-'][id$='-tab']").forEach(el => { el.hidden = true; el.classList.remove("is-active"); });
    const spuTab = $("#products-spu-tab");
    if (spuTab) { spuTab.hidden = false; spuTab.classList.add("is-active"); }
    guardedAction(["物料中心", "SPU"], async () => refreshProductsSpu());
  }

  if (target.dataset.action === "goto-sku") {
    e.preventDefault();
    const skuId = target.dataset.sku;
    tableStates.productsPricing.q = skuId;
    tableStates.productsPricing.page = 1;
    $("#products-pricing-filter-form [name=q]").value = skuId;
    document.querySelectorAll("#products-tabs button").forEach(b => {
      if (b.dataset.tab === "pricing") b.click();
    });
  }

  if (target.dataset.action === "quick-new-pricing") {
    e.preventDefault();
    openProductPricingModal({
      skuUuid: target.dataset.skuUuid,
      skuId: target.dataset.skuId,
      channel: target.dataset.channel || "default",
      unitPrice: target.dataset.unitPrice || "",
      pricingConfigured: target.dataset.pricingConfigured === "true",
    });
  }

  if (target.dataset.action === "edit-product-aliases") {
    e.preventDefault();
    openProductAliasModal(target.dataset.id);
  }

  if (target.dataset.action === "view-product-detail") {
    e.preventDefault();
    openProductDetailModal(target.dataset.skuId);
  }

  if (target.dataset.action === "goto-promotions") {
    e.preventDefault();
    const query = target.dataset.q || "";
    tableStates.productsPromotions.q = query;
    tableStates.productsPromotions.page = 1;
    const formInput = $("#products-promotions-filter-form [name=q]");
    if (formInput) formInput.value = query;
    document.querySelectorAll("#products-tabs button").forEach(b => {
      if (b.dataset.tab === "promotions") b.click();
    });
  }

  if (target.dataset.action === "use-review-alias-suggestion") {
    e.preventDefault();
    openProductAliasSuggestion(target.dataset.id, target.dataset.alias || "");
  }

  if (target.dataset.action === "edit-promotion") {
    e.preventDefault();
    const id = target.dataset.id;
    const rule = window._promotionRules[id];
    if (!rule) return;
    editingPromotionId = rule.id;
    openModal("#product-promotion-modal");
    $("#product-promotion-title").innerText = "编辑促销规则";
    const form = $("#product-promotion-form");
    form.elements.sku_uuid.value = rule.sku_uuid || "";
    form.elements.sku_lookup.value = rule.sku_id || "";
    form.elements.name.value = rule.name || "";
    form.elements.channel.value = rule.channel || "";
    form.elements.discount_type.value = rule.discount_type || "percentage";
    form.elements.discount_value.value = promotionDiscountInputValue(rule);
    form.elements.start_time.value = rule.start_time ? rule.start_time.slice(0, 16) : "";
    form.elements.end_time.value = rule.end_time ? rule.end_time.slice(0, 16) : "";
    form.elements.priority.value = rule.priority || 0;
  }

  if (target.dataset.action === "delete-promotion") {
    e.preventDefault();
    const id = target.dataset.id;
    if (!confirm("确定要删除该促销规则吗？")) return;
    await guardedAction(["物料中心", "删除促销规则"], async () => {
      await api(`/api/promotions/${id}`, { method: "DELETE" });
      toast("删除成功");
      refreshProductsPromotions();
    });
  }

  if (target.dataset.action === "toggle-promotion") {
    e.preventDefault();
    const id = target.dataset.id;
    const isActive = target.dataset.active === "true";
    await guardedAction(["物料中心", "切换状态"], async () => {
      await api(`/api/promotions/${id}/toggle?is_active=${!isActive}`, { method: "POST" });
      toast("操作成功");
      refreshProductsPromotions();
    });
  }

  if (target.dataset.action === "view-crm-order") {
    e.preventDefault();
    await guardedAction(["订单管理", "查看订单详情"], async () => {
      await openCrmOrderDetail(target.dataset.id);
    });
  }

  if (target.dataset.action === "delete-crm-order-local") {
    e.preventDefault();
    await guardedAction(["订单管理", "删除本地 CRM 订单"], async () => {
      await deleteCrmOrderLocal(target.dataset.id, target.dataset.no || "该订单");
    });
  }

  if (target.dataset.action === "queue-crm-v2") {
    e.preventDefault();
    await guardedAction(["订单管理", "V2 入中台"], async () => {
      const result = await api(`/api/crm/orders/${target.dataset.id}/queue-v2`, { method: "POST" });
      toast(`已投递中台事件：${result.job_id}`);
      await refreshCrmOrders();
    });
  }

  if (target.dataset.action === "process-crm-v2") {
    e.preventDefault();
    await guardedAction(["订单管理", "V2 立即预审"], async () => {
      const result = await api(`/api/crm/orders/${target.dataset.id}/process-v2`, { method: "POST" });
      toast(`中台状态：${result.status}`);
      await refreshCrmOrders();
      await refreshExceptions();
      await refreshJobs();
    });
  }
});

$("#product-spu-close")?.addEventListener("click", () => closeModal("#product-spu-modal"));
$("#product-sku-close")?.addEventListener("click", () => closeModal("#product-sku-modal"));
$("#product-alias-close")?.addEventListener("click", () => closeModal("#product-alias-modal"));
$("#product-pricing-close")?.addEventListener("click", () => closeModal("#product-pricing-modal"));
$("#product-promotion-close")?.addEventListener("click", () => closeModal("#product-promotion-modal"));
$("#product-import-close")?.addEventListener("click", () => closeModal("#product-import-modal"));
$("#product-detail-close")?.addEventListener("click", () => closeModal("#product-detail-modal"));

async function openProductDetailModal(skuId) {
  if (!skuId) return;
  openModal("#product-detail-modal");
  
  const lookupKey = productLookupKey(skuId);
  const sku = (window._productSkuRowsByCode || {})[lookupKey] || { sku_id: skuId };
  
  $("#detail-sku-id").textContent = sku.sku_id || "-";
  $("#detail-spu-name").textContent = sku.spu_name || "-";
  $("#detail-model").textContent = sku.model || "-";
  $("#detail-brand").textContent = sku.brand || "-";
  $("#detail-category").textContent = sku.category || "-";
  
  const aliases = sku.review_aliases || [];
  $("#detail-aliases").textContent = aliases.length ? aliases.join(" / ") : "无别名";
  
  const loader = $("#product-detail-modal .stock-loader");
  const tableWrapper = $("#detail-stock-table-wrapper");
  const errorNode = $("#detail-stock-error");
  const tbody = $("#detail-stock-rows");
  
  if (loader) loader.style.display = "flex";
  if (tableWrapper) tableWrapper.hidden = true;
  if (errorNode) errorNode.hidden = true;
  if (tbody) tbody.innerHTML = "";
  
  try {
    const data = await api(`/api/products/sku/${encodeURIComponent(skuId)}/realtime-stock`);
    if (loader) loader.style.display = "none";
    
    const stocks = data.stocks || [];
    if (stocks.length === 0) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 12px; color: var(--muted);">OMS 中暂无该物料各仓库库存数据</td></tr>`;
    } else {
      if (tbody) {
        tbody.innerHTML = stocks.map(item => {
          const q = item.quantity !== null && item.quantity !== undefined ? h(item.quantity) : "-";
          const uq = item.usable_quantity !== null && item.usable_quantity !== undefined ? h(item.usable_quantity) : "-";
          const eq = item.excel_qty !== null && item.excel_qty !== undefined ? h(item.excel_qty) : "-";
          return `
            <tr style="border-bottom: 1px solid var(--line);">
              <td style="padding: 8px 6px;">${h(item.warehouse_code)}</td>
              <td style="padding: 8px 6px;">${h(item.warehouse_name)}</td>
              <td style="padding: 8px 6px; font-weight: 600;">${q}</td>
              <td style="padding: 8px 6px; font-weight: 600; color: var(--success);">${uq}</td>
              <td style="padding: 8px 6px; font-weight: 600; color: var(--accent);">${eq}</td>
            </tr>
          `;
        }).join("");
      }
    }
    if (tableWrapper) tableWrapper.hidden = false;
  } catch (error) {
    if (loader) loader.style.display = "none";
    if (errorNode) {
      errorNode.hidden = false;
      errorNode.textContent = `获取 OMS 实时库存失败：${error.message || "未知接口错误"}`;
    }
  }
}

let currentImportPreviewData = null;
let editingPromotionId = null;

$("#product-import-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  await guardedAction(["物料中心", "导入预览"], async () => {
    const data = await api("/api/products/import/preview", {
      method: "POST",
      body: formData,
    }, true);
    
    currentImportPreviewData = data;
    
    const spuCount = data.spu.new.length + data.spu.conflict.length;
    const skuCount = data.sku.new.length + data.sku.conflict.length;
    const pricingCount = data.pricing.new.length + data.pricing.conflict.length;
    
    $("#product-import-summary").innerText = `解析完毕：发现 SPU ${spuCount} 条（冲突 ${data.spu.conflict.length}），SKU ${skuCount} 条（冲突 ${data.sku.conflict.length}），价格配置 ${pricingCount} 条（冲突 ${data.pricing.conflict.length}）。冲突的数据如果在确认导入后将被覆盖。`;
    
    let conflictHtml = "<strong>冲突项预览（前10条）：</strong><ul>";
    const conflicts = [...data.spu.conflict.map(c => `SPU: ${c.spu_id}`), ...data.sku.conflict.map(c => `SKU: ${c.sku_id}`), ...data.pricing.conflict.map(c => `Pricing: ${c.sku_id} @ ${c.channel}`)];
    if (conflicts.length === 0) {
      conflictHtml += "<li>无冲突数据</li>";
    } else {
      conflicts.slice(0, 10).forEach(c => conflictHtml += `<li>${c}</li>`);
      if (conflicts.length > 10) conflictHtml += `<li>...等 ${conflicts.length} 项</li>`;
    }
    conflictHtml += "</ul>";
    $("#product-import-conflict-list").innerHTML = conflictHtml;
    
    $("#product-import-preview-container").hidden = false;
  });
});

$("#product-import-confirm-btn")?.addEventListener("click", async () => {
  if (!currentImportPreviewData) return;
  await guardedAction(["物料中心", "确认导入"], async () => {
    const result = await api("/api/products/import/confirm", {
      method: "POST",
      body: JSON.stringify(currentImportPreviewData),
    });
    toast(`导入成功！SPU: ${result.counts.spu}, SKU: ${result.counts.sku}, 价格: ${result.counts.pricing}`);
    closeModal("#product-import-modal");
    $("#product-import-form").reset();
    currentImportPreviewData = null;
    $("#product-import-preview-container").hidden = true;
    refreshProductsSpu();
    refreshProductsSku();
    refreshProductsPricing();
  });
});

$("#product-spu-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  await guardedAction(["物料中心", "新增 SPU"], async () => {
    await api("/api/products/spu", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(formData)),
    });
    toast("SPU 创建成功");
    closeModal("#product-spu-modal");
    e.target.reset();
    refreshProductsSpu();
  });
});

$("#product-sku-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData);
  try {
    payload.attributes = payload.attributes ? JSON.parse(payload.attributes) : {};
  } catch (err) {
    toast("属性 JSON 格式错误");
    return;
  }
  await guardedAction(["物料中心", "新增 SKU"], async () => {
    payload.spu_uuid = await resolveProductSpuLookup(e.target);
    delete payload.spu_lookup;
    await api("/api/products/sku", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast("SKU 创建成功");
    closeModal("#product-sku-modal");
    e.target.reset();
    refreshProductsSku();
  });
});

$("#product-alias-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const aliases = String(form.aliases.value || "")
    .split(/\n+/)
    .map(item => item.trim())
    .filter(Boolean);
  await guardedAction(["物料中心", "保存预审别名"], async () => {
    const result = await api(`/api/products/spu/${form.spu_uuid.value}/review-aliases`, {
      method: "PUT",
      body: JSON.stringify({ aliases }),
    });
    const row = window._productSpuRows?.[result.id];
    if (row) row.review_aliases = result.review_aliases || [];
    toast(`已保存 ${result.review_aliases?.length || 0} 个预审别名`);
    closeModal("#product-alias-modal");
    await refreshProductsSpu();
    await refreshProductReviewReadiness();
  });
});

$("#product-pricing-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData);
  ["promo_start_time", "promo_end_time"].forEach(k => {
    if (!payload[k]) payload[k] = null;
  });
  await guardedAction(["物料中心", "配置价格"], async () => {
    payload.sku_uuid = await resolveProductSkuLookup(e.target);
    delete payload.sku_lookup;
    payload.map_price = amountInputToCents(payload.map_price, "底价(MAP)");
    payload.tier_a_price = amountInputToCents(payload.tier_a_price, "A档价格");
    payload.tier_b_price = amountInputToCents(payload.tier_b_price, "B档价格");
    payload.tier_c_price = amountInputToCents(payload.tier_c_price, "C档价格");
    await api("/api/pricing", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast("渠道价格配置成功");
    closeModal("#product-pricing-modal");
    e.target.reset();
    await refreshProductsPricing();
    await refreshProductReviewReadiness();
  });
});

$("#product-promotion-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData);
  payload.priority = parseInt(payload.priority || "0", 10);
  if (isNaN(payload.priority)) payload.priority = 0;
  
  if (!payload.channel) payload.channel = null;
  ["start_time", "end_time"].forEach(k => {
    if (!payload[k]) payload[k] = null;
  });
  
  const label = editingPromotionId ? "编辑促销规则" : "新增促销规则";
  const url = editingPromotionId ? `/api/promotions/${editingPromotionId}` : "/api/promotions";
  const method = editingPromotionId ? "PATCH" : "POST";

  await guardedAction(["物料中心", label], async () => {
    payload.sku_uuid = await resolveProductSkuLookup(e.target);
    delete payload.sku_lookup;
    if (payload.discount_type === "fixed_amount") {
      payload.discount_value = amountInputToCents(payload.discount_value, "固定减免");
    } else {
      const discount = Number(payload.discount_value);
      if (!Number.isFinite(discount) || discount <= 0 || discount > 100) {
        throw new Error("比例折扣请输入 1-100 之间的数字");
      }
      payload.discount_value = Math.round(discount);
    }
    await api(url, {
      method: method,
      body: JSON.stringify(payload),
    });
    toast(`${label}成功`);
    closeModal("#product-promotion-modal");
    e.target.reset();
    editingPromotionId = null;
    $("#product-promotion-title").innerText = "新增促销规则";
    refreshProductsPromotions();
  });
});

// Bind search forms
$("#products-spu-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  tableStates.productsSpu.q = e.target.q.value;
  tableStates.productsSpu.page = 1;
  refreshProductsSpu();
});

$("#products-sku-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  tableStates.productsSku.q = e.target.q.value;
  tableStates.productsSku.crm_semantic = e.target.crm_semantic?.checked ?? false;
  tableStates.productsSku.page = 1;
  refreshProductsSku();
});

$("#products-pricing-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  tableStates.productsPricing.q = e.target.q.value;
  tableStates.productsPricing.page = 1;
  refreshProductsPricing();
});

$("#products-promotions-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  tableStates.productsPromotions.q = e.target.q.value;
  tableStates.productsPromotions.page = 1;
  refreshProductsPromotions();
});

$("#products-review-preview-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const resultNode = $("#products-review-preview-result");
  if (resultNode) {
    resultNode.innerHTML = `<div class="empty-note">正在按订单预审规则匹配成品 SKU...</div>`;
  }
  await guardedAction(["物料中心", "预审测试"], async () => {
    const payload = {
      text: String(form.text.value || "").trim(),
      channel: String(form.channel.value || "default").trim() || "default",
    };
    const result = await api("/api/products/review-preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderProductReviewPreview(result);
  });
});

// 导入海外库存 Excel 文件事件绑定
document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "btn-trigger-import-excel") {
    $("#inventory-excel-file")?.click();
  }
});

$("#inventory-excel-file")?.addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  
  const formData = new FormData();
  formData.append("file", file);
  
  // Clear the input value so that the same file can be selected again
  e.target.value = "";
  
  await guardedAction(["物料中心", "导入海外库存"], async () => {
    toast("正在导入并解析库存数据，请稍候...");
    const res = await api("/api/products/inventory/import-excel", {
      method: "POST",
      body: formData,
      headers: { "Content-Type": undefined } // explicitly override to let fetch set the boundary!
    });
    toast(`成功导入 ${res.imported_count || 0} 条仓库库存记录`);
    await refreshProductsInventory();
  });
});

resetWorkflowChat();
setActivePage();
ensureAuthenticated()
  .then((authenticated) => {
    if (authenticated) {
      setActivePage();
      return refreshAll();
    }
    return null;
  })
  .catch((error) => {
    showLogin();
    toast(error.message || "请先登录");
  });

// ═══════════════════════════════════════
// V2 Phase 1 — 前端新功能
// ═══════════════════════════════════════

// —— 页面切换时初始化 ——
document.addEventListener('click', function(e) {
  var link = e.target.closest('[data-page-link]');
  if (!link) return;
  var page = link.dataset.pageLink;
  setTimeout(function() {
    if (page === 'orders') initOrdersPage();
    else if (page === 'master-data') initMasterDataPage();
    else if (page === 'inventory') initInventoryPage();
  }, 200);
});

// —— Tab 切换 ——
function initTabs(container) {
  container.querySelectorAll('.tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      container.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('is-active'); });
      container.querySelectorAll('.tab-content').forEach(function(tc) { tc.classList.remove('is-active'); });
      this.classList.add('is-active');
      var target = document.querySelector('[data-tab-content="' + this.dataset.tab + '"]');
      if (target) target.classList.add('is-active');
    });
  });
}

// —— API ——
async function apiPost(path, data) {
  return api(path, { method: 'POST', body: JSON.stringify(data), headers: {'Content-Type':'application/json'} });
}

// —— 通知中心 🔔 ——
function refreshBellBadge() {
  api('/api/global-exception-ticker').then(function(data) {
    var count = (data && data.exceptions ? data.exceptions.length : 0);
    var badge = document.getElementById('bell-badge');
    var body = document.getElementById('bell-drawer-body');
    if (!badge || !body) return;
    if (count > 0) {
      badge.textContent = count > 99 ? '99+' : count;
      badge.style.display = 'inline';
      var html = '';
      data.exceptions.forEach(function(ex) {
        var lc = (ex.severity === 'Critical' || ex.severity === 'High') ? 'critical' : 'high';
        html += '<div class="bell-item" data-id="' + ex.id + '">' +
          '<div class="bell-item-header"><span class="bell-item-type">⚠️ ' + (ex.exception_type || '异常') + '</span><span class="bell-item-level ' + lc + '">' + (ex.severity || 'High') + '</span></div>' +
          '<div class="bell-item-order">' + (ex.related_order_no || '') + '</div>' +
          '<div class="bell-item-desc">' + (ex.summary || ex.reason || '').slice(0, 80) + '</div>' +
          '<div class="bell-item-time">' + (ex.created_at || '').slice(0, 16) + '</div></div>';
      });
      body.innerHTML = html;
    } else {
      badge.style.display = 'none';
      body.innerHTML = '<div class="bell-empty">暂无待处理异常</div>';
    }
  }).catch(function() {});
}

function openBell() {
  var d = document.getElementById('bell-drawer');
  var o = document.getElementById('bell-overlay');
  if (d) d.style.display = 'flex';
  if (o) o.style.display = 'block';
  refreshBellBadge();
}
function closeBell() {
  var d = document.getElementById('bell-drawer');
  var o = document.getElementById('bell-overlay');
  if (d) d.style.display = 'none';
  if (o) o.style.display = 'none';
}

(function() {
  var btn = document.getElementById('bell-button');
  if (btn) btn.addEventListener('click', function() {
    var d = document.getElementById('bell-drawer');
    if (!d || d.style.display === 'none') openBell(); else closeBell();
  });
  var c = document.getElementById('bell-drawer-close');
  if (c) c.addEventListener('click', closeBell);
  var va = document.getElementById('bell-view-all');
  if (va) va.addEventListener('click', function() { closeBell(); });
  var ov = document.getElementById('bell-overlay');
  if (ov) ov.addEventListener('click', closeBell);
  setInterval(refreshBellBadge, 30000);
  refreshBellBadge();
})();

// —— 订单处理页面 ——
var _ordersInited = false;
function initOrdersPage() {
  if (_ordersInited) return;
  _ordersInited = true;
  loadOrders();
  var btn = document.getElementById('order-refresh');
  if (btn) btn.addEventListener('click', loadOrders);
  var search = document.getElementById('order-search');
  if (search) search.addEventListener('keydown', function(e) { if (e.key === 'Enter') loadOrders(); });
  ['order-status-filter', 'order-type-filter'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', loadOrders);
  });
}

function orderDetailValue(value) {
  return h(value === undefined || value === null || value === "" ? "-" : value);
}

function orderDetailRow(label, value) {
  return '<div class="order-detail-row"><span class="order-detail-label">' + h(label) + '</span><span class="order-detail-value">' + orderDetailValue(value) + '</span></div>';
}

function orderTypeLabel(type) {
  if (type === 'STOCK_REPLENISHMENT') return '备货';
  if (type === 'SALES_ORDER') return '销售';
  return type || '-';
}

function firstPresent() {
  for (var i = 0; i < arguments.length; i += 1) {
    if (arguments[i] !== undefined && arguments[i] !== null && String(arguments[i]).trim() !== '') return arguments[i];
  }
  return '';
}

function renderOrderBasicCard(detail) {
  return '<section class="order-detail-card">' +
    '<h3>基本信息</h3>' +
    '<div class="order-detail-grid">' +
    orderDetailRow('中台单号', detail.order_no) +
    orderDetailRow('CRM单号', detail.crm_order_no || detail.crm_order_id) +
    orderDetailRow('客户', detail.customer_name) +
    orderDetailRow('销售', detail.sales_user_name) +
    orderDetailRow('类型', orderTypeLabel(detail.order_type)) +
    orderDetailRow('状态', middleOrderStatusLabel(detail.status)) +
    orderDetailRow('下单主体', detail.entity_code) +
    orderDetailRow('出货主体', detail.fulfillment_entity || detail.entity_code) +
    orderDetailRow('渠道', detail.channel_code) +
    orderDetailRow('店铺', detail.shop_code) +
    orderDetailRow('金额', detail.order_amount ? detail.order_amount + (detail.currency ? ' ' + detail.currency : '') : '') +
    orderDetailRow('金蝶单号', detail.erp_bill_no) +
    '</div>' +
    '</section>';
}

function parseRuleValues(code, reason, skuNamesMap) {
  var current = '校验未通过';
  var expected = '符合规则定义';
  skuNamesMap = skuNamesMap || {};

  if (code === "REQUIRED_HEAD_FIELDS") {
    current = '必填头部字段缺失';
    expected = '基础头部字段完整';
  } else if (code === "PHASE1_COMPLETE_PRE_REVIEW_FIELDS") {
    current = '三要素或结算方式等必填项缺失';
    expected = '一期必备信息完整';
  } else if (code === "CUSTOMER_MAPPING") {
    current = '未映射到 OMS 客户主数据';
    expected = '客户主数据已匹配';
  } else if (code === "POSITIVE_ORDER_AMOUNT") {
    current = '金额为 0 或为空';
    expected = '订单金额大于 0';
  } else if (code === "AMOUNT_CONSISTENCY") {
    current = '财务金额核算不一致';
    expected = '金额勾稽关系一致';
  } else if (code === "HAS_ORDER_ITEMS") {
    current = '商品明细为空';
    expected = '包含有效明细行';
  } else if (code === "KNOWN_ACTIVE_SKU") {
    current = '未在主数据启用/不存在';
    expected = 'SKU 存在且已启用';
  } else if (code === "RULE_SKU_BOM_MATCH") {
    current = 'CRM 商品无法匹配标准 SKU/BOM';
    expected = '匹配标准 SKU/BOM';
  } else if (code === "RULE_CONTRACT_AMOUNT_CONSISTENCY") {
    current = '合同解析金额不符';
    expected = '合同金额与订单一致';
  } else if (code === "ATTACHMENT_PRODUCT_CONSISTENCY") {
    current = '附件商品解析匹配失败';
    expected = '附件商品与订单一致';
  } else if (code === "LOCAL_INVENTORY_AVAILABLE") {
    current = '库存不足或无快照';
    expected = '本地库存可用';
  } else if (code === "INVENTORY_THREE_STEP") {
    current = '所有仓库均缺货';
    expected = '本地仓或海外仓有可用库存';
  } else if (code === "CONTRACT_APPROVAL") {
    current = '合同状态未审批';
    expected = '合同状态为已审批';
  }

  if (reason) {
    if (code === "KNOWN_ACTIVE_SKU") {
      var match = reason.match(/SKU 未在主数据启用：(.*)/) || reason.match(/匹配标准 SKU，需人工确认：(.*)/);
      if (match) {
        var rawSku = match[1].trim();
        var name = skuNamesMap[rawSku] || rawSku;
        current = '商品【' + name + '】未在主数据启用/不存在';
      }
    } else if (code === "LOCAL_INVENTORY_AVAILABLE") {
      var match = reason.match(/未找到库存快照：(.*)/);
      if (match) {
        var rawSku = match[1].trim();
        var name = skuNamesMap[rawSku] || rawSku;
        current = '商品【' + name + '】无库存快照';
      } else if (reason.includes("库存可用量不足")) {
        current = '库存量不足';
      }
    } else if (code === "INVENTORY_THREE_STEP") {
      if (reason.includes("均缺货")) {
        current = '各发货仓均处于缺货状态';
      }
    } else if (code === "CONTRACT_APPROVAL") {
      var match = reason.match(/合同审批状态为 \[(.*?)\]/);
      if (match) {
        current = '合同审批状态为 ' + match[1];
      }
    } else if (code === "POSITIVE_ORDER_AMOUNT") {
      if (reason.includes("必须大于 0")) {
        current = '订单金额 <= 0';
      }
    } else if (code === "ATTACHMENT_PRODUCT_CONSISTENCY") {
      var match = reason.match(/不一致：(.*)/);
      if (match) {
        current = match[1];
      }
    } else if (code === "AMOUNT_CONSISTENCY") {
      current = reason;
    }
  }

  return {
    current: current,
    expected: expected
  };
}

function formatCellLines(text) {
  if (!text) return '';
  var lines = text.split('；');
  return lines.map(function(line, idx) {
    if (!line.trim()) return '';
    var suffix = idx < lines.length - 1 ? '；' : '';
    return '<div style="margin-bottom: 6px; line-height: 1.5; font-size: 12px; word-break: break-all;">' + h(line.trim() + suffix) + '</div>';
  }).join('');
}

function renderValidationSummaryTable(failedRules, skuNamesMap) {
  if (!failedRules || !failedRules.length) return '';

  var RULE_NAMES_ZH = {
    "REQUIRED_HEAD_FIELDS": "订单头基础字段",
    "PHASE1_COMPLETE_PRE_REVIEW_FIELDS": "一期完整性预审",
    "CUSTOMER_MAPPING": "客户主数据映射",
    "POSITIVE_ORDER_AMOUNT": "订单金额有效性",
    "AMOUNT_CONSISTENCY": "金额一致性",
    "HAS_ORDER_ITEMS": "订单商品明细",
    "KNOWN_ACTIVE_SKU": "SKU 主数据启用",
    "RULE_SKU_BOM_MATCH": "SKU/BOM 匹配",
    "RULE_CONTRACT_AMOUNT_CONSISTENCY": "合同金额一致性",
    "ATTACHMENT_PRODUCT_CONSISTENCY": "附件商品一致性",
    "LOCAL_INVENTORY_AVAILABLE": "本地库存可用量",
    "INVENTORY_THREE_STEP": "库存三步判断",
    "CONTRACT_APPROVAL": "商务审核前置条件"
  };

  var html = '<div class="validation-summary-table-wrap" style="grid-column: 1 / -1; margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px;">' +
    '<h4 style="font-size: 13px; font-weight: bold; margin-bottom: 8px; color: #ef4444;">❌ 预审未通过细则 (' + failedRules.length + ' 项)</h4>' +
    '<table class="data-table" style="width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; background: #ffffff;">' +
    '<thead>' +
      '<tr style="background: var(--surface-soft); color: var(--ink); border-bottom: 1px solid var(--line);">' +
        '<th style="padding: 8px 10px; font-weight: bold; width: 25%;">未通过规则</th>' +
        '<th style="padding: 8px 10px; font-weight: bold; width: 50%;">现值</th>' +
        '<th style="padding: 8px 10px; font-weight: bold; width: 25%;">预期值</th>' +
      '</tr>' +
    '</thead>' +
    '<tbody>';

  failedRules.forEach(function(rule) {
    var code = rule.rule_code || 'UNKNOWN';
    var ruleName = RULE_NAMES_ZH[code] || code;
    var reason = rule.reason || rule.message || '校验未通过';
    var parsed = parseRuleValues(code, reason, skuNamesMap);
    
    html += '<tr style="border-bottom: 1px solid var(--line);">' +
      '<td style="padding: 10px; color: var(--accent); font-weight: bold; vertical-align: top; line-height: 1.5;">' + h(ruleName) + '</td>' +
      '<td style="padding: 10px; color: #ef4444; vertical-align: top;">' + formatCellLines(parsed.current) + '</td>' +
      '<td style="padding: 10px; color: #10b981; font-weight: bold; vertical-align: top; line-height: 1.5;">' + h(parsed.expected) + '</td>' +
    '</tr>';
  });

  html += '</tbody></table></div>';
  return html;
}

function renderOrderFlowCard(detail) {
  var flow = detail.flow || {};
  var notices = detail.delivery_notices || [];
  var latestNotice = notices[0] || {};
  var validation = flow.validation_summary || detail.validation_summary || {};
  var failedRules = (validation.results || []).filter(function(rule) { return rule && rule.passed === false; });
  var validationText = validation.summary || validation.message || validation.reason || '';

  var skuNamesMap = {};
  (detail.items || []).forEach(function(item) {
    if (item.sku_code && item.product_name) {
      skuNamesMap[String(item.sku_code).trim()] = item.product_name;
    }
  });

  return '<section class="order-detail-card">' +
    '<h3>流程信息</h3>' +
    '<div class="order-detail-grid">' +
    orderDetailRow('来源策略', flow.source_policy || detail.source_policy) +
    orderDetailRow('当前状态', middleOrderStatusLabel(flow.status || detail.status)) +
    orderDetailRow('导入时间', formatTime(flow.imported_at || detail.created_at)) +
    orderDetailRow('预审时间', formatTime(flow.validated_at)) +
    orderDetailRow('更新时间', formatTime(flow.updated_at || detail.updated_at)) +
    orderDetailRow('流程版本', flow.version || detail.version) +
    orderDetailRow('发货预览单', latestNotice.notice_no) +
    orderDetailRow('预览状态', deliveryNoticeStatusLabel(latestNotice.status)) +
    orderDetailRow('OMS单号', latestNotice.oms_order_no) +
    orderDetailRow('运单号', latestNotice.waybill_no || latestNotice.platform_fulfillment_synced_waybill_no) +
    orderDetailRow('确认人', latestNotice.confirmed_by) +
    orderDetailRow('推送时间', formatTime(latestNotice.pushed_at)) +
    (validationText && !failedRules.length ? orderDetailRow('预审摘要', validationText) : '') +
    (failedRules.length ? renderValidationSummaryTable(failedRules, skuNamesMap) : '') +
    '</div>' +
    '</section>';
}

function renderProductLogisticsCards(detail) {
  var items = detail.items || [];
  var receipt = detail.receipt || {};
  var notices = detail.delivery_notices || [];
  var latestNotice = notices[0] || {};
  var groups = ((latestNotice.split_preview || {}).groups || []);
  if (!items.length) {
    return '<section class="order-detail-card"><h3>产品-物流（邮寄信息）</h3><div class="empty-note">暂无产品明细</div></section>';
  }
  var cards = items.map(function(item, index) {
    var itemLogistics = item.logistics || {};
    var group = groups.find(function(g) {
      return (g.items || []).some(function(groupItem) {
        return groupItem.sku_code && item.sku_code && String(groupItem.sku_code) === String(item.sku_code);
      });
    }) || groups[0] || {};
    return '<div class="order-product-logistics-card">' +
      '<div class="order-product-title"><strong>' + orderDetailValue(item.product_name || item.sku_code || ('产品 ' + (index + 1))) + '</strong><small>' + orderDetailValue(item.sku_code) + '</small></div>' +
      '<div class="order-detail-grid">' +
      orderDetailRow('数量', item.quantity) +
      orderDetailRow('平台SKU', item.shop_sku_code) +
      orderDetailRow('仓库', firstPresent(itemLogistics.warehouse_code, group.warehouse_name, group.warehouse_code, latestNotice.warehouse_code)) +
      orderDetailRow('物流方式', firstPresent(itemLogistics.shipping_method, latestNotice.logistic_code, detail.fulfillment_type)) +
      orderDetailRow('收货人', firstPresent(itemLogistics.contact, receipt.contact)) +
      orderDetailRow('联系电话', firstPresent(itemLogistics.phone, receipt.phone)) +
      orderDetailRow('期望交期', firstPresent(itemLogistics.delivery_date, receipt.delivery_date)) +
      orderDetailRow('邮寄地址', firstPresent(itemLogistics.address, receipt.address)) +
      '</div>' +
      '</div>';
  }).join('');
  return '<section class="order-detail-card"><h3>产品-物流（邮寄信息）</h3><div class="order-product-logistics-list">' + cards + '</div></section>';
}

function renderOrderDetailDrawer(detail) {
  return '<div class="order-detail-header"><span>📋 ' + orderDetailValue(detail.order_no) + '</span><button class="button ghost" onclick="this.parentElement.parentElement.remove();this.closest(\'.drawer-overlay\')&&this.closest(\'.drawer-overlay\').remove()">✕</button></div>' +
    '<div class="order-detail-body">' +
    renderOrderBasicCard(detail) +
    renderOrderFlowCard(detail) +
    renderProductLogisticsCards(detail) +
    '</div>';
}

function loadOrders() {
  var q = (document.getElementById('order-search') || {}).value || '';
  var status = (document.getElementById('order-status-filter') || {}).value || '';
  var type = (document.getElementById('order-type-filter') || {}).value || '';
  api('/api/v2/order-dashboard').then(function(dash) {
    if (dash && dash.status_counts) {
      var metrics = document.getElementById('order-metrics');
      if (metrics) {
        var html = '';
        Object.keys(dash.status_counts).forEach(function(k) {
          html += '<div class="metrics-card"><div class="num">' + dash.status_counts[k] + '</div><div class="label">' + middleOrderStatusLabel(k) + '</div></div>';
        });
        metrics.innerHTML = html;
      }
    }
  }).catch(function(err) {
    notifyError(err, ["订单处理", "加载仪表盘失败"]);
  });
  var url = '/api/v2/orders?page=1&page_size=50&q=' + encodeURIComponent(q) + '&status=' + encodeURIComponent(status);
  api(url).then(function(data) {
    var orders = data.items || data.orders || [];
    var list = document.getElementById('order-list');
    if (!list) return;
    if (!orders.length) {
      list.innerHTML = '<div style="text-align:center;padding:48px;color:var(--muted);">暂无匹配订单</div>';
      return;
    }
    var html = '<table class="data-table"><thead><tr><th>单号</th><th>客户</th><th>类型</th><th>状态</th><th>主体</th><th>操作</th></tr></thead><tbody>';
    orders.forEach(function(o) {
      var sc = 'pending';
      var outOfScope = o.status === 'OUT_OF_SCOPE';
      if (o.status === 'ERP_SAVED' || o.status === 'DELIVERY_NOTICE_READY') sc = 'success';
      else if (o.status === 'ERP_FAILED' || o.status === 'VALIDATION_BLOCKED') sc = 'failed';
      else if (o.status === 'ERP_SAVING' || o.status === 'ERP_PENDING') sc = 'running';
      if (outOfScope) sc = 'muted';
      html += '<tr' + (outOfScope ? ' class="order-row-out-of-scope"' : '') + '><td>' + (o.order_no || '').slice(-12) + '</td><td>' + (o.customer_name || '').slice(0, 16) + '</td>' +
        '<td>' + (o.order_type === 'STOCK_REPLENISHMENT' ? '备货' : '销售') + '</td>' +
        '<td><span class="status-tag ' + sc + '">' + h(middleOrderStatusLabel(o.status)) + '</span></td>' +
        '<td>' + (o.entity_code || '') + '</td>' +
        '<td>' + (outOfScope ? '<span class="muted-action">—</span>' : 
          '<a href="#" class="order-view" data-id="' + (o.id || '') + '">详情</a> | ' +
          '<a href="#" class="order-kingdee-preview" data-id="' + (o.id || '') + '">金蝶预览</a>'
        ) + '</td></tr>';
    });
    html += '</tbody></table>';
    list.innerHTML = html;
    list.querySelectorAll('.order-view').forEach(function(a) {
      a.addEventListener('click', function(e) {
        e.preventDefault();
        var id = this.dataset.id;
        if (!id) return;
        api('/api/v2/orders/' + id).then(function(detail) {
          if (!detail) return;
          var d = document.createElement('div');
          d.className = 'order-detail-drawer';
          d.innerHTML = renderOrderDetailDrawer(detail);
          document.body.appendChild(d);
          var ov = document.createElement('div');
          ov.className = 'drawer-overlay';
          ov.addEventListener('click', function() { d.remove(); ov.remove(); });
          document.body.appendChild(ov);
        }).catch(function(err) { notifyError(err, ["订单处理", "加载订单详情失败"]); });
      });
    });
    list.querySelectorAll('.order-kingdee-preview').forEach(function(a) {
      a.addEventListener('click', function(e) {
        e.preventDefault();
        var id = this.dataset.id;
        if (!id) return;
        api('/api/v2/orders/' + id).then(function(detail) {
          if (!detail) return;
          openKingdeePreviewModal(detail);
        }).catch(function(err) { notifyError(err, ["订单处理", "加载金蝶预览失败"]); });
      });
    });
  }).catch(function(e) { notifyError(e, ["订单处理", "加载订单列表失败"]); });
}

function getEntityName(code) {
  var names = {
    'SZ': '深圳积木易搭科技技术有限公司',
    'SZ_WH': '深圳积木易搭武汉分公司',
    'WH': '武汉尺子科技有限公司',
    'SZ_3D': '深圳积木三维科技有限公司',
    'WH_RX': '武汉睿数信息技术有限公司',
    'SZ_3D_WH': '深圳积木三维武汉分公司',
    'HK': '积木易搭（香港）有限公司',
    'GZ': '广州积木易搭数字科技有限公司',
    'SZ_SZ': '深圳积木数智软件技术有限公司',
    'LU': '积木易搭（卢森堡）有限公司',
    'US': '积木易搭（美国）有限公司'
  };
  return names[code] || code;
}

function openKingdeePreviewModal(detail) {
  var validation = detail.flow?.validation_summary || detail.validation_summary || {};
  var failedRules = (validation.results || []).filter(function(r) { return r && r.passed === false; });
  var failedCodes = failedRules.map(function(r) { return r.rule_code; });

  var erpBillNo = detail.erp_bill_no;
  var isBillNoErr = !erpBillNo; 
  var billNoHtml = isBillNoErr 
    ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>' 
    : h(erpBillNo);

  var customerName = detail.customer_name;
  var isCustErr = failedCodes.indexOf("CUSTOMER_MAPPING") !== -1 || !customerName;
  var customerHtml = isCustErr
    ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>'
    : h(customerName);

  var orgId = detail.fulfillment_entity || detail.entity_code || '';
  var isOrgErr = failedCodes.indexOf("REQUIRED_HEAD_FIELDS") !== -1 && !orgId;
  var orgName = getEntityName(orgId);
  
  var orgHtml = isOrgErr || !orgName
    ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>'
    : h(orgName);

  var orderDate = detail.created_at ? formatTime(detail.created_at).split(' ')[0] : '';
  var dateHtml = orderDate ? h(orderDate) : h(new Date().toISOString().split('T')[0].replace(/-/g, '/'));

  var currency = detail.currency || '人民币';
  var currencyHtml = h(currency);

  var deptName = detail.dept_name || '国内营销中心教育事业部';
  var salesperson = detail.sales_user_name || '杜红刚';

  var orderNo = detail.order_no;
  var orderNoHtml = h(orderNo);

  var billStatus = (detail.status === 'ERP_SAVED' || detail.status === 'DELIVERY_NOTICE_READY') ? '已审核' : '暂存';

  var receipt = detail.receipt || {};
  var isReceiptErr = failedCodes.indexOf("PHASE1_COMPLETE_PRE_REVIEW_FIELDS") !== -1;
  var contactHtml = (isReceiptErr && !receipt.contact) 
    ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>' 
    : h(receipt.contact || '-');
  var phoneHtml = (isReceiptErr && !receipt.phone) 
    ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>' 
    : h(receipt.phone || '-');
  var addressHtml = (isReceiptErr && !receipt.address) 
    ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>' 
    : h(receipt.address || '-');

  var itemsHtml = '';
  var items = detail.items || [];
  items.forEach(function(item, idx) {
    var isSkuErr = failedCodes.indexOf("KNOWN_ACTIVE_SKU") !== -1 && (!item.sku_code || item.sku_code.length > 20);
    var itemRulesFailed = failedRules.filter(function(r) {
      return (r.rule_code === "KNOWN_ACTIVE_SKU" || r.rule_code === "LOCAL_INVENTORY_AVAILABLE") && 
             (r.reason || '').indexOf(item.sku_code) !== -1;
    });
    
    var isItemSkuErr = isSkuErr || itemRulesFailed.some(function(r) { return r.rule_code === "KNOWN_ACTIVE_SKU"; });

    var skuHtml = isItemSkuErr
      ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>'
      : h(item.sku_code);

    var nameHtml = item.official_product_name 
      ? h(item.official_product_name) 
      : '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>';
    var modelHtml = h(item.product_model || item.model_name || '-');
    var qty = Number(item.quantity || 0);
    var qtyHtml = h(qty);

    var price = Number(item.price || 0);
    var isPriceErr = failedCodes.indexOf("POSITIVE_ORDER_AMOUNT") !== -1 && price <= 0;
    var priceHtml = isPriceErr
      ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>'
      : (price ? '¥' + price.toFixed(2) : '-');

    var isGift = price <= 0;
    var taxRate = 13.00;
    var totalAmount = qty * price;
    var netAmount = totalAmount / 1.13;
    var taxAmount = totalAmount - netAmount;

    var totalHtml = isPriceErr
      ? '<span style="color:#ef4444;font-weight:bold;">未获取/未通过预审</span>'
      : '¥' + totalAmount.toFixed(2);

    var netHtml = isPriceErr
      ? '-'
      : '¥' + netAmount.toFixed(2);

    var taxAmountHtml = isPriceErr
      ? '-'
      : '¥' + taxAmount.toFixed(2);

    itemsHtml += '<tr>' +
      '<td>' + (idx + 1) + '</td>' +
      '<td>标准产品</td>' +
      '<td class="' + (isItemSkuErr ? 'error' : '') + '">' + skuHtml + '</td>' +
      '<td>' + nameHtml + '</td>' +
      '<td>' + modelHtml + '</td>' +
      '<td>' + qtyHtml + '</td>' +
      '<td>台</td>' +
      '<td class="' + (isPriceErr ? 'error' : '') + '">' + priceHtml + '</td>' +
      '<td class="' + (isPriceErr ? 'error' : '') + '">' + priceHtml + '</td>' +
      '<td style="text-align:center;"><input type="checkbox" class="kd-checkbox" disabled ' + (isGift ? 'checked' : '') + ' /></td>' +
      '<td>' + taxRate.toFixed(2) + '</td>' +
      '<td>' + taxAmountHtml + '</td>' +
      '<td>' + netHtml + '</td>' +
      '<td class="' + (isPriceErr ? 'error' : '') + '">' + totalHtml + '</td>' +
    '</tr>';
  });

  var body = document.getElementById('kingdee-preview-body');
  if (!body) return;

  function kdField(label, valueHtml, isRequired, isDropdown, isDate, extraStyle, isError) {
    var asterisk = isRequired ? '<span class="kd-field-required-marker">*</span>' : '';
    var icon = isDropdown ? '<span class="kd-field-icon">▼</span>' : (isDate ? '<span class="kd-field-icon">📅</span>' : '');
    var errorClass = isError ? ' error' : '';
    var notesClass = label === '备注' ? ' notes-field' : '';
    return '<div class="kd-field-row" style="' + (extraStyle || '') + '">' +
      '<span class="kd-field-label">' + h(label) + '</span>' +
      '<div class="kd-field-input-wrap">' +
        '<div class="kd-input-field' + errorClass + notesClass + '">' + valueHtml + '</div>' +
        icon +
        asterisk +
      '</div>' +
    '</div>';
  }

  body.innerHTML = 
    // Deep Blue Header Bar
    '<div class="kd-header-bar">' +
      '<div class="kd-logo-area">' +
        '<span class="kd-logo-icon">K</span>' +
        '<span class="kd-logo-text">金蝶云 星空</span>' +
        '<span class="kd-company-name">深圳积木易搭科技有限公司-2... | 100 深圳积木易搭科技有限公司</span>' +
      '</div>' +
      '<div class="kd-header-right">' +
        '<span class="kd-header-menu-item">帮助</span>' +
        '<span class="kd-header-menu-item">关于</span>' +
        '<span class="kd-header-menu-item">👤 刘伟燕</span>' +
        '<button type="button" id="kd-preview-close" style="font-size: 16px; font-weight: bold;">✕</button>' +
      '</div>' +
    '</div>' +
    // System Tabs
    '<div class="kd-sys-tabs">' +
      '<div class="kd-sys-tab">销售订单列表</div>' +
      '<div class="kd-sys-tab active">销售订单 - 修改 ✕</div>' +
    '</div>' +
    // Button Toolbar
    '<div class="kd-toolbar">' +
      '<div class="kd-toolbar-buttons">' +
        '<button class="kd-toolbar-btn primary">新增 ▼</button>' +
        '<button class="kd-toolbar-btn">保存</button>' +
        '<button class="kd-toolbar-btn">提交 ▼</button>' +
        '<button class="kd-toolbar-btn">审核 ▼</button>' +
        '<button class="kd-toolbar-btn">选单 ▼</button>' +
        '<button class="kd-toolbar-btn">下推 ▼</button>' +
        '<button class="kd-toolbar-btn">关联查询 ▼</button>' +
        '<button class="kd-toolbar-btn">业务操作 ▼</button>' +
        '<button class="kd-toolbar-btn">业务查询 ▼</button>' +
        '<button class="kd-toolbar-btn">报价评估</button>' +
        '<button class="kd-toolbar-btn">前一 ▼</button>' +
        '<button class="kd-toolbar-btn">后一 ▼</button>' +
        '<button class="kd-toolbar-btn">列表</button>' +
        '<button class="kd-toolbar-btn">选项 ▼</button>' +
        '<button class="kd-toolbar-btn" id="kd-preview-exit">退出</button>' +
      '</div>' +
      '<div class="kd-toolbar-right">' +
        '<span>☁️ 订单风险</span>' +
        '<span>⚙️ 设置</span>' +
      '</div>' +
    '</div>' +
    // Sub-Tabs Bar
    '<div class="kd-page-subtabs">' +
      '<div class="kd-subtab active">基本信息</div>' +
      '<div class="kd-subtab">客户信息</div>' +
      '<div class="kd-subtab">财务信息</div>' +
      '<div class="kd-subtab">明细信息</div>' +
      '<div class="kd-subtab">明细财务信息</div>' +
    '</div>' +
    // Scroll Container
    '<div class="kd-content-scroll">' +
      // Basic info Card (Accordion)
      '<div class="kd-section-accordion">' +
        '<div class="kd-accordion-head"><span class="kd-accordion-arrow">▼</span>基本信息</div>' +
        '<div class="kd-accordion-body">' +
          '<div class="kd-grid-form-5">' +
            // Row 1
            kdField('单据类型', '标准销售订单', true, true) +
            kdField('销售类型', '产品类销售', true, true) +
            kdField('销售部门', h(deptName), true, true) +
            kdField('销售项目', '', false, true) +
            kdField('收货国家', '', false, true) +

            // Row 2
            kdField('单据编号', billNoHtml, false, false, false, '', isBillNoErr) +
            kdField('销售组织', orgHtml, true, true, false, '', isOrgErr) +
            kdField('销售组', '', false, true) +
            kdField('有无风险', '', false, true) +
            kdField('收货国家简称', '', false, false) +

            // Row 3
            kdField('日期', dateHtml, true, false, true) +
            kdField('客户', customerHtml, true, true, false, '', isCustErr) +
            kdField('销售员', h(salesperson), true, true) +
            kdField('订单编号', orderNoHtml, false, false) +
            kdField('(美国) 州', '', false, true) +

            // Row 4
            kdField('业务类型', '普通销售', true, true) +
            kdField('结算币别', currencyHtml, true, true) +
            kdField('单据状态', h(billStatus), false, true) +
            kdField('网店订单号', '', false, false) +
            kdField('(美国) 州简称', '', false, false) +

            // Row 5
            kdField('交货方式', '', false, true) +
            kdField('价目表', '', false, true) +
            kdField('变更原因', '', false, true) +
            kdField('终端网店单号', '', false, false) +
            '<div class="kd-field-row"></div>' +

            // Row 6
            kdField('交货地点', '', false, true) +
            kdField('收款条件', '', false, true) +
            kdField('备注', '中台订单 ' + orderNoHtml + ' | ' + customerHtml, false, false) +
            kdField('销售渠道', '', false, true) +
            '<div class="kd-field-row"></div>' +
          '</div>' +
        '</div>' +
      '</div>' +
      // Other Collapsed Accordions
      '<div class="kd-section-accordion">' +
        '<div class="kd-accordion-head collapsed"><span class="kd-accordion-arrow">▶</span>客户信息</div>' +
      '</div>' +
      '<div class="kd-section-accordion">' +
        '<div class="kd-accordion-head collapsed"><span class="kd-accordion-arrow">▶</span>收款执行明细</div>' +
      '</div>' +
      '<div class="kd-section-accordion">' +
        '<div class="kd-accordion-head collapsed"><span class="kd-accordion-arrow">▶</span>财务信息</div>' +
      '</div>' +
      // Detail list Card
      '<div class="kd-section-accordion">' +
        '<div class="kd-accordion-head"><span class="kd-accordion-arrow">▼</span>明细信息</div>' +
        '<div class="kd-accordion-body">' +
          '<div class="kd-table-toolbar">' +
            '<button class="kd-table-btn">新增行</button>' +
            '<button class="kd-table-btn">删除行</button>' +
            '<button class="kd-table-btn">批量填充</button>' +
            '<button class="kd-table-btn">业务操作 ▼</button>' +
            '<button class="kd-table-btn">业务查询 ▼</button>' +
            '<button class="kd-table-btn">锁库 ▼</button>' +
            '<button class="kd-table-btn">套件展开 ▼</button>' +
            '<button class="kd-table-btn">本单累计数量取价</button>' +
            '<button class="kd-table-btn" style="color: #004ea2; font-weight: bold;">附件</button>' +
          '</div>' +
          '<div class="kd-table-wrap">' +
            '<table class="kd-table">' +
              '<thead>' +
                '<tr>' +
                  '<th>序号</th>' +
                  '<th>产品类型</th>' +
                  '<th>物料编码 <span style="color:#ef4444;">*</span></th>' +
                  '<th>物料名称</th>' +
                  '<th>规格型号</th>' +
                  '<th>父项产品</th>' +
                  '<th>销售数量 <span style="color:#ef4444;">*</span></th>' +
                  '<th>销售单位</th>' +
                  '<th>计价数量</th>' +
                  '<th>计价单位</th>' +
                  '<th>单价</th>' +
                  '<th>含税单价 <span style="color:#ef4444;">*</span></th>' +
                  '<th>是否赠品</th>' +
                  '<th>税率%</th>' +
                  '<th>税额</th>' +
                  '<th>金额</th>' +
                  '<th>价税合计</th>' +
                '</tr>' +
              '</thead>' +
              '<tbody>' +
                itemsHtml +
              '</tbody>' +
            '</table>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';

  var modal = document.getElementById('kingdee-preview-modal');
  if (modal) {
    modal.removeAttribute('hidden');
    modal.style.display = 'flex';
  }

  var closeBtn = document.getElementById('kd-preview-close');
  var exitBtn = document.getElementById('kd-preview-exit');
  
  function closeModal() {
    if (modal) {
      modal.setAttribute('hidden', 'true');
      modal.style.display = 'none';
    }
  }

  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (exitBtn) exitBtn.addEventListener('click', closeModal);
}

// —— 主数据页面 ——
var _mdInited = false;
function initMasterDataPage() {
  if (_mdInited) return;
  _mdInited = true;
  initTabs(document.querySelector('[data-page="master-data"]'));
  loadPP(); loadEW(); loadMR(); loadME(); loadCBT();
  var ppR = document.getElementById('pp-refresh');
  if (ppR) ppR.addEventListener('click', loadPP);
  var ppA = document.getElementById('pp-add-btn');
  if (ppA) ppA.addEventListener('click', function() { showModal('price'); });
  var ewA = document.getElementById('ew-add-btn');
  if (ewA) ewA.addEventListener('click', function() { showModal('entitywh'); });
  var mrA = document.getElementById('mr-add-btn');
  if (mrA) mrA.addEventListener('click', function() { showModal('receiver'); });
  var meA = document.getElementById('me-add-btn');
  if (meA) meA.addEventListener('click', function() { showModal('materialEntity'); });

  var cbtR = document.getElementById('cbt-refresh');
  if (cbtR) cbtR.addEventListener('click', loadCBT);
  var cbtA = document.getElementById('cbt-add-btn');
  if (cbtA) cbtA.addEventListener('click', function() { showModal('crmBusinessType'); });
  var cbtSearch = document.getElementById('cbt-search');
  if (cbtSearch) cbtSearch.addEventListener('input', loadCBT);

  var mdTabs = document.querySelector('[data-page="master-data"]');
  if (mdTabs) {
    mdTabs.querySelectorAll('.tab').forEach(function(tab) {
      tab.addEventListener('click', function() {
        var tabName = this.dataset.tab;
        if (tabName === 'crm-business-type') loadCBT();
        if (tabName === 'review-aliases') loadAliasesList();
      });
    });
  }

  // 预审别名导入
  var raPreview = document.getElementById('ra-preview-btn');
  if (raPreview) raPreview.addEventListener('click', loadAliasesPreview);
  var raConfirm = document.getElementById('ra-confirm-btn');
  if (raConfirm) raConfirm.addEventListener('click', confirmAliasesImport);
  // 预审别名列表
  var raSearchBtn = document.getElementById('ra-search-btn');
  if (raSearchBtn) raSearchBtn.addEventListener('click', function() { _aliasesListState.page = 1; loadAliasesList(); });
  var raSearch = document.getElementById('ra-search');
  if (raSearch) raSearch.addEventListener('keydown', function(e) { if (e.key === 'Enter') { _aliasesListState.page = 1; loadAliasesList(); } });
  var raPrev = document.getElementById('ra-prev-btn');
  if (raPrev) raPrev.addEventListener('click', function() { if (_aliasesListState.page > 1) { _aliasesListState.page--; loadAliasesList(); } });
  var raNext = document.getElementById('ra-next-btn');
  if (raNext) raNext.addEventListener('click', function() { if (_aliasesListState.page < _aliasesListState.totalPages) { _aliasesListState.page++; loadAliasesList(); } });
  loadAliasesList();
}

var _aliasesPreviewData = null;

function loadAliasesPreview() {
  var fileInput = document.getElementById('ra-upload');
  var status = document.getElementById('ra-status');
  var summary = document.getElementById('ra-summary');
  var tableWrap = document.getElementById('ra-table-wrap');
  var confirmBtn = document.getElementById('ra-confirm-btn');
  if (!fileInput || !fileInput.files.length) { toast('请先选择 Excel 文件'); return; }
  var file = fileInput.files[0];
  if (!file.name.endsWith('.xlsx')) { toast('仅支持 .xlsx 格式'); return; }

  status.textContent = '正在解析文件...';
  var formData = new FormData();
  formData.append('file', file);
  fetch('/api/review-aliases/import/preview', { method: 'POST', body: formData, credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { status.textContent = '解析失败: ' + data.error; return; }
      _aliasesPreviewData = data;
      status.textContent = '共 ' + data.matched + ' 行匹配，' + (data.skipped_no_spu || 0) + ' 行未找到 SPU';
      if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.style.opacity = '1'; }

      // Summary
      if (summary) {
        summary.style.display = 'block';
        var s = data.summary || {};
        summary.innerHTML = '<strong>预览结果：</strong>'
          + s.spus_with_new + ' 个 SPU 将新增 ' + s.total_new_aliases + ' 个别名'
          + (data.skipped_no_spu ? '，' + data.skipped_no_spu + ' 个产品编码未匹配到 SPU（将跳过）' : '')
          + (data.skipped_no_alias ? '，' + data.skipped_no_alias + ' 行无别名数据（将跳过）' : '');
      }

      // Table
      if (tableWrap) {
        var items = data.items || [];
        if (!items.length) { tableWrap.innerHTML = '<div class="empty-note">无匹配数据</div>'; return; }
        var html = '<table class="data-table"><thead><tr><th>产品编码</th><th>SPU 名称</th><th>现有别名</th><th>新增别名</th><th>合并后总数</th></tr></thead><tbody>';
        var shown = 0;
        items.forEach(function(item) {
          if (shown >= 200) return;
          shown++;
          var existingHtml = (item.existing_aliases || []).slice(0, 3).join(', ') + ((item.existing_aliases || []).length > 3 ? '...' : '') || '-';
          var newHtml = (item.new_aliases || []).join(', ') || '-';
          html += '<tr><td>' + h(item.spu_id) + '</td><td>' + h(item.product_name) + '</td><td><small>' + h(existingHtml) + '</small></td><td>' + h(newHtml) + '</td><td>' + (item.merged_aliases || []).length + '</td></tr>';
        });
        if (items.length > 200) html += '<tr><td colspan="5" style="text-align:center;color:var(--muted);">仅显示前 200 条，共 ' + items.length + ' 条</td></tr>';
        html += '</tbody></table>';
        tableWrap.innerHTML = html;
      }
    })
    .catch(function(err) { notifyError(err, ['主数据', '别名预览失败']); status.textContent = '解析失败'; });
}

function confirmAliasesImport() {
  if (!_aliasesPreviewData) { toast('请先预览'); return; }
  var confirmBtn = document.getElementById('ra-confirm-btn');
  var statusEl = document.getElementById('ra-status');
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.style.opacity = '0.5'; }
  statusEl.textContent = '正在导入...';
  api('/api/review-aliases/import/confirm', {
    method: 'POST',
    body: JSON.stringify({ items: _aliasesPreviewData.items }),
  })
  .then(function(result) {
    toast(result.message || '别名导入成功');
    statusEl.textContent = result.message || '导入完成';
    _aliasesPreviewData = null;
  })
  .catch(function(err) {
    notifyError(err, ['主数据', '别名确认导入失败']);
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.style.opacity = '1'; }
  });
}

var _aliasesListState = { page: 1, pageSize: 20, q: '', totalPages: 1, total: 0 };

function loadAliasesList() {
  var wrap = document.getElementById('ra-list-wrap');
  var info = document.getElementById('ra-list-info');
  var pageInfo = document.getElementById('ra-page-info');
  var prevBtn = document.getElementById('ra-prev-btn');
  var nextBtn = document.getElementById('ra-next-btn');
  var searchInput = document.getElementById('ra-search');
  if (!wrap) return;

  _aliasesListState.q = (searchInput ? searchInput.value : '').trim();

  wrap.innerHTML = '<div style="text-align:center;padding:24px;color:var(--muted);">加载中...</div>';

  var url = '/api/review-aliases/list?page=' + _aliasesListState.page + '&page_size=' + _aliasesListState.pageSize;
  if (_aliasesListState.q) url += '&q=' + encodeURIComponent(_aliasesListState.q);

  fetch(url, { credentials: 'same-origin' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _aliasesListState.totalPages = data.total_pages || 1;
      _aliasesListState.total = data.total || 0;

      if (info) info.textContent = '共 ' + _aliasesListState.total + ' 个物料';
      if (pageInfo) pageInfo.textContent = '第 ' + data.page + '/' + data.total_pages + ' 页';

      if (prevBtn) { prevBtn.disabled = _aliasesListState.page <= 1; prevBtn.style.opacity = prevBtn.disabled ? '0.4' : '1'; }
      if (nextBtn) { nextBtn.disabled = _aliasesListState.page >= _aliasesListState.totalPages; nextBtn.style.opacity = nextBtn.disabled ? '0.4' : '1'; }

      var items = data.items || [];
      if (!items.length) {
        wrap.innerHTML = '<div class="empty-note">暂无预审别名数据</div>';
        return;
      }

      var html = '<table class="data-table"><thead><tr><th style="width:140px;">产品编码</th><th>SPU 名称</th><th>预审别名</th><th style="width:80px;">数量</th></tr></thead><tbody>';
      items.forEach(function(item) {
        var aliasText = (item.aliases || []).join('、') || '-';
        html += '<tr><td>' + h(item.spu_id) + '</td><td>' + h(item.name) + '</td><td style="max-width:400px;word-break:break-all;"><small>' + h(aliasText) + '</small></td><td style="text-align:center;">' + (item.alias_count || 0) + '</td></tr>';
      });
      html += '</tbody></table>';
      wrap.innerHTML = html;
    })
    .catch(function(err) {
      notifyError(err, ['主数据', '加载别名列表失败']);
      wrap.innerHTML = '<div class="empty-note">加载失败</div>';
    });
}

function loadPP() {
  api('/api/config/product-prices?page_size=200').then(function(data) {
    var items = data.items || [];
    var html = '<table class="data-table"><thead><tr><th>物料编码</th><th>主体</th><th>内部价格</th><th>币种</th><th>操作</th></tr></thead><tbody>';
    items.forEach(function(p) {
      html += '<tr><td>' + p.sku_id + '</td><td>' + p.entity_code + '</td><td>¥' + (p.unit_price / 100).toFixed(2) + '</td><td>' + p.currency + '</td>' +
        '<td><a href="#" class="pp-edit" data-sku="' + p.sku_id + '" data-ent="' + p.entity_code + '" data-pr="' + p.unit_price + '">编辑</a></td></tr>';
    });
    html += '</tbody></table>';
    var w = document.getElementById('pp-table-wrap');
    if (w) w.innerHTML = html;
    document.querySelectorAll('.pp-edit').forEach(function(a) {
      a.addEventListener('click', function(e) { e.preventDefault(); showModal('price', {sku:this.dataset.sku, ent:this.dataset.ent, pr:this.dataset.pr}); });
    });
  }).catch(function() {});
}

function loadEW() {
  api('/api/config/entity-mappings').then(function(data) {
    var items = data.items || [];
    var filter = document.getElementById('ew-entity-filter');
    if (filter) {
      var fhtml = '<option value="">全部主体</option>';
      items.forEach(function(m) { fhtml += '<option value="' + m.entity_code + '">' + m.entity_code + ' - ' + m.entity_name + '</option>'; });
      filter.innerHTML = fhtml;
      filter.onchange = function() { loadEWFilter(); };
    }
    renderEWTable(items);
  }).catch(function(e) { notifyError(e, ['主数据', '加载主体-仓库映射失败']); });
}

function loadEWFilter() {
  var entity = (document.getElementById('ew-entity-filter') || {}).value || '';
  api('/api/config/entity-mappings').then(function(data) {
    var items = data.items || [];
    if (entity) items = items.filter(function(m) { return m.entity_code === entity; });
    renderEWTable(items);
  }).catch(function(e) { notifyError(e, ['主数据', '加载主体-仓库映射失败']); });
}

function renderEWTable(items) {
  var html = '<table class="data-table"><thead><tr><th>主体编码</th><th>主体名称</th><th>ERP 组织ID</th><th>关联仓库</th><th>状态</th><th>操作</th></tr></thead><tbody>';
  items.forEach(function(m) {
    var whs = (m.warehouses || []).map(function(w) { return w.warehouse_name || w.warehouse_code || w; }).join('、') || '-';
    var statusHtml = m.is_active !== false ? '<span class="status-tag success">启用</span>' : '<span class="status-tag muted">禁用</span>';
    html += '<tr><td>' + h(m.entity_code) + '</td><td>' + h(m.entity_name) + '</td><td>' + h(m.erp_org_id || '-') + '</td>' +
      '<td><small>' + h(whs) + '</small></td><td>' + statusHtml + '</td>' +
      '<td><a href="#" class="ew-edit" data-code="' + m.entity_code + '">编辑</a> <a href="#" class="ew-del" data-code="' + h(m.entity_code) + '" style="color:#dc2626;">删除</a></td></tr>';
  });
  if (!items.length) html += '<tr><td colspan="6" style="text-align:center;color:var(--muted);">暂无主体-仓库映射，请先新增</td></tr>';
  html += '</tbody></table>';
  var w = document.getElementById('ew-table-wrap');
  if (w) w.innerHTML = html;
  document.querySelectorAll('.ew-edit').forEach(function(a) {
    a.addEventListener('click', function(e) { e.preventDefault(); showModal('entitywh', {entity_code: this.dataset.code}); });
  });
  document.querySelectorAll('.ew-del').forEach(function(a) {
    a.addEventListener('click', function(e) {
      e.preventDefault();
      var code = this.dataset.code;
      if (!confirm('确认删除主体 ' + code + ' 的映射？')) return;
      api('/api/config/entity-mappings/' + encodeURIComponent(code), { method: 'DELETE' })
        .then(function() { toast('已删除 ' + code); loadEW(); })
        .catch(function(e) { notifyError(e, ['主数据', '删除主体映射失败']); });
    });
  });
}

function loadCBT() {
  api('/api/config/crm-business-type-mappings').then(function(data) {
    var items = data.items || [];
    renderCBTTable(items);
  }).catch(function(e) {
    notifyError(e, ["主数据", "加载 CRM 业务类型映射失败"]);
  });
}

function renderCBTTable(items) {
  var html = '<table class="data-table"><thead><tr>' +
    '<th>业务类型编码 (record_type)</th>' +
    '<th>业务类型名称</th>' +
    '<th>销售主体编码</th>' +
    '<th>销售主体名称</th>' +
    '<th>状态</th>' +
    '<th>操作</th>' +
    '</tr></thead><tbody>';

  var searchVal = (document.getElementById('cbt-search') || {}).value || '';
  if (searchVal) {
    searchVal = searchVal.toLowerCase();
    items = items.filter(function(m) {
      return (m.business_type_code || '').toLowerCase().indexOf(searchVal) !== -1 ||
             (m.business_type_name || '').toLowerCase().indexOf(searchVal) !== -1;
    });
  }

  if (items.length === 0) {
    html += '<tr><td colspan="6" style="text-align:center; color:var(--muted);">暂无匹配的数据</td></tr>';
  } else {
    items.forEach(function(m) {
      var statusHtml = m.is_active 
        ? '<span class="status-tag success">启用</span>' 
        : '<span class="status-tag danger">禁用</span>';
      
      html += '<tr>' +
        '<td><code>' + h(m.business_type_code) + '</code></td>' +
        '<td>' + h(m.business_type_name) + '</td>' +
        '<td><code>' + h(m.entity_code) + '</code></td>' +
        '<td>' + h(getEntityName(m.entity_code)) + '</td>' +
        '<td>' + statusHtml + '</td>' +
        '<td>' +
          '<a href="#" class="cbt-edit" data-code="' + h(m.business_type_code) + '" data-name="' + h(m.business_type_name) + '" data-ent="' + m.entity_code + '" data-act="' + m.is_active + '" style="margin-right: 8px;">编辑</a>' +
          '<a href="#" class="cbt-delete" data-code="' + h(m.business_type_code) + '" style="color:var(--danger);">删除</a>' +
        '</td>' +
        '</tr>';
    });
  }
  html += '</tbody></table>';

  var wrap = document.getElementById('cbt-table-wrap');
  if (wrap) wrap.innerHTML = html;

  // Bind actions
  document.querySelectorAll('.cbt-edit').forEach(function(el) {
    el.addEventListener('click', function(e) {
      e.preventDefault();
      var data = {
        business_type_code: this.dataset.code,
        business_type_name: this.dataset.name,
        entity_code: this.dataset.ent,
        is_active: this.dataset.act === 'true'
      };
      showModal('crmBusinessType', data);
    });
  });

  document.querySelectorAll('.cbt-delete').forEach(function(el) {
    el.addEventListener('click', function(e) {
      e.preventDefault();
      var code = this.dataset.code;
      if (confirm('确认删除业务类型 ' + code + ' 的映射关系吗？')) {
        apiDelete('/api/config/crm-business-type-mappings/' + encodeURIComponent(code)).then(function() {
          toast('删除成功');
          loadCBT();
        }).catch(function(err) {
          toast('删除失败: ' + (err.message || ''), true);
        });
      }
    });
  });
}

function loadMR() {
  api('/api/config/mail-receivers').then(function(data) {
    var items = data.items || [];
    var html = '<table class="data-table"><thead><tr><th>场景</th><th>收件人</th><th>操作</th></tr></thead><tbody>';
    items.forEach(function(r) {
      var toArr = r.to || [];
      try { if (typeof toArr === 'string') toArr = JSON.parse(toArr); } catch(e) {}
      html += '<tr><td>' + r.scene + '</td><td>' + (Array.isArray(toArr) ? toArr.join(', ') : toArr) + '</td>' +
        '<td><a href="#" class="mr-edit" data-scene="' + r.scene + '" data-to="' + (Array.isArray(toArr) ? toArr.join(',') : '') + '">编辑</a></td></tr>';
    });
    html += '</tbody></table>';
    var w = document.getElementById('mr-table-wrap');
    if (w) w.innerHTML = html;
    document.querySelectorAll('.mr-edit').forEach(function(a) {
      a.addEventListener('click', function(e) { e.preventDefault(); showModal('receiver', {scene:this.dataset.scene, to:this.dataset.to}); });
    });
  }).catch(function() {});
}

function loadME() {
  api('/api/config/material-entity').then(function(data) {
    var items = data.items || [];
    var html = '<table class="data-table"><thead><tr><th>物料编码</th><th>物料名称</th><th>出货主体</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    items.forEach(function(m) {
      html += '<tr><td>' + m.material_code + '</td><td>' + (m.material_name || '').slice(0, 30) + '</td><td>' + m.entity_code + '</td><td>' + (m.is_active ? '启用' : '停用') + '</td>' +
        '<td><a href="#" class="me-edit" data-code="' + m.material_code + '" data-ent="' + m.entity_code + '">编辑</a></td></tr>';
    });
    html += '</tbody></table>';
    var w = document.getElementById('me-table-wrap');
    if (w) w.innerHTML = html;
    document.querySelectorAll('.me-edit').forEach(function(a) {
      a.addEventListener('click', function(e) { e.preventDefault(); showModal('materialEntity', {code:this.dataset.code, ent:this.dataset.ent}); });
    });
  }).catch(function() {});
}

// —— Modal 弹窗 ——
function showModal(type, data) {
  data = data || {};
  var html = '', id = 'modal-' + Date.now();
  if (type === 'price') {
    html = '<div class="modal-overlay" id="' + id + '"><div class="modal-box"><h3>' + (data.sku ? '编辑价格' : '新增价格') + '</h3>' +
      '<label><span>物料编码</span><input id="m-sku" value="' + (data.sku || '') + '" /></label>' +
      '<label><span>主体</span><select id="m-ent"><option value="SZ">深圳</option><option value="HK">香港</option><option value="LU">卢森堡</option></select></label>' +
      '<label><span>价格（分）</span><input id="m-pr" type="number" value="' + (data.pr || '') + '" /></label>' +
      '<div class="modal-actions"><button class="button ghost" onclick="this.closest(\'.modal-overlay\').remove()">取消</button><button class="button" data-act="save-price">保存</button></div></div></div>';
  } else if (type === 'entitywh') {
    var isEdit = Boolean(data.entity_code);
    var whRows = (data.warehouses || []).map(function(w, i) {
      var whCode = w.warehouse_code || w.code || w;
      var whName = w.warehouse_name || w.name || '';
      return '<div class="ew-wh-row" style="display:flex;gap:6px;margin-bottom:4px;">' +
        '<input class="ew-wh-code" list="wh-suggestions" placeholder="仓库编码（可选）" value="' + h(whCode) + '" style="flex:1;min-width:0;" />' +
        '<input class="ew-wh-name" placeholder="仓库名称" value="' + h(whName) + '" style="flex:2;min-width:0;" />' +
        '<button type="button" class="icon-button ew-wh-remove" style="color:#dc2626;">×</button></div>';
    }).join('');
    html = '<div class="modal-overlay" id="' + id + '"><div class="modal-box" style="min-width:520px;"><h3>' + (isEdit ? '编辑主体' : '新增主体') + '</h3>' +
      '<datalist id="wh-suggestions"></datalist>' +
      '<label style="display:grid;grid-template-columns:100px 1fr;gap:6px;align-items:center;margin-bottom:8px;">' +
        '<span>主体编码</span><input id="mew-code" value="' + h(data.entity_code || '') + '" ' + (isEdit ? 'readonly style="background:#f3f4f6;"' : '') + ' placeholder="如 SZ / HK / LU" /></label>' +
      '<label style="display:grid;grid-template-columns:100px 1fr;gap:6px;align-items:center;margin-bottom:8px;">' +
        '<span>主体名称</span><input id="mew-name" value="' + h(data.entity_name || '') + '" placeholder="如 深圳积木易搭科技技术有限公司" /></label>' +
      '<label style="display:grid;grid-template-columns:100px 1fr;gap:6px;align-items:center;margin-bottom:8px;">' +
        '<span>ERP 组织ID</span><input id="mew-org" value="' + h(data.erp_org_id || '') + '" placeholder="如 100" /></label>' +
      '<div style="margin-bottom:8px;"><div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">' +
        '<strong style="font-size:13px;">关联仓库</strong><button type="button" class="button ghost mini" id="mew-wh-add">+ 添加</button></div>' +
        '<div id="mew-wh-list">' + (whRows || '<div style="color:var(--muted);font-size:12px;">暂无仓库，点击"添加"新增，输入时可从已有仓库下拉选择</div>') + '</div></div>' +
      '<label style="display:grid;grid-template-columns:100px 1fr;gap:6px;align-items:center;margin-bottom:8px;">' +
        '<span>状态</span><select id="mew-active"><option value="true"' + (data.is_active !== false ? ' selected' : '') + '>启用</option><option value="false"' + (data.is_active === false ? ' selected' : '') + '>禁用</option></select></label>' +
      '<div class="modal-actions"><button class="button ghost" onclick="this.closest(\'.modal-overlay\').remove()">取消</button><button class="button" data-act="save-entitywh">保存</button></div></div></div>';
    } else if (type === 'materialEntity') {
    var entityOpts = ['SZ','HK','LU','US','WH','GZ'].map(function(e) { return '<option value="' + e + '"' + (e === (data.ent||'') ? ' selected' : '') + '>' + e + '</option>'; }).join('');
    html = '<div class="modal-overlay" id="' + id + '"><div class="modal-box"><h3>' + (data.code ? '编辑物料例外' : '新增物料例外') + '</h3>' +
      '<label><span>物料编码</span><input id="mme-code" value="' + (data.code || '') + '" placeholder="如 1300100118" /></label>' +
      '<label><span>出货主体</span><select id="mme-ent">' + entityOpts + '</select></label>' +
      '<div class="modal-actions"><button class="button ghost" onclick="this.closest(\'.modal-overlay\').remove()">取消</button><button class="button" data-act="save-materialEntity">保存</button></div></div></div>';
  } else if (type === 'receiver') {
    html = '<div class="modal-overlay" id="' + id + '"><div class="modal-box"><h3>编辑收件人</h3>' +
      '<label><span>场景</span><select id="m-scene"><option value="domestic_delivery">国内仓发货</option><option value="overseas_delivery">海外仓发货</option><option value="replenishment_domestic">备货武汉</option><option value="replenishment_overseas">备货海外</option></select></label>' +
      '<label><span>收件人（逗号分隔）</span><input id="m-to" value="' + ((data.to || '').replace(/,/g, ', ')) + '" /></label>' +
      '<div class="modal-actions"><button class="button ghost" onclick="this.closest(\'.modal-overlay\').remove()">取消</button><button class="button" data-act="save-receiver">保存</button></div></div></div>';
  } else if (type === 'crmBusinessType') {
    var entityOptions = [
      {code: 'SZ', name: '深圳积木易搭科技技术有限公司 (SZ)'},
      {code: 'SZ_WH', name: '深圳积木易搭武汉分公司 (SZ_WH)'},
      {code: 'WH', name: '武汉尺子科技有限公司 (WH)'},
      {code: 'SZ_3D', name: '深圳积木三维科技有限公司 (SZ_3D)'},
      {code: 'WH_RX', name: '武汉睿数信息技术有限公司 (WH_RX)'},
      {code: 'SZ_3D_WH', name: '深圳积木三维武汉分公司 (SZ_3D_WH)'},
      {code: 'HK', name: '积木易搭（香港）有限公司 (HK)'},
      {code: 'GZ', name: '广州积木易搭数字科技有限公司 (GZ)'},
      {code: 'SZ_SZ', name: '深圳积木数智软件技术有限公司 (SZ_SZ)'},
      {code: 'LU', name: '积木易搭（卢森堡）有限公司 (LU)'},
      {code: 'US', name: '积木易搭（美国）有限公司 (US)'}
    ];
    var entityHtml = entityOptions.map(function(e) {
      return '<option value="' + e.code + '"' + (e.code === (data.entity_code || '') ? ' selected' : '') + '>' + e.name + '</option>';
    }).join('');
    html = '<div class="modal-overlay" id="' + id + '"><div class="modal-box"><h3>' + (data.business_type_code ? '编辑映射' : '新增映射') + '</h3>' +
      '<label><span>业务类型编码 (record_type)</span><input id="mcbt-code" value="' + (data.business_type_code || '') + '" ' + (data.business_type_code ? 'disabled' : '') + ' placeholder="例如: record_hnH91__c" /></label>' +
      '<label><span>业务类型名称</span><input id="mcbt-name" value="' + (data.business_type_name || '') + '" placeholder="例如: 深圳积木易搭订单" /></label>' +
      '<label><span>主体公司</span><select id="mcbt-ent">' + entityHtml + '</select></label>' +
      '<label><span>是否启用</span><select id="mcbt-act"><option value="true" ' + (data.is_active !== false ? 'selected' : '') + '>启用</option><option value="false" ' + (data.is_active === false ? 'selected' : '') + '>禁用</option></select></label>' +
      '<div class="modal-actions"><button class="button ghost" onclick="this.closest(\'.modal-overlay\').remove()">取消</button><button class="button" data-act="save-crmBusinessType">保存</button></div></div></div>';
  }
  if (!html) return;
  document.body.insertAdjacentHTML('beforeend', html);
  var modalEl = document.getElementById(id);

  // 主体-仓库动态行添加
  if (type === 'entitywh') {
    // 加载已有仓库列表供自动填充
    var _warehouseMap = {};
    api('/api/products/inventory/warehouses?limit=100').then(function(whData) {
      var items = whData.items || [];
      var dl = modalEl.querySelector('#wh-suggestions');
      if (dl) {
        items.forEach(function(w) {
          var opt = document.createElement('option');
          opt.value = w.warehouse_code;
          opt.label = w.warehouse_code + ' - ' + (w.warehouse_name || '');
          dl.appendChild(opt);
        });
      }
      items.forEach(function(w) { _warehouseMap[w.warehouse_code] = w.warehouse_name || w.warehouse_code; });
    }).catch(function() {});
    // 仓库编码输入时自动填充名称
    modalEl.addEventListener('input', function(e) {
      var codeInput = e.target.closest('.ew-wh-code');
      if (!codeInput || !codeInput.value) return;
      var row = codeInput.closest('.ew-wh-row');
      if (!row) return;
      var nameInput = row.querySelector('.ew-wh-name');
      if (!nameInput || nameInput.dataset.autofilled) return;
      var matched = _warehouseMap[codeInput.value];
      if (matched) { nameInput.value = matched; nameInput.dataset.autofilled = '1'; }
    });
    modalEl.querySelector('#mew-wh-add')?.addEventListener('click', function() {
      var list = modalEl.querySelector('#mew-wh-list');
      var row = document.createElement('div');
      row.className = 'ew-wh-row';
      row.style.cssText = 'display:flex;gap:6px;margin-bottom:4px;';
      row.innerHTML = '<input class="ew-wh-code" list="wh-suggestions" placeholder="仓库编码（可选）" style="flex:1;min-width:0;" />' +
        '<input class="ew-wh-name" placeholder="仓库名称" style="flex:2;min-width:0;" />' +
        '<button type="button" class="icon-button" style="color:#dc2626;">×</button>';
      row.querySelector('.icon-button').addEventListener('click', function() { row.remove(); });
      list.querySelector('div:first-child')?.remove(); // remove placeholder
      list.appendChild(row);
    });
    modalEl.querySelectorAll('.ew-wh-remove').forEach(function(btn) {
      btn.addEventListener('click', function() { btn.closest('.ew-wh-row').remove(); });
    });
  }

  document.getElementById(id).querySelector('[data-act]').addEventListener('click', function() {
    var modal = this.closest('.modal-overlay');
    var act = this.dataset.act;
    if (act === 'save-price') {
      var sku = document.getElementById('m-sku').value.trim();
      var ent = document.getElementById('m-ent').value;
      var pr = parseInt(document.getElementById('m-pr').value) || 0;
      if (!sku) { toast('物料编码必填', true); return; }
      apiPost('/api/config/product-prices', {sku_id:sku, entity_code:ent, unit_price:pr}).then(function() { modal.remove(); toast('保存成功'); loadPP(); }).catch(function(e) { toast('失败: ' + (e.message||''), true); });
    } else if (act === 'save-entitywh') {
      var ecode = document.getElementById('mew-code').value.trim();
      var ename = document.getElementById('mew-name').value.trim();
      var eorg = document.getElementById('mew-org').value.trim();
      var eactive = document.getElementById('mew-active').value === 'true';
      if (!ecode) { toast('主体编码必填', true); return; }
      if (!ename) { toast('主体名称必填', true); return; }
      var warehouses = [];
      document.querySelectorAll('#' + modal.id + ' .ew-wh-row').forEach(function(row) {
        var wc = (row.querySelector('.ew-wh-code') || {}).value || '';
        var wn = (row.querySelector('.ew-wh-name') || {}).value || '';
        if (wc) warehouses.push({warehouse_code: wc, warehouse_name: wn || wc});
      });
      apiPost('/api/config/entity-mappings', {
        entity_code: ecode, entity_name: ename, erp_org_id: eorg,
        warehouses: warehouses, is_active: eactive
      }).then(function() { modal.remove(); toast('保存成功'); loadEW(); }).catch(function(e) { toast('失败: ' + (e.message||''), true); });
    } else if (act === 'save-materialEntity') {
      var mc = document.getElementById('mme-code').value.trim();
      var ent = document.getElementById('mme-ent').value;
      if (!mc) { toast('物料编码必填', true); return; }
      apiPost('/api/config/material-entity', {material_code: mc, entity_code: ent}).then(function() { modal.remove(); toast('保存成功'); loadME(); }).catch(function(e) { toast('失败: ' + (e.message||''), true); });
    } else if (act === 'save-receiver') {
      var p = {scene: document.getElementById('m-scene').value, to: document.getElementById('m-to').value.split(/[,，\s]+/).filter(Boolean)};
      if (!p.scene) { toast('场景必填', true); return; }
      apiPost('/api/config/mail-receivers', p).then(function() { modal.remove(); toast('保存成功'); loadMR(); }).catch(function(e) { toast('失败: ' + (e.message||''), true); });
    } else if (act === 'save-crmBusinessType') {
      var code = document.getElementById('mcbt-code').value.trim();
      var name = document.getElementById('mcbt-name').value.trim();
      var ent = document.getElementById('mcbt-ent').value;
      var active = document.getElementById('mcbt-act').value === 'true';
      if (!code) { toast('业务类型编码必填', true); return; }
      if (!name) { toast('业务类型名称必填', true); return; }
      apiPost('/api/config/crm-business-type-mappings', {
        business_type_code: code,
        business_type_name: name,
        entity_code: ent,
        is_active: active
      }).then(function() {
        modal.remove();
        toast('保存成功');
        loadCBT();
      }).catch(function(e) {
        toast('失败: ' + (e.message || ''), true);
      });
    }
  });
}

// —— 库存管理 ——
var _invInited = false;
var _invPage = 1;
function initInventoryPage() {
  if (_invInited) return;
  _invInited = true;
  initTabs(document.querySelector('[data-page="inventory"]'));
  loadInvWarehouses();
  loadInv();
  var btn = document.getElementById('inv-refresh');
  if (btn) btn.addEventListener('click', function() { _invPage = 1; loadInv(); });
  var search = document.getElementById('inv-search');
  if (search) search.addEventListener('keydown', function(e) { if (e.key === 'Enter') { _invPage = 1; loadInv(); } });
  var wh = document.getElementById('inv-wh-filter');
  if (wh) wh.addEventListener('change', function() { _invPage = 1; loadInv(); });
  var outOfStock = document.getElementById('inv-out-of-stock');
  if (outOfStock) outOfStock.addEventListener('change', function() { _invPage = 1; loadInv(); });
  var drop = document.getElementById('inv-drop-zone');
  if (drop) drop.addEventListener('click', function() { var fi = document.getElementById('inv-file-input'); if (fi) fi.click(); });
  var fi = document.getElementById('inv-file-input');
  if (fi) fi.addEventListener('change', function(e) {
    var f = e.target.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function(ev) {
      try {
        var formData = new FormData();
        formData.append('file', f);
        fetch('/api/inventory/import', { method: 'POST', body: formData })
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.ok) {
              toast('导入成功: ' + (data.total_rows || 0) + ' 行, 仓库: ' + (data.warehouses || []).join(', '));
              loadInv();
            } else {
              toast('导入失败: ' + (data.error || '未知错误'), true);
            }
          })
          .catch(function(ex) { toast('上传失败: ' + ex.message, true); });
      } catch(ex) { toast('读取失败: ' + ex.message, true); }
    };
    reader.readAsArrayBuffer(f);
  });
}

function loadInvWarehouses() {
  api('/api/inventory/warehouses').then(function(data) {
    var sel = document.getElementById('inv-wh-filter');
    if (!sel) return;
    var html = '<option value="">全部仓库</option>';
    (data.warehouses || []).forEach(function(w) {
      html += '<option value="' + w + '">' + w + '</option>';
    });
    sel.innerHTML = html;
  }).catch(function() {});
}

function loadInv(opts) {
  opts = opts || {};
  var page = opts.page || _invPage || 1;
  _invPage = page;
  var wh = (document.getElementById('inv-wh-filter') || {}).value || '';
  var q = (document.getElementById('inv-search') || {}).value || '';
  var onlyOutOfStock = Boolean((document.getElementById('inv-out-of-stock') || {}).checked);
  var url = '/api/inventory/snapshots?page=' + page + '&page_size=50';
  if (wh) url += '&warehouse=' + encodeURIComponent(wh);
  if (q) url += '&q=' + encodeURIComponent(q);
  if (onlyOutOfStock) url += '&stock_status=out_of_stock';
  api(url).then(function(data) {
    var items = data.items || [];
    var total = data.total || 0;
    var tp = data.total_pages || 1;
    var hasMore = Boolean(data.has_more);
    var html = '<table class="data-table"><thead><tr><th>仓库</th><th>物料编码</th><th>物料名称</th><th>库存数量</th><th>同步时间</th></tr></thead><tbody>';
    items.forEach(function(s) {
      var qty = parseFloat(s.qty) || 0;
      var style = qty <= 0 ? ' style="color:#dc2626;font-weight:600;"' : '';
      html += '<tr' + style + '><td>' + s.warehouse_code + '</td><td>' + s.material_code + '</td><td>' + (s.material_name || '').slice(0, 40) + '</td><td>' + qty + '</td><td>' + (s.synced_at || '').slice(0, 10) + '</td></tr>';
    });
    html += '</tbody></table>';
    var w = document.getElementById('inv-table-wrap');
    if (w) w.innerHTML = html;
    // pagination
    var pg = document.getElementById('inv-pagination');
    if (pg) {
      var totalText = data.total_estimated ? '至少 ' + total + ' 条' : '共 ' + total + ' 条';
      var displayPages = data.total_estimated ? (hasMore ? page + 1 : page) : tp;
      var ph = '<span style="margin-right:12px;color:var(--muted);font-size:13px;">' + totalText + '</span>';
      ph += '<button class="button ghost ' + (page <= 1 ? 'disabled' : '') + '" id="inv-prev"' + (page <= 1 ? ' disabled' : '') + '>‹ 上一页</button> ';
      ph += '<span style="margin:0 8px;font-size:13px;">' + page + '/' + displayPages + '</span> ';
      ph += '<button class="button ghost ' + (!hasMore ? 'disabled' : '') + '" id="inv-next"' + (!hasMore ? ' disabled' : '') + '>下一页 ›</button>';
      pg.innerHTML = ph;
      var prev = document.getElementById('inv-prev');
      if (prev) prev.addEventListener('click', function() { if (page > 1) loadInv({page: page - 1}); });
      var next = document.getElementById('inv-next');
      if (next) next.addEventListener('click', function() { if (hasMore) loadInv({page: page + 1}); });
    }
  }).catch(function(e) { toast('加载库存失败: ' + (e.message || ''), true); });
}

// —— 库存导入历史 ——
function loadInvRecords() {
  api('/api/inventory/import-records').then(function(data) {
    var items = data.items || [];
    var html = '<table class="data-table"><thead><tr><th>时间</th><th>文件名</th><th>仓库</th><th>行数</th><th>操作人</th></tr></thead><tbody>';
    items.forEach(function(r) {
      html += '<tr><td>' + (r.created_at || '').slice(0, 16) + '</td><td>' + (r.file_name || '') + '</td><td>' + (r.warehouse || '') + '</td><td>' + r.row_count + '</td><td>' + (r.operated_by || '') + '</td></tr>';
    });
    html += '</tbody></table>';
    var w = document.getElementById('inv-records-table');
    if (w) w.innerHTML = html;
  }).catch(function(e) { toast('加载导入历史失败: ' + (e.message || ''), true); });
}

// Hook into inventory page init
var _origInvInit = window._invInited ? null : initInventoryPage;
var _origInvInit2 = initInventoryPage;
initInventoryPage = function() {
  _origInvInit2();
  var btn = document.getElementById('inv-refresh-records');
  if (btn) btn.addEventListener('click', loadInvRecords);
  // Also init tabs for records
  var tabsContainer = document.querySelector('[data-page="inventory"]');
  if (tabsContainer) {
    tabsContainer.querySelectorAll('.tab').forEach(function(tab) {
      tab.addEventListener('click', function() {
        if (this.dataset.tab === 'inv-records') loadInvRecords();
      });
    });
  }
};

window.gotoReviewRule = function(code) {
  // 1. Switch to review-rules page
  setActivePage('review-rules');
  // 2. Set search filter to this rule code
  if (window.tableStates && window.tableStates.reviewRules) {
    window.tableStates.reviewRules.q = code;
    window.tableStates.reviewRules.page = 1;
  }
  // 3. Set input value in form
  var form = document.getElementById('initial-review-rules-filter-form');
  if (form) {
    var input = form.querySelector('input[name="q"]');
    if (input) {
      input.value = code;
    }
  }
  // 4. Refresh rules list
  if (typeof refreshV2ReviewRules === 'function') {
    refreshV2ReviewRules();
  }
};

function orderDetailRowRaw(label, htmlValue) {
  var val = htmlValue === undefined || htmlValue === null || htmlValue === "" ? "-" : htmlValue;
  return '<div class="order-detail-row"><span class="order-detail-label">' + h(label) + '</span><span class="order-detail-value">' + val + '</span></div>';
}
