import { chromium } from "playwright";
import { execFile } from "node:child_process";
import http from "node:http";
import net from "node:net";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const HOST = "127.0.0.1";
let CRM_PORT = Number(process.env.FXIAOKE_MOCK_PORT || "0");
let CDP_PORT = Number(process.env.FXIAOKE_TEST_CDP_PORT || "0");

function listen(server, port, host) {
  return new Promise((resolve, reject) => {
    const onError = (error) => {
      server.off("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.off("error", onError);
      resolve(server.address());
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(port, host);
  });
}

async function freePort(preferred = 0) {
  const server = net.createServer();
  const address = await listen(server, preferred, HOST);
  const port = typeof address === "object" && address ? address.port : preferred;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => {
      if (!body) return resolve({});
      try {
        resolve(JSON.parse(body));
      } catch (error) {
        reject(error);
      }
    });
  });
}

function sendJson(req, res, payload) {
  const origin = req.headers.origin || "null";
  res.writeHead(200, {
    "content-type": "application/json;charset=utf-8",
    "access-control-allow-origin": origin,
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "GET,POST,OPTIONS",
  });
  res.end(JSON.stringify(payload));
}

const orders = [
  {
    _id: "crm-order-001",
    name: "SO-CRM-001",
    account_id: "account-001",
    account_id__r: "测试客户A",
    new_opportunity_id: "opp-001",
    new_opportunity_id__r: "RayZoom G100",
    life_status: "normal",
    order_time: 1780329600000,
    field_2nI76__c: "option1",
    order_amount: "20425.00",
    payment_amount: "1024.00",
    receivable_amount: "19401.00",
    invoice_amount: "20425.00",
    product_amount: "20425.00",
    logistics_status: "1",
    owner_department: "商务部",
    create_time: 1780329600000,
    last_modified_time: 1780333200000,
    total_num: 2,
  },
  {
    _id: "crm-order-002",
    name: "SO-CRM-002",
    account_id: "account-002",
    account_id__r: "测试客户B",
    new_opportunity_id: "opp-002",
    new_opportunity_id__r: "RayZoom G200",
    life_status: "normal",
    order_time: 1780416000000,
    field_2nI76__c: "option1",
    order_amount: "35100.00",
    payment_amount: "35100.00",
    receivable_amount: "0.00",
    invoice_amount: "35100.00",
    product_amount: "35100.00",
    logistics_status: "1",
    owner_department: "商务部",
    create_time: 1780416000000,
    last_modified_time: 1780419600000,
    total_num: 2,
  },
];

function detailFor(id) {
  const order = orders.find((item) => item._id === id) || orders[0];
  return {
    Result: { FailureCode: 0, StatusCode: 0 },
    Value: {
      data: {
        ...order,
        owner: "owner-001",
        owner__r: { name: "刘测试" },
        order_status: "7",
        invoice_status: "1",
        ship_to_id: { name: "张三", id: "contact-001" },
        ship_to_add: "湖北省武汉市东湖高新区测试路 1 号",
        delivery_date: "2026-06-30",
        delivery_comment: "按合同约定分批发货",
        UDAttach1__c: [
          {
            filename: "合同盖章版.pdf",
            signedUrl: `http://${HOST}:${CRM_PORT}/files/contract.pdf`,
            signature: "file-001",
          },
        ],
      },
      objectDescribeExt: {
        fields: {
          logistics_status: {
            api_name: "logistics_status",
            label: "发货状态",
            options: [
              { value: "1", label: "待发货" },
              { value: "3", label: "已发货" },
            ],
          },
          order_status: {
            api_name: "order_status",
            label: "订单状态",
            options: [{ value: "7", label: "已确认" }],
          },
          invoice_status: {
            api_name: "invoice_status",
            label: "开票状态",
            options: [{ value: "1", label: "已开票" }],
          },
          ship_to_id: { api_name: "ship_to_id", label: "收货人" },
          ship_to_add: { api_name: "ship_to_add", label: "收货地址" },
          delivery_date: { api_name: "delivery_date", label: "交货日期" },
        },
      },
    },
  };
}

