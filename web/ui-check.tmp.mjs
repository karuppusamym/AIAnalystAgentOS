import { chromium } from '@playwright/test';

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1918, height: 991 }, deviceScaleFactor: 1 });
const root = 'http://127.0.0.1:5174';
const workspace = 'ws_272b2ca5401b';
await page.goto(`${root}/w/${workspace}/outputs`);
await page.waitForTimeout(1000);
if (await page.getByRole('heading', { name: 'Sign in' }).isVisible().catch(() => false)) {
  await page.getByLabel('Email').fill('admin@analystos.local');
  await page.getByLabel('Password').fill('ChangeMe123!');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.waitForTimeout(1000);
  console.log('post login', page.url(), (await page.locator('body').innerText()).slice(0, 400));
}
for (const [name, path] of [
  ['outputs', `/w/${workspace}/outputs`],
  ['ask', `/w/${workspace}/work/ask?thread=ask_99da43d7ccd4`],
  ['registry', '/settings/capabilities'],
  ['settings', '/settings/platform'],
]) {
  await page.goto(`${root}${path}`);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `ui-${name}.tmp.png`, fullPage: false });
  console.log(name, page.url(), await page.locator('body').evaluate(el => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth })));
}
await browser.close();
