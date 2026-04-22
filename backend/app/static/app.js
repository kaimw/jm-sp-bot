const $ = (selector) => document.querySelector(selector);
let initialReviewState = { enabled: true, required_fields: [], rules: [], field_options: [], operator_options: [] };
let taskQueryState = { q: "", status: "", customer: "", product: "", salesperson: "", order_no: "", delivery: "", page: 1, page_size: 10 };
const tableStates = {
  departments: { q: "", status: "", page: 1, page_size: 10 },
  mails: { q: "", classification: "", direction: "", from_address: "", page: 1, page_size: 10 },
  outbound: { q: "", status: "", mail_type: "", recipient: "", page: 1, page_size: 10 },
  exceptions: { q: "", status: "Open", severity: "", exception_type: "", page: 1, page_size: 10 },
  jobs: { q: "", status: "", job_type: "", page: 1, page_size: 10 },
  attachments: { q: "", parse_status: "", content_type: "", mail_id: "", page: 1, page_size: 10 },
  audit: { q: "", event_type: "", actor: "", related_object_type: "", page: 1, page_size: 10 },
  backups: { q: "", status: "", backup_type: "", page: 1, page_size: 10 },
  reviewRules: { q: "", status: "", page: 1, page_size: 10 },
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
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
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

function appendChatMessage(role, label, message) {
  const log = $("#model-chat-log");
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
  const target = pages.find((page) => page.dataset.page === pageName) || pages.find((page) => page.dataset.page === "dashboard");
  if (!target) return;
  pages.forEach((page) => page.classList.toggle("is-active", page === target));
  document.querySelectorAll("[data-page-link]").forEach((link) => {
    link.classList.toggle("active", link.dataset.pageLink === target.dataset.page);
  });
  $("#page-title").textContent = target.dataset.title || "工作台";
  $("#page-subtitle").textContent = target.dataset.subtitle || "";
}

async function refreshDashboard() {
  const data = await api("/api/dashboard");
  const labels = [
    ["任务总数", data.tasks_total],
    ["草稿待确认", data.drafted],
    ["已下达", data.issued],
    ["生产疑问", data.questioned],
    ["已关闭", data.closed],
    ["发送失败", data.outbound_failed],
    ["变更/取消", data.change_review],
  ];
  $("#dashboard").innerHTML = labels
    .map(([label, value]) => `<div class="metric"><span>${h(label)}</span><strong>${h(value)}</strong></div>`)
    .join("");
}

async function refreshDepartments() {
  const data = normalizeListPayload(await api(`/api/departments?${queryFromState(tableStates.departments)}`), tableStates.departments);
  const rows = data.items || [];
  setSelectOptions("#departments-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.departments.status);
  $("#departments-list").innerHTML = rows
    .map(
      (row) => `
        <div class="row">
          <div><strong>${h(row.department_name)}</strong><br /><small>${h(row.department_code)}</small></div>
          <div><small>主送</small><br />${h(row.mail_to.join(", ") || "未配置")}</div>
          <div><small>抄送</small><br />${h(row.mail_cc.join(", ") || "无")}</div>
          <div><small>${h(row.status)}</small></div>
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
        <div class="row">
          <div>
            <strong>${h(row.task_no)}</strong><br />
            <small>${h(row.customer_name || "未识别客户")} · ${h(row.salesperson_email || "未知销售")}</small><br />
            <small>${h(row.external_order_no || "无订单号")}</small>
          </div>
          <div>${h(row.product_summary || "未识别产品")}<br /><small>${h(row.quantity_text || "")} ${h(row.expected_delivery_date || "")}</small></div>
          <div><small>${h(row.status)}</small></div>
          <div class="actions">
            <button class="button" data-action="workflow" data-id="${row.id}">查看工作流</button>
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
    Running: "status-running",
  }[status] || "status-muted";
}

async function refreshOutbound() {
  const data = normalizeListPayload(await api(`/api/outbound-mails?${queryFromState(tableStates.outbound)}`), tableStates.outbound);
  const rows = data.items || [];
  setSelectOptions("#outbound-filter-form [name=status]", data.status_options || [], "全部状态", tableStates.outbound.status);
  setSelectOptions("#outbound-filter-form [name=mail_type]", data.mail_type_options || [], "全部类型", tableStates.outbound.mail_type);
  $("#outbound-list").innerHTML =
    rows
      .map(
        (row) => `
        <div class="row">
          <div><strong>${h(row.subject)}</strong><br /><small>${h(row.mail_type)}</small></div>
          <div><small>主送</small><br />${h(row.to.join(", ") || "无")}</div>
          <div><small>抄送</small><br />${h(row.cc.join(", ") || "无")}</div>
          <div>
            <small class="status-text ${mailStatusClass(row.status)}">${h(row.status)}</small>
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

function reviewRuleId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function optionLabel(options, key) {
  return options.find((item) => item.key === key)?.label || key;
}

function renderInitialReviewRules() {
  const form = $("#initial-review-form");
  if (!form) return;
  form.querySelector("[name=enabled]").checked = Boolean(initialReviewState.enabled);
  const requiredNode = $("#initial-review-required-fields");
  const required = new Set(initialReviewState.required_fields || []);
  const fieldOptions = (initialReviewState.field_options || []).filter((item) => item.key !== "source_text");
  requiredNode.innerHTML = fieldOptions
    .map(
      (field) => `
        <label class="check-item">
          <input type="checkbox" name="required_field" value="${h(field.key)}" ${required.has(field.key) ? "checked" : ""} />
          <span>${h(field.label)}</span>
        </label>`
    )
    .join("");

  const fieldSelect = $("#initial-review-rule-form [name=field]");
  const operatorSelect = $("#initial-review-rule-form [name=operator]");
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
      optionLabel(initialReviewState.operator_options || [], rule.operator),
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
        <div class="row">
          <div><strong>${h(rule.name || "未命名规则")}</strong><br /><small>${h(rule.enabled === false ? "停用" : "启用")}</small></div>
          <div><small>字段 / 判断</small><br />${h(optionLabel(initialReviewState.field_options || [], rule.field))} · ${h(optionLabel(initialReviewState.operator_options || [], rule.operator))}</div>
          <div><small>规则值</small><br />${h(rule.value || "无")}</div>
          <div>
            <small>${h(rule.message || "未填写未通过原因")}</small>
            <div class="actions row-actions">
              <button class="button ghost" data-action="toggle-review-rule" data-id="${h(rule.id)}">${rule.enabled === false ? "启用" : "停用"}</button>
              <button class="button warn" data-action="delete-review-rule" data-id="${h(rule.id)}">删除</button>
            </div>
          </div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无自定义规则，当前仅执行必填项和内置风险初审。</div></div>`;
  renderListPagination("#initial-review-rules-pagination", "reviewRules", pageData);
}

async function refreshInitialReviewRules() {
  initialReviewState = await api("/api/initial-review/rules");
  renderInitialReviewRules();
}

async function saveInitialReviewRules() {
  const requiredFields = [...document.querySelectorAll("#initial-review-required-fields [name=required_field]:checked")].map(
    (input) => input.value
  );
  const enabled = $("#initial-review-form [name=enabled]").checked;
  initialReviewState = await api("/api/initial-review/rules", {
    method: "PUT",
    body: JSON.stringify({
      enabled,
      required_fields: requiredFields,
      rules: initialReviewState.rules || [],
    }),
  });
  renderInitialReviewRules();
}

async function refreshConfig() {
  const data = await api("/api/config");
  fillForm("#runtime-mail-form", data.configs || {});
  fillForm("#e2e-mail-form", data.configs || {});
  if (data.model) {
    fillForm("#model-form", data.model);
  }
  const password = $("#runtime-mail-form [name=bot_email_password]");
  if (password) password.value = "";
  document.querySelectorAll("#e2e-mail-form input[type=password]").forEach((input) => {
    input.value = "";
  });
  const modelKey = $("#model-form [name=api_key]");
  if (modelKey) modelKey.value = "";
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
        <div class="row">
          <div><strong>${h(row.subject)}</strong><br /><small>${h(row.from_address)}</small></div>
          <div><small>分类</small><br />${h(row.classification)} (${h(row.classification_confidence)})</div>
          <div><small>任务</small><br />${h(row.related_task_id || "未关联")}</div>
          <div><small>${h(row.created_at)}</small></div>
        </div>`
      )
      .join("") || `<div class="row"><div>暂无入库邮件</div></div>`;
  renderListPagination("#mails-pagination", "mails", data);
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
  $("#workflow-title").textContent = `${task.customer_name || "未识别客户"} · ${task.product_summary || "未识别产品"}`;
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
  const weekStats = periods.week?.task_stats || {};
  const monthStats = periods.month?.task_stats || {};
  $("#weekly-preview-time").textContent = `生成时间：${formatTime(data.generated_at)}`;
  $("#weekly-preview-title").textContent = data.subject || "商务生产任务单周报";
  $("#weekly-preview-meta").innerHTML = `
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
    refreshDepartments(),
    refreshTasks(),
    refreshOutbound(),
    refreshExceptions(),
    refreshInitialReviewRules(),
    refreshConfig(),
    refreshWeeklyReportRecipients(),
    refreshJobs(),
    refreshAttachments(),
    refreshMails(),
    refreshOps(),
    loadTemplate(),
  ]);
}

const defaultTableStates = JSON.parse(JSON.stringify(tableStates));
const tableRefreshers = {
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

async function refreshTable(key) {
  const refresher = tableRefreshers[key];
  if (refresher) await refresher();
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
  await api("/api/departments/default", {
    method: "PUT",
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

$("#initial-review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveInitialReviewRules();
  toast("初审规则已保存");
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
  initialReviewState.rules = [
    ...(initialReviewState.rules || []),
    {
      id: reviewRuleId(),
      name: data.name,
      field: data.field,
      operator: data.operator,
      value: data.value || "",
      message: data.message || `${optionLabel(initialReviewState.field_options || [], data.field)} 未通过初审规则：${data.name}`,
      enabled: true,
    },
  ];
  await saveInitialReviewRules();
  form.reset();
  toast("自定义初审规则已添加");
});

$("#initial-review-rules-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const id = target.dataset.id;
  if (target.dataset.action === "delete-review-rule") {
    initialReviewState.rules = (initialReviewState.rules || []).filter((rule) => rule.id !== id);
  }
  if (target.dataset.action === "toggle-review-rule") {
    initialReviewState.rules = (initialReviewState.rules || []).map((rule) =>
      rule.id === id ? { ...rule, enabled: rule.enabled === false } : rule
    );
  }
  await saveInitialReviewRules();
  toast("初审规则已更新");
});

$("#runtime-mail-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const values = Object.fromEntries(form.entries());
  if (!values.bot_email_password) delete values.bot_email_password;
  if (values.mail_auto_worker_interval_seconds && Number(values.mail_auto_worker_interval_seconds) < 300) {
    toast("邮件心跳间隔不能低于 300 秒");
    return;
  }
  values.llm_fallback_enabled = $("#runtime-mail-form [name=llm_fallback_enabled]").checked;
  await api("/api/config/mail", {
    method: "PUT",
    body: JSON.stringify(values),
  });
  toast("邮箱与运行参数已保存");
  await refreshAll();
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
  resultNode.textContent = "正在运行真实腾讯企业邮箱端到端测试。IMAP/SMTP 登录和单账号发信都已限制为至少 5 分钟一次，必要时测试会等待下一次窗口...";
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
  const result = await api("/api/reports/weekly/enqueue", { method: "POST" });
  toast(`周报邮件已进入外发队列：${result.status}`);
  await refreshAll();
});

$("#sync-mailbox").addEventListener("click", async () => {
  const result = await api("/api/mailbox/sync", { method: "POST" });
  toast(`已同步 ${result.imported} 封邮件，入队 ${result.queued} 条`);
  await refreshAll();
});

$("#run-jobs").addEventListener("click", async () => {
  const result = await api("/api/jobs/run-pending", { method: "POST" });
  toast(`队列完成 ${result.completed} 条，失败 ${result.failed} 条`);
  await refreshAll();
});

$("#send-pending").addEventListener("click", async () => {
  const result = await api("/api/outbound-mails/send-pending", { method: "POST" });
  toast(`已发送 ${result.sent} 封邮件，失败 ${result.failed || 0} 封`);
  await refreshAll();
});

$("#outbound-list").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target || target.dataset.action !== "retry-outbound") return;
  await api(`/api/outbound-mails/${target.dataset.id}/retry`, { method: "POST" });
  toast("已重新加入外发队列");
  await refreshAll();
});

$("#test-model").addEventListener("click", async () => {
  await api("/api/model-providers/test", { method: "POST" });
  toast("模型服务连通性正常");
  await refreshAll();
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

$("#tasks").addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  const id = target.dataset.id;
  const action = target.dataset.action;
  if (action === "workflow") {
    await openWorkflow(id);
  }
});

$("#workflow-close").addEventListener("click", closeWorkflow);
$("#workflow-modal").addEventListener("click", (event) => {
  if (event.target.id === "workflow-modal") closeWorkflow();
});

$("#weekly-preview-close").addEventListener("click", closeWeeklyReportPreview);
$("#weekly-preview-modal").addEventListener("click", (event) => {
  if (event.target.id === "weekly-preview-modal") closeWeeklyReportPreview();
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
    const product_summary = window.prompt("产品/规格，留空则不修改", "");
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