async function startMockCrm() {
  const server = http.createServer(async (req, res) => {
    if (req.method === "OPTIONS") return sendJson(req, res, {});
    if (req.url === "/") return sendJson(req, res, { ok: true });
    if (req.url?.startsWith("/SalesOrderObj/controller/List")) {
      const payload = await readJson(req);
      const query = JSON.parse(payload.search_query_info || "{}");
      const offset = Number(query.offset || 0);
      const limit = Number(query.limit || 20);
      return sendJson(req, res, { Result: { StatusCode: 0 }, Value: { dataList: orders.slice(offset, offset + limit) } });
    }
    if (req.url?.startsWith("/SalesOrderObj/controller/WebDetail")) {
      const payload = await readJson(req);
      return sendJson(req, res, detailFor(payload.objectDataId));
    }
    if (req.url?.startsWith("/files/")) {
      res.writeHead(200, { "content-type": "application/pdf" });
      return res.end("mock file");
    }
    res.writeHead(404);
    res.end("not found");
  });
  const address = await listen(server, CRM_PORT, HOST);
  if (typeof address === "object" && address?.port) CRM_PORT = address.port;
  return server;
}

function execFilePromise(file, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { ...options, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

async function waitForCdp() {
  const url = `http://${HOST}:${CDP_PORT}/json/version`;
  for (let index = 0; index < 80; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Keep polling until Chromium exposes the debugging endpoint.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`CDP endpoint did not start: ${url}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  if (!CRM_PORT) CRM_PORT = await freePort();
  if (!CDP_PORT) CDP_PORT = await freePort();
  const mockServer = await startMockCrm();
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "fxiaoke-integration-"));
  const userDataDir = path.join(tempDir, "chrome-profile");
  const listRequestPath = path.join(tempDir, "list-request.json");
  const detailRequestPath = path.join(tempDir, "detail-request.json");
  const chrome = (await import("node:child_process")).spawn(
    chromium.executablePath(),
    [
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${userDataDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--headless=new",
      `http://${HOST}:${CRM_PORT}/`,
    ],
    { stdio: "ignore" },
  );

  try {
    await fs.writeFile(
      listRequestPath,
      JSON.stringify({
        method: "POST",
        url: `http://${HOST}:${CRM_PORT}/SalesOrderObj/controller/List`,
        postData: JSON.stringify({ search_query_info: JSON.stringify({ offset: 0, limit: 20 }) }),
      }),
    );
    await fs.writeFile(
      detailRequestPath,
      JSON.stringify({
        method: "POST",
        url: `http://${HOST}:${CRM_PORT}/SalesOrderObj/controller/WebDetail`,
        postData: JSON.stringify({ objectDataId: "{{crm_order_id}}", objectDescribeApiName: "SalesOrderObj" }),
      }),
    );

    await waitForCdp();
    const { stdout } = await execFilePromise(
      process.execPath,
      ["scripts/fxiaoke_replay_sales_orders.mjs", `--request=${listRequestPath}`, `--detail-request=${detailRequestPath}`],
      { cwd: process.cwd(), env: { ...process.env, FXIAOKE_CDP_URL: `http://${HOST}:${CDP_PORT}` } },
    );
    const summary = JSON.parse(stdout);
    const output = JSON.parse(await fs.readFile(summary.jsonPath, "utf8"));
    const first = output.rows.find((row) => row.crm_order_no === "SO-CRM-001");

    assert(summary.ok === true, "CRM replay summary should be ok");
    assert(summary.total === 2 && summary.count === 2, "CRM replay should sync 2 mock orders");
    assert(summary.detailPages.every((item) => item.status === "Synced"), "all detail pages should sync");
    assert(first.sales_user_name === "刘测试", "sales owner should be mapped from owner__r");
    assert(first.logistics_status === "待发货", "logistics enum should be converted to label");
    assert(first.invoice_status === "已开票", "invoice enum should be converted to label");
    assert(first.attachments?.[0]?.file_url?.includes("/files/contract.pdf"), "attachment signedUrl should be downloadable");

    console.log(JSON.stringify({ ok: true, checked: ["list", "detail", "owner", "enum_labels", "attachments"], jsonPath: summary.jsonPath }, null, 2));
  } finally {
    chrome.kill();
    await new Promise((resolve) => mockServer.close(resolve));
    await fs.rm(tempDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: String(error), stdout: error.stdout, stderr: error.stderr }, null, 2));
  process.exitCode = 1;
});
