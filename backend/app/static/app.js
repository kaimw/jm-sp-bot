const $ = (selector) => document.querySelector(selector);
const hiddenPages = new Set(["orders", "templates"]);
let initialReviewState = { enabled: true, required_fields: [], rules: [], field_options: [], operator_options: [] };
let workflowRulesState = { items: [], editingVersionId: "", editingRules: null, readonly: false };
let productionDepartmentState = { items: [] };
let workflowChatState = { messages: [], compiledRule: null, validationErrors: [], ready: false, editVersionId: "", editWorkflowName: "" };
let runtimeConfigState = {};
let startupReadinessState = { ready: false, missing: [] };
let dashboardViewState = { period: "year" };
let baiduMapLoadPromise = null;
let baiduDemandMap = null;
let taskQueryState = { q: "", status: "", customer: "", product: "", salesperson: "", order_no: "", delivery: "", page: 1, page_size: 10 };
const tableStates = {
  workflows: { q: "", status: "", page: 1, page_size: 10 },
  departments: { q: "", status: "", page: 1, page_size: 10 },
  mails: { q: "", classification: "", direction: "", from_address: "", page: 1, page_size: 10 },
  outbound: { q: "", status: "", mail_type: "", recipient: "", page: 1, page_size: 10 },
  exceptions: { q: "", status: "Open", severity: "", exception_type: "", page: 1, page_size: 10 },
  jobs: { q: "", status: "", job_type: "", page: 1, page_size: 10 },
  attachments: { q: "", parse_status: "", content_type: "", mail_id: "", page: 1, page_size: 10 },
  audit: { q: "", event_type: "", actor: "", related_object_type: "", page: 1, page_size: 10 },
  backups: { q: "", status: "", backup_type: "", page: 1, page_size: 10 },
  reviewRules: { q: "", status: "", page: 1, page_size: 10 },
  productsSpu: { q: "", page: 1, page_size: 10 },
  productsSku: { spu_id: "", page: 1, page_size: 10 },
  productsPricing: { sku_id: "", page: 1, page_size: 10 },
  productsPromotions: { page: 1, page_size: 10 },
};

async function api(path, options = {}) {
  const { skipAuthRedirect, headers, ...fetchOptions } = options;
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(headers || {}) },
    credentials: "same-origin",
    ...fetchOptions,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    if (response.status === 401 && !skipAuthRedirect) {
      showLogin();
    }
    throw new Error(error.detail || response.statusText);
  }
  return response.json();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
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
  noticeTrail(parts, message, "error");
  toast(message);
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

function showApp() {
  document.body.classList.add("is-authenticated");
  const screen = $("#login-screen");
  if (screen) screen.hidden = true;
}

async function ensureAuthenticated() {
  const data = await api("/api/auth/me", { skipAuthRedirect: true });
  if (data.authenticated) {
    showApp();
    return true;
  }
  showLogin();
  return false;
}

function currentPageName() {
  return (window.location.hash || "#dashboard").slice(1) || "dashboard";
}

function setActivePage(pageName = currentPageName()) {
  const pages = [...document.querySelectorAll(".page")];
  const visiblePages = pages.filter((page) => !hiddenPages.has(page.dataset.page));
  const target = visiblePages.find((page) => page.dataset.page === pageName) || visiblePages.find((page) => page.dataset.page === "dashboard");
  if (!target) return;
  pages.forEach((page) => page.classList.toggle("is-active", page === target));
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.classList.toggle("active", link.dataset.pageLink === target.dataset.page);
  });
  $("#page-title").textContent = target.dataset.title || "工作台";
  $("#page-subtitle").textContent = target.dataset.subtitle || "";

  if (pageName === "skill-lab" || pageName === "self-maintenance") {
    refreshSkills();
  }
}

