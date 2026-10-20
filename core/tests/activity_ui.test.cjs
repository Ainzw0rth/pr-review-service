// Browser regression: NODE_PATH=<installed Playwright node_modules> node --test core/tests/activity_ui.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {mkdirSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require('playwright');

const root = join(__dirname, '../..');
const start = new Date(Date.now() - 30000).toISOString();
const end = new Date(Date.parse(start) + 12000).toISOString();
const stage = (id, title, state = 'pending') => ({id, title, state, detail: 'Recorded evidence', started_at: state === 'pending' ? null : start, finished_at: state === 'done' ? end : null});
function fixture() {
  return {
    status: 'running', status_label: 'Running', provider: 'Test API', heartbeat_at: start,
    reason: '', started_at: start, finished_at: null, next_retry_at: null,
    flow: {
      legacy: false, model_detail: 'API reviews overlap; subscription CLI sessions run sequentially.',
      stages: [stage('files', 'Changed files', 'done'), stage('discussion', 'Discussions', 'done'), stage('docs', 'Documentation', 'done'), stage('graph', 'Dependency graph', 'done'), stage('intent', 'Grounded intent', 'done'), stage('checks', 'Required CI', 'done'), stage('tests', 'Isolated tests', 'done'), stage('code_review', 'Code reviewer', 'done'), stage('security_review', 'Security reviewer', 'running'), stage('test_review', 'Test reviewer'), stage('synthesis', 'Review adjudication'), stage('pr_summary', 'PR summary')],
      models: [
        {title: 'Old CLI', model: 'old-model', backend: 'codex_cli', state: 'failed', attempts: [{id: 1, state: 'failed', phase: 'intent', started_at: '2026-10-19T09:00:00Z', finished_at: '2026-10-19T09:00:05Z', failure_reason: 'Old recorded failure', next_action: 'Continued with Test API.'}]},
        {title: 'Test API', model: 'current-model', backend: 'openai', state: 'running', attempts: [{id: 2, state: 'running', phase: 'specialists', started_at: start, finished_at: null, usage: {session_count: 1, totals: {input_tokens: 100, output_tokens: 10}, passes: [{phase: 'code', recovery_pass: 0, input_tokens: 100, output_tokens: 10}]}}]},
      ],
    },
    events: [
      {id: 1, at: end, kind: 'cli_output_text', source: 'Test API / code', message: 'Code phase public progress'},
      {id: 2, at: end, kind: 'cli_output_text', source: 'Test API / security', message: 'Security phase public progress'},
    ],
  };
}
const shell = `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/core/desk.css"><link rel="stylesheet" href="/static/core/motion.css"></head><body><main><section class="activity-panel" data-activity-url="/activity/"><div class="activity-heading"><h2>Review progress</h2><button class="activity-pause" aria-pressed="false">Pause updates</button></div><p class="activity-connection"></p><p class="activity-heartbeat"></p><div class="review-flow"></div><p class="flow-legacy" hidden>Historical attempt</p><details class="disclosure activity-tools"><summary>Activity log</summary><button data-terminal-follow>Following ↓</button><button data-terminal-mode="cli">CLI output</button><button data-terminal-mode="worker">Worker events</button><ol class="activity-log"></ol></details></section><section data-job-note hidden><p></p></section></main><script src="/static/core/activity.js"></script></body></html>`;

