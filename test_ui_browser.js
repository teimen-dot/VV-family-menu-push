#!/usr/bin/env node
const { chromium } = require('playwright');

const BASE = process.env.UI_BASE_URL || 'http://127.0.0.1:8090';
const viewports = [320, 375, 390, 430, 768, 1024, 1440];

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  const failures = [];
  for (const user of ['vivian', 'kitchen']) {
    for (const width of viewports) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        extraHTTPHeaders: { 'X-Authenticated-User': user },
      });
      const page = await context.newPage();
      const errors = [];
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
      page.on('pageerror', error => errors.push(error.message));
      const route = user === 'kitchen' ? '/pantry' : '/tomorrow';
      const response = await page.goto(BASE + route, { waitUntil: 'networkidle' });
      const result = await page.evaluate(() => ({
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        role: document.body.dataset.role,
        heading: document.querySelector('h1')?.textContent?.trim(),
        ownerControls: [...document.querySelectorAll('[data-action="confirm"], [data-action="fill"], [data-action="replace"]')].filter(x => !x.hidden).length,
        pantryButtons: [...document.querySelectorAll('.stock-actions')].slice(0, 3).map(row => row.querySelectorAll('button').length),
        brokenImages: [...document.images].filter(img => img.complete && img.naturalWidth === 0).length,
        historicalCombos: [...document.querySelectorAll('.dish-card h3')].filter(x => x.textContent.includes('＋')).length,
        mobileActions: [...document.querySelectorAll('.mobile-action-bar [data-action]')].filter(x => getComputedStyle(x).display !== 'none').length,
      }));
      if (!response || response.status() !== 200) failures.push(`${user} ${width}: HTTP ${response?.status()}`);
      if (result.overflow > 1) failures.push(`${user} ${width}: overflow ${result.overflow}px`);
      if (result.role !== (user === 'vivian' ? 'owner' : 'worker')) failures.push(`${user} ${width}: wrong role ${result.role}`);
      if (user === 'kitchen' && result.ownerControls) failures.push(`${user} ${width}: ${result.ownerControls} owner controls visible`);
      if (user === 'kitchen' && result.pantryButtons.some(count => count !== 3)) failures.push(`${user} ${width}: pantry controls incomplete`);
      if (result.brokenImages) failures.push(`${user} ${width}: ${result.brokenImages} broken images`);
      if (user === 'vivian' && result.historicalCombos) failures.push(`${user} ${width}: historical combo was not split`);
      if (user === 'vivian' && width <= 700 && result.mobileActions !== 2) failures.push(`${user} ${width}: mobile action bar incomplete`);
      if (errors.length) failures.push(`${user} ${width}: console ${errors.join(' | ')}`);
      if ([320, 390, 1440].includes(width)) await page.screenshot({ path: `/private/tmp/c222-${user}-${width}.png`, fullPage: true });
      console.log(JSON.stringify({ user, width, ...result }));
      await context.close();
    }
  }
  await browser.close();
  if (failures.length) {
    console.error(failures.join('\n'));
    process.exit(1);
  }
})().catch(error => { console.error(error); process.exit(1); });