async function refreshDashboard() {
  const [data, health] = await Promise.all([api("/api/dashboard"), api("/api/system/health")]);
  const labels = [
    ["任务总数", data.tasks_total, "全部销售需求任务"],
    ["草稿待确认", data.drafted, "等待初审或人工补充"],
    ["已下达", data.issued, "已发送生产任务单"],
    ["生产疑问", data.questioned, "生产侧待答疑"],
    ["已关闭", data.closed, "已确认、取消或终止"],
    ["发送失败", data.outbound_failed, "需运维处理的外发"],
    ["变更/取消", data.change_review, "待复核的变更请求"],
  ];
  $("#dashboard").innerHTML = labels
    .map(([label, value, hint]) => `<div class="metric"><span>${h(label)}</span><strong>${h(value)}</strong><small>${h(hint)}</small></div>`)
    .join("");
  renderSystemHealth(health);
  renderDashboardInsights(data.analytics || {});
}

function renderSystemHealth(health) {
  const node = $("#system-health");
  if (!node) return;
  const readiness = health.readiness || { ready: false, missing: [] };
  const worker = health.worker || {};
  const outbound = health.queues?.outbound || {};
  const processing = health.queues?.processing || {};
  const outboundCounts = outbound.counts || {};
  const processingCounts = processing.counts || {};
  node.innerHTML = `
    <div class="health-card ${readiness.ready ? "is-ok" : "is-warn"}">
      <small>启动就绪</small>
      <strong>${readiness.ready ? "已就绪" : "未就绪"}</strong>
      <span>${readiness.ready ? "配置完整" : `缺少：${h((readiness.missing || []).join("、") || "未知")}`}</span>
    </div>
    <div class="health-card ${health.bot_enabled ? "is-ok" : "is-warn"}">
      <small>系统开关</small>
      <strong>${health.bot_enabled ? "运行中" : "已停用"}</strong>
      <span>${health.bot_enabled ? "自动流程允许执行" : "不会自动消费邮件和队列"}</span>
    </div>
    <div class="health-card">
      <small>自动 worker</small>
      <strong>${worker.auto_worker_enabled ? `${h(worker.configured_interval_seconds)} 秒/轮` : "未启用"}</strong>
      <span>${worker.last_finished_at ? `最近完成：${h(formatTime(worker.last_finished_at))}` : "当前进程尚无完成记录"}</span>
    </div>
    <div class="health-card ${Number(outboundCounts.Pending || 0) ? "is-warn" : "is-ok"}">
      <small>外发队列</small>
      <strong>Pending ${h(outboundCounts.Pending || 0)}</strong>
      <span>自动 ${h(outbound.pending_auto_dispatchable || 0)} / 手动 ${h(outbound.pending_manual_only || 0)} / 失败 ${h(outboundCounts.Failed || 0)}</span>
    </div>
    <div class="health-card ${Number(processingCounts.Pending || 0) ? "is-warn" : "is-ok"}">
      <small>入库队列</small>
      <strong>Pending ${h(processingCounts.Pending || 0)}</strong>
      <span>Running ${h(processingCounts.Running || 0)} / Failed ${h(processingCounts.Failed || 0)}</span>
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
        <div class="row clickable-row" data-outbound-id="${h(row.id)}" role="button" tabindex="0" title="查看外发邮件详情">
          <div><strong>${h(row.subject)}</strong><br /><small>${h(row.mail_type)}</small></div>
          <div><small>主送</small><br />${h(row.to.join(", ") || "无")}</div>
          <div><small>抄送</small><br />${h(row.cc.join(", ") || "无")}</div>
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
    const input = form.querySelector(`[name=${key}]`);
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
  if (!button) return;
  const enabled = configEnabled(runtimeConfigState.bot_enabled, false);
  button.textContent = enabled ? "停用系统" : "启用系统";
  button.title = enabled
    ? "停用机器人邮箱监听与自动处理"
    : (startupReadinessState.ready ? "启用机器人邮箱监听与自动处理" : `启动前需补齐：${(startupReadinessState.missing || []).join("、")}`);
  button.classList.toggle("is-paused", !enabled);
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
  if (!fieldSelect || !operatorSelect) return;
  fieldSelect.innerHTML = (initialReviewState.field_options || [])
    .map((field) => `<option value="${h(field.key)}">${h(field.label)}</option>`)
    .join("");
  operatorSelect.innerHTML = (initialReviewState.operator_options || [])
    .map((operator) => `<option value="${h(operator.key)}">${h(operator.label)}</option>`)
    .join("");

  const q = tableStates.reviewRules.q.trim().toLowerCase();
  const status = tableStates.reviewRules.status;
  const filteredRules = (initialReviewState.rules || []).filter((rule) => {
    const enabled = rule.enabled !== false;
    if (status === "enabled" && !enabled) return false;
    if (status === "disabled" && enabled) return false;
    if (!q) return true;
    const haystack = [
      rule.name,
      optionLabel(initialReviewState.field_options || [], rule.field),
      reviewOperatorLabel(rule.operator),
      rule.value,
      rule.message,
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
            <span class="status-pill ${rule.enabled === false ? "is-muted" : "is-active"}">${h(reviewRuleStatusText(rule))}</span>
          </div>
          <div class="review-rule-field"><small>字段 / 判断</small><span>${h(optionLabel(initialReviewState.field_options || [], rule.field))} · ${h(reviewOperatorLabel(rule.operator))}</span></div>
          <div class="review-rule-value"><small>规则值</small><span>${h(rule.value || "无")}</span></div>
          <div class="review-rule-message"><small>未通过原因</small><span>${h(rule.message || "未填写未通过原因")}</span></div>
          <div class="actions row-actions review-rule-actions">
              ${
                isReadonlyReviewRule(rule)
                  ? `<span class="status-pill">系统内置 · 只读</span>`
                  : `
                    <button class="button ghost" data-action="toggle-review-rule" data-id="${h(rule.id)}">${rule.enabled === false ? "启用" : "停用"}</button>
                    <button class="button warn" data-action="delete-review-rule" data-id="${h(rule.id)}">删除</button>
                  `
              }
          </div>
        </div>`
      )
      .join("") || `<div class="empty-note">暂无自定义规则，当前仅执行必填项和内置风险初审。</div>`;
  renderListPagination("#initial-review-rules-pagination", "reviewRules", pageData);
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