test('progress cards retain accessible disclosures, reviewer attribution and history across polling', async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    let data = fixture();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {window.setInterval = callback => {window.activityPoll = callback; return 1;};});
    await page.route('https://review.test/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/activity/') return route.fulfill({json: data});
      if (path.startsWith('/static/')) return route.fulfill({path: join(root, path), contentType: path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'font/ttf'});
      return route.fulfill({body: shell, contentType: 'text/html'});
    });
    await page.goto('https://review.test/');
    await page.locator('[data-progress-id="review"] > summary').waitFor();
    assert.equal(await page.locator('details[data-progress-id]').count(), 4);
    assert.equal(await page.locator('details[data-progress-id][open]').count(), 0);
    assert.equal(await page.getByText('Technical details', {exact: true}).count(), 0);
    assert.equal(await page.getByText('Review team', {exact: true}).count(), 0);

    const review = page.locator('details[data-progress-id="review"]');
    await review.locator(':scope > summary').focus();
    await review.locator(':scope > summary').press('Enter');
    assert.equal(await page.locator('.review-orchestrator').count(), 1);
    assert.equal(await page.locator('.review-session').count(), 4);
    assert.equal(await page.locator('.review-sessions > li').count(), 3);
    assert.equal(await page.locator('.review-sessions [data-phase="synthesis"]').count(), 0);
    assert.equal(await page.locator('.review-adjudication > [data-phase="synthesis"]').count(), 1);
    assert.match(await page.locator('.review-orchestrator').innerText(), /Service-managed/);
    const adjudication = page.locator('.review-adjudication > .review-session');
    await adjudication.locator(':scope > summary').click();
    await adjudication.locator(':scope > summary').focus();
    await page.evaluate(() => window.activityPoll());
    assert.equal(await adjudication.getAttribute('open'), '');
    assert.equal(await adjudication.locator(':scope > summary').evaluate(node => node === document.activeElement), true);
    await adjudication.locator(':scope > summary').click();
    const code = page.locator('.review-session[data-phase="code"]');
    await code.locator(':scope > summary').click();
    assert.match(await code.innerText(), /current-model/);
    assert.match(await code.innerText(), /12s/);
    assert.match(await code.innerText(), /100/);
    assert.match(await code.innerText(), /Code phase public progress/);
    const codeMarker = await code.locator('.node-state').evaluate(node => getComputedStyle(node,'::before').content);
    assert.match(codeMarker, /✓/); // A running ancestor must not turn completed children into running dots.
    assert.doesNotMatch(await code.innerText(), /Security phase public progress/);
    await code.locator(':scope > summary').focus();
    data.flow.stages.find(row => row.id === 'security_review').state = 'done';
    data.flow.stages.find(row => row.id === 'test_review').state = 'running';
    await page.evaluate(() => window.activityPoll());
    assert.equal(await review.getAttribute('open'), '');
    assert.equal(await code.getAttribute('open'), '');
    assert.equal(await code.locator(':scope > summary').evaluate(node => node === document.activeElement), true);
    assert.equal(await page.locator('.review-session[data-phase="tests"]').getAttribute('data-state'), 'running');

    // Moving to a fallback attempt must not collapse the current phase or lose keyboard focus.
    data.flow.models[1].attempts[0].id = 3;
    await page.evaluate(() => window.activityPoll());
    assert.equal(await code.getAttribute('open'), '');
    assert.equal(await code.locator(':scope > summary').evaluate(node => node === document.activeElement), true);

    data.flow.models[0].attempts[0].phase = 'code';
    data.flow.models[0].attempts[0].phases = [{phase:'intent',state:'done',started_at:'2026-10-19T09:00:00Z',finished_at:'2026-10-19T09:00:01Z'},{phase:'code',state:'skipped',started_at:'2026-10-19T09:00:01Z',finished_at:'2026-10-19T09:00:05Z'},...['security','tests','synthesis'].map(phase => ({phase,state:'skipped',started_at:null,finished_at:null}))];
    // Per-attempt records override global stages; old sessions must not inherit the latest run.
    data.flow.models[1].attempts[0].phases = [{phase:'code',state:'done',started_at:start,finished_at:end},{phase:'security',state:'done',started_at:start,finished_at:end},{phase:'tests',state:'running',started_at:end,finished_at:null}];
    await page.evaluate(() => window.activityPoll());
    const history = page.locator('[data-flow-key="model-history"]');
    await history.locator(':scope > summary').click();
    assert.match(await history.innerText(), /Old recorded failure/);
    assert.match(await history.innerText(), /Continued with Test API/);
    const oldPhases = history.locator('[data-flow-key="attempt-phases-1"]');
    await oldPhases.locator(':scope > summary').click();
    assert.equal(await oldPhases.locator('[data-phase="code"]').getAttribute('data-state'), 'skipped');
    assert.match(await oldPhases.locator('[data-phase="code"] > summary').innerText(), /Skipped/);
    assert.doesNotMatch(await oldPhases.locator('[data-phase="code"] > summary').innerText(), /quota/);
    assert.equal(await oldPhases.locator('[data-phase="security"]').getAttribute('data-state'), 'skipped');
    const currentPhases = review.locator(':scope > .progress-content > .review-tree');
    assert.equal(await currentPhases.locator('[data-phase="tests"]').getAttribute('data-state'), 'running');
    assert.match(await oldPhases.locator('[data-phase="code"]').innerText(), /4s/);
    const oldCode = oldPhases.locator('[data-phase="code"] > summary');
    await oldCode.click();
    assert.match(await oldPhases.locator('[data-phase="code"]').innerText(), /No review was delivered/);
    await oldCode.focus();
    await page.evaluate(() => window.activityPoll());
    assert.equal(await oldCode.evaluate(node => node === document.activeElement), true);
    assert.equal(await oldPhases.locator('[data-phase="code"]').getAttribute('open'), '');
    data.flow.model_detail = 'Saved analysis reused for delivery; no additional model call.';
    data.delivery_retry = true;
    await page.evaluate(() => window.activityPoll());
    assert.match(await review.innerText(), /Saved analysis reused for delivery; no additional model call/);
    assert.equal(await history.getAttribute('open'), '');

    const codePhase = data.flow.models[1].attempts[0].phases.find(row => row.phase === 'code');
    codePhase.coverage_complete = false;
    await page.evaluate(() => window.activityPoll());
    assert.match(await currentPhases.locator('[data-phase="code"] > summary').innerText(), /Complete/);
    assert.doesNotMatch(await currentPhases.locator('[data-phase="code"] > summary').innerText(), /Limited review/);
    assert.doesNotMatch(await currentPhases.locator('[data-phase="code"] > summary').innerText(), /Incomplete evidence/);
    assert.match(await currentPhases.locator('[data-phase="code"]').innerText(), /Coverage caveats/);

    // Delivery status is independent of the assessment's coverage confidence.
    data.status = 'completed';
    data.status_label = 'Completed';
    data.flow.models[1].state = 'success';
    data.flow.models[1].attempts[0].state = 'success';
    data.flow.models[1].attempts[0].finished_at = end;
    data.flow.models[1].attempts[0].phases = ['code','security','tests','synthesis'].map(phase => ({phase,state:'done',coverage_complete:phase === 'tests',started_at:start,finished_at:end}));
    await page.evaluate(() => window.activityPoll());
    assert.equal(await review.getAttribute('data-state'), 'done');
    assert.match(await review.locator(':scope > summary').innerText(), /Complete/);
    assert.equal(await currentPhases.locator('.review-session.node-done').count(), 4);
    assert.match(await currentPhases.locator('.review-orchestrator .node-state').innerText(), /Complete/);
    for (const width of [1280, 390]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.ACTIVITY_SCREENSHOT_DIR) {
        mkdirSync(process.env.ACTIVITY_SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: join(process.env.ACTIVITY_SCREENSHOT_DIR, `review-complete-${width}.png`), fullPage: true});
      }
    }

    // Usage with missing historical timing stays unknown, never complete or running.
    data.status = 'completed';
    data.flow.models[1].attempts[0].state = 'success';
    data.flow.models[1].attempts[0].phases = [{phase:'code',state:'unknown',started_at:null,finished_at:null},{phase:'security',state:'done',started_at:start,finished_at:end},{phase:'tests',state:'done',started_at:start,finished_at:end},{phase:'synthesis',state:'done',started_at:start,finished_at:end}];
    await page.evaluate(() => window.activityPoll());
    const unknownCode = currentPhases.locator('[data-phase="code"]');
    assert.equal(await unknownCode.getAttribute('data-state'), 'unknown');
    assert.match(await unknownCode.locator(':scope > summary').innerText(), /Not recorded/);
    assert.match(await unknownCode.locator('.session-elapsed').innerText(), /Timing not recorded/);
    assert.equal(await review.getAttribute('data-state'), 'unknown');

    // Old workflows retain their original model attempts without invented specialists.
    data.flow.stages = data.flow.stages.filter(row => !['intent', 'code_review', 'security_review', 'test_review', 'synthesis'].includes(row.id));
    data.flow.legacy = true;
    data.flow.models = [{title: 'Legacy model', model: 'legacy', state: 'success', attempts: []}];
    await page.evaluate(() => window.activityPoll());
    assert.equal(await page.locator('.review-session').count(), 0);
    assert.match(await review.innerText(), /Legacy model/);
    assert.equal(await page.locator('.flow-legacy').isVisible(), true);

    data = fixture();
    await page.evaluate(() => window.activityPoll());
    // Each card owns its content; multiple disclosures can stay open independently.
    for (const [id, expected] of [['evidence', /Changed files[\s\S]*Discussions[\s\S]*Documentation[\s\S]*Dependency graph[\s\S]*Grounded intent/], ['checks', /Required CI[\s\S]*Isolated tests/], ['delivery', /PR summary/]]) {
      const card = page.locator(`details[data-progress-id="${id}"]`);
      await card.locator(':scope > summary').click();
      assert.match(await card.innerText(), expected);
      assert.equal(await review.getAttribute('open'), '');
      await card.locator(':scope > summary').click();
    }
    await page.locator(':scope .progress-content > .review-tree .review-session[data-phase="code"] > summary').click();
    for (const width of [1280, 390]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      const dependencyOrder = await currentPhases.evaluate(tree => {
        const adjudication = tree.querySelector('.review-adjudication').getBoundingClientRect();
        const reviewers = [...tree.querySelectorAll('.review-sessions > li > .review-session')].map(node => node.getBoundingClientRect());
        return adjudication.top > Math.max(...reviewers.map(box => box.bottom));
      });
      assert.equal(dependencyOrder, true, `Adjudication must follow all three reviewers at ${width}px`);
      if (process.env.ACTIVITY_SCREENSHOT_DIR) {
        mkdirSync(process.env.ACTIVITY_SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: join(process.env.ACTIVITY_SCREENSHOT_DIR, `review-cards-${width}.png`), fullPage: true});
      }
    }
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.equal(await review.locator(':scope > summary').evaluate(node => getComputedStyle(node).transitionDuration), '0s');
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
