/* Record the disposable local demo. See docs/demo.md for tooling and regeneration. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'var/product-demo');
const media = path.join(root, 'docs/media');
const base = 'http://127.0.0.1:18041';
fs.mkdirSync(output, { recursive: true });
fs.mkdirSync(media, { recursive: true });

(async () => {
  for (let attempt = 0; ; attempt++) {
    try { if ((await fetch(base + '/healthz/')).ok) break; } catch {}
    if (attempt >= 39) throw new Error('Start scripts/demo.py before recording.');
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  const browser = await chromium.launch({ headless: true, args: process.getuid?.() === 0 ? ['--no-sandbox'] : [] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  const localOnly = route => new URL(route.request().url()).origin === base ? route.continue() : route.abort();
  await context.route('**/*', localOnly);
  const login = await context.newPage();
  await login.goto(base + '/login/');
  await login.getByLabel('Username:').fill('demo');
  await login.getByLabel('Password:').fill('demo-review-only');
  await login.getByRole('button', { name: 'Enter workspace' }).click();
  await login.waitForURL(base + '/');
  if (!(await login.locator('main').innerText()).includes('northstar-demo/payments-api')) throw new Error('Not the fictional demo database.');
  const state = await context.storageState();
  await context.close();

  const recording = await browser.newContext({ storageState: state, viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1, recordVideo: { dir: output, size: { width: 1440, height: 1000 } } });
  await recording.route('**/*', localOnly);
  const page = await recording.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const pause = ms => page.waitForTimeout(ms);
  async function chapter(number, title, subtitle) {
    await page.evaluate(({ number, title, subtitle }) => {
      document.getElementById('demo-caption')?.remove();
      const caption = document.createElement('aside');
      caption.id = 'demo-caption';
      caption.style.cssText = 'position:fixed;z-index:9999;bottom:20px;left:24px;right:24px;display:flex;gap:20px;align-items:center;background:#222220;color:#f6f4ee;padding:18px 24px;border-radius:10px;box-shadow:0 6px 28px #22222020;font:12px "Martian Mono",monospace;pointer-events:none';
      const label = document.createElement('strong'); label.textContent = number + ' / ' + title;
      const detail = document.createElement('span'); detail.textContent = subtitle; detail.style.cssText = 'color:#d2cec3;font-size:10px';
      const demo = document.createElement('span'); demo.textContent = 'FICTIONAL DEMO'; demo.style.cssText = 'margin-left:auto;white-space:nowrap;color:#bab6ab;font-size:9px;letter-spacing:1px';
      caption.append(label, detail, demo); document.body.append(caption);
    }, { number, title, subtitle });
  }
  async function click(locator) {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 20 });
    await pause(300); await locator.click();
  }
  async function still(name) {
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(media, name), animations: 'disabled' });
  }
  await page.goto(base + '/');
  await page.evaluate(() => document.fonts.ready);
  await chapter('01', 'THE REVIEW QUEUE', 'See what is running, waiting, and complete.');
  await pause(2000); await still('queue.png'); await pause(2500);
  await click(page.getByLabel('Status', { exact: true }));
  await page.getByLabel('Status', { exact: true }).selectOption('completed');
  await page.waitForFunction(() => document.querySelectorAll('.queue-table tbody tr').length === 4);
  await pause(2200);
  await page.getByLabel('Status', { exact: true }).selectOption('');
  await page.waitForFunction(() => document.querySelectorAll('.queue-table tbody tr').length === 8);
  await click(page.locator('.repo').first());
  await page.locator('[data-flow-key="progress-review"] > summary').waitFor();
  await chapter('02', 'FOLLOW THE REVIEW', 'Evidence, checks, specialist reviews, then GitHub delivery.');
  await pause(1800);
  await click(page.locator('[data-flow-key="progress-review"] > summary'));
  await pause(800); await page.evaluate(() => window.scrollBy({ top: 260, behavior: 'smooth' }));
  await pause(1200); await still('review-progress.png'); await pause(2800);
  await click(page.locator('.activity-tools > summary'));
  await page.locator('.activity-tools').scrollIntoViewIfNeeded();
  await chapter('02', 'PUBLIC PROGRESS', 'Follow operational updates without exposing private reasoning.');
  await pause(3000);
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' })); await pause(600);
  await click(page.getByRole('link', { name: 'Settings', exact: true }));
  await chapter('03', 'YOUR REPOSITORIES', 'Add repositories. Pause monitoring. Set review capacity.');
  await pause(1500); await still('settings.png'); await pause(2200);
  await click(page.locator('.settings-policy > summary').first());
  await pause(2400);
  await click(page.getByRole('link', { name: 'Add repository', exact: true }));
  await chapter('04', 'CONNECT ANOTHER REPOSITORY', 'Configure it in Settings—not in source code.');
  await page.getByLabel('Repository', { exact: true }).fill('northstar-demo/mobile-app');
  await pause(2000);
  await page.getByLabel('GitHub reviewer').fill('review-bot-demo');
  await pause(2500); // Do not save: this is a visual tour, not a GitHub integration test.
  await page.goto(base + '/');
  await chapter('START HERE', 'python3 scripts/setup.py', 'Self-hosted. Your models. Your repositories.');
  await pause(3000);
  await recording.close();
  await page.video().saveAs(path.join(output, 'walkthrough.webm'));

  const mobile = await browser.newContext({ storageState: state, viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
  await mobile.route('**/*', localOnly);
  const phone = await mobile.newPage(); await phone.goto(base + '/settings/');
  if (await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Mobile Settings overflow');
  await mobile.close(); await browser.close();
  if (errors.length) throw new Error(errors.join('\n'));
  console.log('Captured queue, progress, Settings, and video. Mobile Settings: no overflow. No browser exceptions.');
})().catch(error => { console.error(error); process.exit(1); });