function ensureWorkflowRequiredFieldsInBody(bodyTemplate, requiredFields) {
  const body = String(bodyTemplate || "").trim();
  const extraFields = (requiredFields || []).filter((field) => {
    const key = String(field || "").trim();
    if (!key) return false;
    if (["customer_name", "product_summary", "quantity_text", "expected_delivery_date", "external_order_no"].includes(key)) return false;
    return !workflowTemplateHasVariable(body, key);
  });
  if (!extraFields.length) return body;
  const lines = ["流程必填信息：", ...extraFields.map((field) => `${workflowFieldLabel(field)}：{{${field}}}`)];
  return [body, lines.join("\n")].filter(Boolean).join("\n\n");
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
  const requiredNode = $("#workflow-mail-preview-required-fields");
  if (!subjectNode || !bodyNode) return;
  let rule;
  if (typeof ruleOrRaw === "string") {
    const source = String(ruleOrRaw || "").trim();
    if (!source) {
      subjectNode.textContent = "请先选择流程规则";
      bodyNode.textContent = "请先选择流程规则";
      if (requiredNode) requiredNode.innerHTML = `<span class="empty-note">请先选择流程规则</span>`;
      return;
    }
    try {
      rule = JSON.parse(source);
    } catch (error) {
      subjectNode.textContent = "JSON 解析失败";
      bodyNode.textContent = error?.message || "请检查 JSON 格式";
      if (requiredNode) requiredNode.innerHTML = `<span class="empty-note">JSON 解析失败</span>`;
      return;
    }
  } else {
    rule = ruleOrRaw || {};
  }
  const context = workflowPreviewContext(rule);
  const requiredFields = Array.isArray(rule?.required_fields) ? rule.required_fields.map((item) => String(item || "").trim()).filter(Boolean) : [];
  const subjectTemplate = String(rule?.subject_template || "").trim();
  const bodyTemplate = ensureWorkflowRequiredFieldsInBody(String(rule?.body_template || "").trim(), requiredFields);
  subjectNode.textContent = subjectTemplate
    ? renderTemplateWithContext(subjectTemplate, context)
    : "（未配置 subject_template，保存后将使用系统默认主题模板）";
  bodyNode.textContent = bodyTemplate
    ? renderTemplateWithContext(bodyTemplate, context)
    : "（未配置 body_template，保存后将使用系统默认正文模板）";
  if (requiredNode) {
    requiredNode.innerHTML =
      requiredFields
        .map((field) => `<span class="workflow-preview-field"><strong>${h(workflowFieldLabel(field))}</strong><small>${h(context[field] || "示例值")}</small></span>`)
        .join("") || `<span class="empty-note">当前流程未勾选必填字段。</span>`;
  }
}

