const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

(async () => {
  const htmlPath = path.resolve(__dirname, '../docs/mermaid-chart.html');
  const outPath = path.resolve(__dirname, '../docs/workflow-flowchart.png');

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 2400 } });

  // 加载页面，等待 Mermaid 渲染完成
  await page.goto('file://' + htmlPath, { waitUntil: 'networkidle', timeout: 30000 });

  // 等待 mermaid 渲染完成
  await page.waitForSelector('.mermaid svg', { timeout: 15000 }).catch(() => {});

  // 等 SVG 完全渲染
  await new Promise(r => setTimeout(r, 3000));

  // 获取 mermaid 容器的边界框
  const box = await page.locator('.mermaid').boundingBox();
  if (box) {
    await page.screenshot({ path: outPath, clip: { x: box.x, y: box.y, width: box.width, height: box.height } });
    console.log('OK: ' + outPath);
  } else {
    // 截全屏
    await page.screenshot({ path: outPath, fullPage: true });
    console.log('OK (fullpage): ' + outPath);
  }

  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
