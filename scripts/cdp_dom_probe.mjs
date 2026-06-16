import { chromium } from "playwright";

const browser = await chromium.connectOverCDP(process.env.CDP_URL || "http://127.0.0.1:9333");
try {
  const page = browser.contexts()[0]?.pages()[0] || (await browser.newPage());
  const result = await page.evaluate(() => ({
      title: document.title,
      url: location.href,
      text: document.body.innerText.slice(0, 5000)
    }));
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