function syncWorkflowRuleEditorState() {
  const form = $("#workflow-rule-editor-form");
  if (!form || !workflowRulesState.editingRules) return null;
  if (workflowRulesState.readonly) return workflowRulesState.editingRules;
  const requiredFields = [...form.querySelectorAll("#workflow-required-fields [name=workflow_required_field]:checked")].map((input) => input.value);
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
  const requiredFields = Array.isArray(rules.required_fields) ? rules.required_fields : [];
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
  $("#workflow-required-fields").innerHTML =
    requiredFields
      .map(
        (field) => `
          <label class="check-item">
            <input type="checkbox" name="workflow_required_field" value="${h(field)}" checked ${readonly ? "disabled" : ""} />
            <span>${h(workflowFieldLabel(field))}</span>
          </label>`
      )
      .join("") || `<div class="empty-note">当前流程未配置必填字段。</div>`;

  const existingIds = new Set(reviewRules.map((rule) => String(rule.id || "")));
  const selectableRules = (initialReviewState.rules || []).filter((rule) => !isReadonlyReviewRule(rule) && !existingIds.has(String(rule.id || "")));
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
              <small>${h(isBuiltin ? "内置默认流程（只读）" : row.approved_at || row.created_at || "")}</small>
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
  fillForm("#e2e-mail-form", data.configs || {});
  if (data.model) {
    fillForm("#model-form", data.model);
  }
  const password = $("#runtime-mail-form [name=bot_email_password]");
  if (password) password.value = "";
  const baiduMapAk = $("#runtime-mail-form [name=baidu_map_ak]");
  if (baiduMapAk) baiduMapAk.value = "";
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
          <div><small>尝试</small><br />${h(row.attempt_count)}</div>
          <div><small>${h(row.error_message || row.created_at)}</small></div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无入库队列任务</div></div>`;
  renderListPagination("#jobs-pagination", "jobs", data);
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
          <div><small>任务</small><br />${h(row.related_task_id || "未关联")}</div>
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
    <div><small>关联任务</small><strong>${h(detail.related_task_id || "未关联")}</strong></div>
    <div><small>附件</small><strong>${h((detail.attachments || []).map((item) => item.file_name).join(", ") || "无")}</strong></div>
  `;
  $("#mail-detail-body").textContent = detail.body_text || "无正文内容";
  $("#mail-detail-modal").hidden = false;
}

async function openOutboundDetail(outboundId) {
  const detail = await api(`/api/outbound-mails/${outboundId}`);
  $("#mail-detail-title").textContent = detail.subject || "外发邮件详情";
  $("#mail-detail-meta").textContent = `外发队列 · ${detail.created_at || ""}`;
  $("#mail-detail-fields").innerHTML = `
    <div><small>外发ID</small><strong>${h(detail.id || "未记录")}</strong></div>
    <div><small>邮件类型</small><strong>${h(detail.mail_type || "未记录")}</strong></div>
    <div><small>主送</small><strong>${h((detail.to || []).join(", ") || "未记录")}</strong></div>
    <div><small>抄送人</small><strong>${h((detail.cc || []).join(", ") || "无")}</strong></div>
    <div><small>状态</small><strong>${h(detail.status || "未记录")}</strong></div>
    <div><small>关联任务</small><strong>${h(detail.related_task_id || "未关联")}</strong></div>
    <div><small>关联版本</small><strong>${h(detail.related_version_id || "未关联")}</strong></div>
    <div><small>幂等键</small><strong>${h(detail.idempotency_key || "未记录")}</strong></div>
  `;
  $("#mail-detail-body").textContent = detail.body || "无正文内容";
  $("#mail-detail-modal").hidden = false;
}

