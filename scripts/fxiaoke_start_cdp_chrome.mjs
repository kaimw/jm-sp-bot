#!/usr/bin/env node
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const CRM_HOME_URL = "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj";
const CRM_LOGIN_URL = `https://www.fxiaoke.com/proj/page/loginv2?returnUrl=${encodeURIComponent(CRM_HOME_URL)}`;

function argValue(name, fallback = "") {
  const prefix = `--${name}=`;
  const hit = process.argv.find((arg) => arg.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : fallback;
}

function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

function chromeExecutable() {
  const candidates = [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error("未找到 Chrome 可执行文件。可通过 CHROME_BIN=/path/to/chrome 指定。");
}

const port = Number(argValue("port", process.env.FXIAOKE_CDP_PORT || "9333"));
const userDataDir = argValue("user-data-dir", process.env.FXIAOKE_CDP_USER_DATA_DIR || `/private/tmp/fxiaoke-cdp-profile-${port}`);
const url = argValue("url", CRM_LOGIN_URL);
const headed = hasFlag("headed") || process.env.FXIAOKE_CDP_HEADED === "1";
const executable = chromeExecutable();

fs.mkdirSync(userDataDir, { recursive: true });

const chromeArgs = [
  `--remote-debugging-address=127.0.0.1`,
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${userDataDir}`,
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-background-networking",
  "--disable-default-apps",
  "--disable-sync",
  "--disable-features=Translate,OptimizationHints,MediaRouter",
  "--window-size=1440,1100",
];

if (!headed) {
  chromeArgs.push("--headless=new");
}

chromeArgs.push(url);

console.log(JSON.stringify({
  ok: true,
  mode: headed ? "headed" : "headless",
  cdp_url: `http://127.0.0.1:${port}`,
  user_data_dir: userDataDir,
  executable,
  url,
}, null, 2));

const child = spawn(executable, chromeArgs, { stdio: "inherit" });

function shutdown(signal) {
  if (!child.killed) child.kill(signal);
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