function closeMailDetail() {
  $("#mail-detail-modal").hidden = true;
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
          <div><small>${h(row.created_at)}</small></div>
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
          <div><small>${h(row.created_at)}</small></div>
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

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
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

async function openWorkflow(id) {
  const data = await api(`/api/tasks/${id}/workflow`);
  const task = data.task || {};
  $("#workflow-task-no").textContent = task.task_no || "生产任务";
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
        (row) => `
        <div class="row">
          <div><strong>${h(row.exception_type)}</strong><br /><small>${h(row.id)}</small></div>
          <div><small>级别</small><br />${h(row.severity)}</div>
          <div><small>状态</small><br />${h(row.status)}</div>
          <div>
            <small>${h(summarizeExceptionDetail(row.detail))}</small>
            <div class="actions row-actions">
              <button class="button ghost" data-action="patch-exception" data-id="${h(row.id)}">补字段</button>
              <button class="button ghost" data-action="resolve-exception" data-id="${h(row.id)}">关闭</button>
            </div>
          </div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无异常</div></div>`;
  renderListPagination("#exceptions-pagination", "exceptions", data);
}

async function loadTemplate() {
  const template = await api("/api/templates/production-task");
  $("#template-form [name=subject_template]").value = template.subject_template;
  $("#template-form [name=body_template]").value = template.body_template;
}

async function refreshAll() {
  await Promise.all([
    refreshDashboard(),
    refreshSkills(),
    refreshDepartments(),
    refreshTasks(),
    refreshOutbound(),
    refreshExceptions(),
    refreshInitialReviewRules(),
    refreshWorkflowRules(),
    refreshConfig(),
    refreshWeeklyReportRecipients(),
    refreshJobs(),
    refreshAttachments(),
    refreshMails(),
    refreshOps(),
    loadTemplate(),
    refreshProductsSpu(),
    refreshProductsSku(),
    refreshProductsPricing(),
    refreshProductsPromotions(),
  ]);
}

const defaultTableStates = JSON.parse(JSON.stringify(tableStates));
const tableRefreshers = {
  workflows: refreshWorkflowRules,
  departments: refreshDepartments,
  mails: refreshMails,
  outbound: refreshOutbound,
  exceptions: refreshExceptions,
  jobs: refreshJobs,
  attachments: refreshAttachments,
  audit: refreshOps,
  backups: refreshOps,
  reviewRules: async () => renderInitialReviewRules(),
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
    await refreshTable(key);
  });
  const reset = form.querySelector("[data-filter-reset]");
  if (reset) {
    reset.addEventListener("click", async () => {
      const state = tableStates[key];
      if (!state) return;
      const pageSize = state.page_size;
      Object.assign(state, defaultTableStates[key] || {}, { page: 1, page_size: pageSize });
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
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(form.entries())),
      skipAuthRedirect: true,
    });
    showApp();
    toast("已登录");
    await refreshAll();
  } catch (error) {
    toast(error.message || "登录失败");
  }
});

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
    const exists = (workflowRulesState.editingRules.review_rules || []).some((rule) => String(rule.id || "") === sourceId);
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

$("#workflow-rule-editor-form")?.addEventListener("change", (event) => {
  if (event.target.matches("[name=workflow_required_field], [name=routing_to_names], [name=routing_cc_names], [name=max_question_rounds], [name=conversation_exceeded_message]")) {
    syncWorkflowRuleEditorState();
  }
});

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
  values.bot_enabled = $("#runtime-mail-form [name=bot_enabled]").checked;
  values.llm_fallback_enabled = $("#runtime-mail-form [name=llm_fallback_enabled]").checked;
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
  const enabled = configEnabled(runtimeConfigState.bot_enabled, false);
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
    const runtimeToggle = $("#runtime-mail-form [name=bot_enabled]");
    if (runtimeToggle) runtimeToggle.checked = nextEnabled;
    await refreshConfig();
    renderSystemToggle();
    toast(nextEnabled ? "系统已启用" : "系统已停用");
  } catch (error) {
    notifyError(error, ["系统", nextEnabled ? "启动失败" : "停用失败"]);
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
  const row = event.target.closest("[data-mail-id]");
  if (!row) return;
  await guardedAction(["邮件", "详情"], async () => openMailDetail(row.dataset.mailId));
});

$("#mails-list")?.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
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



$("#exceptions-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const id = target.dataset.id;
  const action = target.dataset.action;
  if (action === "resolve-exception") {
    const note = window.prompt("关闭说明", "已人工处理");
    if (note === null) return;
    await api(`/api/exceptions/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ note }),
    });
    toast("异常已关闭");
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

async function refreshProductsSpu() {
  const data = await api(`/api/products/spu?${queryFromState(tableStates.productsSpu)}`);
  const rows = data.items || [];
  $("#products-spu-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div><strong>${h(row.spu_id)}</strong><br /><small>${h(row.name)}</small></div>
      <div><small>${h(row.brand || "-")}</small><br /><small>${h(row.category || "-")}</small></div>
      <div><small>${h(formatTime(row.created_at))}</small></div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>暂无 SPU 数据</div></div>`;
  renderListPagination("#products-spu-pagination", "productsSpu", data);
}

async function refreshProductsSku() {
  const data = await api(`/api/products/sku?${queryFromState(tableStates.productsSku)}`);
  const rows = data.items || [];
  $("#products-sku-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div><strong>${h(row.sku_id)}</strong><br /><small>${h(JSON.stringify(row.attributes))}</small></div>
      <div><a href="#" class="link" data-action="goto-spu" data-spu="${h(row.spu_id)}">${h(row.spu_id)}</a></div>
      <div><small>${h(row.status)}</small><br /><a href="#" class="link" data-action="quick-new-pricing" data-sku-uuid="${h(row.id)}" data-sku-id="${h(row.sku_id)}">配置价格</a></div>
      <div><small>${h(formatTime(row.created_at))}</small></div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>暂无 SKU 数据</div></div>`;
  renderListPagination("#products-sku-pagination", "productsSku", data);
}

async function refreshProductsPricing() {
  const data = await api(`/api/pricing?${queryFromState(tableStates.productsPricing)}`);
  const rows = data.items || [];
  $("#products-pricing-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div><strong>${h(row.channel)}</strong> / <a href="#" class="link" data-action="goto-sku" data-sku="${h(row.sku_id)}">${h(row.sku_id)}</a><br /><small>${h(row.currency)}</small></div>
      <div>
        <small>A: ${h(row.tier_a_price || "-")} | B: ${h(row.tier_b_price || "-")} | C: ${h(row.tier_c_price || "-")}</small>
        ${row.promo_start_time ? `<br/><small class="text-secondary">${h(formatTime(row.promo_start_time))} 至 ${h(formatTime(row.promo_end_time))}</small>` : ""}
      </div>
      <div><strong>${h(row.map_price)}</strong></div>
      <div><small>${h(formatTime(row.updated_at))}</small></div>
    </div>
  `).join("") || `<div class="row product-row product-empty-row"><div>暂无定价数据</div></div>`;
  renderListPagination("#products-pricing-pagination", "productsPricing", data);
}

async function refreshProductsPromotions() {
  const data = await api(`/api/promotions?${queryFromState(tableStates.productsPromotions)}`);
  const rows = data.items || [];
  $("#products-promotions-list").innerHTML = rows.map(row => `
    <div class="row product-row">
      <div><strong>${h(row.name)}</strong><br /><small>${h(row.channel || "通用")}</small></div>
      <div><small>${h(row.discount_type === 'percentage' ? '比例' : '固定减免')}</small><br /><strong>${h(row.discount_value)}</strong></div>
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

// Product Tab Logic
$("#products-tabs")?.addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") return;
  const tabName = e.target.dataset.tab;
  document.querySelectorAll("#products-tabs button").forEach(b => b.classList.toggle("active", b === e.target));
  document.querySelectorAll("[id^='products-'][id$='-tab']").forEach(el => el.hidden = true);
  document.querySelectorAll("[id^='products-'][id$='-tab']").forEach(el => el.classList.remove("is-active"));
  const activeTab = $(`#products-${tabName}-tab`);
  if (activeTab) {
    activeTab.hidden = false;
    activeTab.classList.add("is-active");
  }
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
  if (target.dataset.action === "new-sku") openModal("#product-sku-modal");
  if (target.dataset.action === "new-pricing") openModal("#product-pricing-modal");
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
    document.querySelectorAll("#products-tabs button").forEach(b => {
      if (b.dataset.tab === "spu") b.click();
    });
  }

  if (target.dataset.action === "goto-sku") {
    e.preventDefault();
    const skuId = target.dataset.sku;
    tableStates.productsPricing.sku_id = skuId;
    tableStates.productsPricing.page = 1;
    $("#products-pricing-filter-form [name=sku_id]").value = skuId;
    document.querySelectorAll("#products-tabs button").forEach(b => {
      if (b.dataset.tab === "pricing") b.click();
    });
  }

  if (target.dataset.action === "quick-new-pricing") {
    e.preventDefault();
    const skuUuid = target.dataset.skuUuid;
    openModal("#product-pricing-modal");
    $("#product-pricing-form [name=sku_uuid]").value = skuUuid;
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
    form.elements.name.value = rule.name || "";
    form.elements.channel.value = rule.channel || "";
    form.elements.discount_type.value = rule.discount_type || "percentage";
    form.elements.discount_value.value = rule.discount_value || "";
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
});

$("#product-spu-close")?.addEventListener("click", () => closeModal("#product-spu-modal"));
$("#product-sku-close")?.addEventListener("click", () => closeModal("#product-sku-modal"));
$("#product-pricing-close")?.addEventListener("click", () => closeModal("#product-pricing-modal"));
$("#product-promotion-close")?.addEventListener("click", () => closeModal("#product-promotion-modal"));
$("#product-import-close")?.addEventListener("click", () => closeModal("#product-import-modal"));

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

$("#product-pricing-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData);
  // Ensure integers where needed
  ["map_price", "tier_a_price", "tier_b_price", "tier_c_price"].forEach(k => {
    if (payload[k] !== undefined && payload[k] !== "") {
      payload[k] = parseInt(payload[k], 10);
    } else {
      payload[k] = null;
    }
  });
  ["promo_start_time", "promo_end_time"].forEach(k => {
    if (!payload[k]) payload[k] = null;
  });
  await guardedAction(["物料中心", "配置价格"], async () => {
    await api("/api/pricing", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast("渠道价格配置成功");
    closeModal("#product-pricing-modal");
    e.target.reset();
    refreshProductsPricing();
  });
});

$("#product-promotion-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData);
  payload.discount_value = parseInt(payload.discount_value, 10);
  if (isNaN(payload.discount_value)) {
    alert("请输入有效的折扣数值");
    return;
  }
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
  tableStates.productsSku.spu_id = e.target.spu_id.value;
  tableStates.productsSku.page = 1;
  refreshProductsSku();
});

$("#products-pricing-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  tableStates.productsPricing.sku_id = e.target.sku_id.value;
  tableStates.productsPricing.page = 1;
  refreshProductsPricing();
});

resetWorkflowChat();
setActivePage();
ensureAuthenticated()
  .then((authenticated) => {
    if (authenticated) {
      return refreshAll();
    }
    return null;
  })
  .catch((error) => {
    showLogin();
    toast(error.message || "请先登录");
  });
