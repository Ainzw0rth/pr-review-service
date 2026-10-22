// NODE_PATH=<installed Playwright node_modules> node --test core/tests/workflow_ui.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {mkdirSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require('playwright');
const root = join(__dirname, '../..');
// Render the real Django template and base. HTTP state alone is stubbed.
const html = execFileSync(join(root, '.venv/bin/python'), ['-c', `
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'reviewdesk.settings')
os.environ['DJANGO_SECRET_KEY'] = 'browser-test-only-not-for-deployment'
os.environ['DEBUG'] = '1'
import django
django.setup()
from django.template.loader import render_to_string
from types import SimpleNamespace
user = SimpleNamespace(is_authenticated=True, is_superuser=False, username='owner')
request = SimpleNamespace(resolver_match=SimpleNamespace(url_name='workflow'))
print(render_to_string('core/workflow.html', {'user':user, 'request':request, 'workflow_owner':True, 'csrf_token':'test-token'}))
`], {cwd:root, encoding:'utf8'});
const fixture = () => ({version:'123456789abcdef0', models:[
  {id:1,name:'Codex',kind:'codex_cli',model:'gpt-5.4',enabled:true,effort:'high'},
  {id:2,name:'Claude',kind:'claude_cli',model:'claude-opus-4-6',enabled:true,effort:''},
  {id:3,name:'Reserve',kind:'codex_cli',model:'gpt-5.4-mini',enabled:false,effort:''},
], agents:['code','security','tests'].map(key => ({key,name:{code:'Code',security:'Security',tests:'Tests'}[key],instructions:'',enabled:true})), probes:[]});
async function setup(browser) {
  const page = await browser.newPage({viewport:{width:1280,height:1000}});
  const errors = [], payloads = []; let data = fixture(), conflict = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {window.setInterval = fn => {window.workflowPoll = fn; return 1;};});
  await page.route('https://review.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/workflow/state/') {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON(); payloads.push(body);
        if (conflict) return route.fulfill({status:409,json:{error:'Workflow changed in another tab. Reload before saving.'}});
        data = {...body, version:'fedcba9876543210', probes:[], models:body.models.map((m,i) => ({...m,id:m.id || i + 10})), agents:body.agents.map((a,i) => ({...a,key:a.key || `agent_${String(i).padStart(12,'0')}`}))};
      }
      return route.fulfill({json:data});
    }
    if (path.startsWith('/workflow/probe/')) return route.fulfill({status:202,json:{message:'Connection test queued.'}});
    if (path.startsWith('/static/')) return route.fulfill({path:join(root,path),contentType:path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'font/ttf'});
    return route.fulfill({body:html,contentType:'text/html'});
  });
  await page.goto('https://review.test/workflow/');
  await page.locator('#workflow-editor:not([hidden])').waitFor();
  return {page, errors, payloads, conflict:() => {conflict = true;}};
}
const order = page => page.locator('#workflow-chain > .workflow-node').evaluateAll(nodes => nodes.map(n => n.dataset.modelKey));

test('whole pipeline, adjacent library, native drag and keyboard model ordering', async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const {page,errors,payloads} = await setup(browser);
    assert.equal(await page.locator('.workflow-stage').count(),7);
    assert.equal(await page.locator('.workflow-branch').count(),3);
    const shelf = await page.locator('.workflow-shelf').boundingBox(), pipeline = await page.locator('.workflow-pipeline').boundingBox();
    assert.ok(shelf.x + shelf.width < pipeline.x);
    assert.ok(Math.abs(shelf.y - pipeline.y) < 5);
    await page.getByRole('button',{name:'Move Claude up',exact:true}).focus();
    await page.keyboard.press('Enter');
    assert.deepEqual(await order(page),['model-2','model-1']);
    assert.equal(await page.locator('#workflow-chain .workflow-model-edit').first().evaluate(n => n === document.activeElement),true);
    // Actual native browser drag from the adjacent library into the first slot.
    await page.locator('#model-library [data-model-key="model-3"]').dragTo(page.locator('#workflow-chain'),{sourcePosition:{x:20,y:44},targetPosition:{x:40,y:12}});
    assert.deepEqual(await order(page),['model-3','model-2','model-1']);
    // Reorder inside the chain while the page is scrolled.
    await page.locator('#workflow-chain [data-model-key="model-1"] .workflow-drag-handle').dragTo(page.locator('#workflow-chain'),{targetPosition:{x:45,y:12}});
    assert.deepEqual(await order(page),['model-1','model-3','model-2']);
    await page.getByRole('button',{name:'Remove Reserve from order',exact:true}).click();
    await page.getByRole('button',{name:'Remove Claude from order',exact:true}).click();
    await page.getByRole('button',{name:'Remove Codex from order',exact:true}).click();
    assert.deepEqual(await order(page),['model-1']);
    assert.match(await page.locator('#workflow-status').innerText(),/at least one model/);
    await page.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.getByText('Saved · new jobs will use this workflow.',{exact:true}).waitFor();
    assert.deepEqual(payloads[0].models.filter(m => m.enabled).map(m => m.id),[1]);
    assert.ok(payloads[0].models.every(m => !('uiKey' in m)));
    await page.getByRole('button',{name:'Configure Codex',exact:true}).click();
    await page.getByRole('button',{name:'Test saved connection',exact:true}).click();
    await page.getByText('Connection test queued.',{exact:true}).waitFor();
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

test('reviewer toggles, custom draft retention, literal text and stale-save protection', async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const {page,errors,payloads,conflict} = await setup(browser);
    await page.getByRole('tab',{name:'Models',exact:true}).focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.getByRole('tab',{name:'Reviewers',exact:true}).getAttribute('aria-selected'),'true');
    await page.getByRole('button',{name:'Disable Security',exact:true}).click();
    assert.equal(await page.locator('.workflow-branch').count(),2);
    await page.getByRole('button',{name:'Enable Security',exact:true}).click();
    assert.deepEqual(await page.locator('.workflow-branch').evaluateAll(nodes => nodes.map(n => n.dataset.agentKey)),['code','security','tests']);
    await page.getByRole('button',{name:'Disable Security',exact:true}).click();
    await page.getByRole('button',{name:'Disable Tests',exact:true}).click();
    await page.getByRole('button',{name:'Disable Code',exact:true}).click();
    assert.equal(await page.locator('.workflow-branch').count(),1);
    assert.match(await page.locator('#workflow-status').innerText(),/at least one reviewer/);
    await page.getByRole('button',{name:'+ Custom reviewer',exact:true}).click();
    const name = 'Architecture <img src=x>';
    await page.locator('#reviewer-name').fill(name);
    await page.locator('#reviewer-instructions').fill('Check boundaries and compatibility. <script>alert(1)</script>');
    assert.equal(await page.locator('.workflow-branch').count(),2);
    assert.equal(await page.locator('.workflow-branch img').count(),0);
    await page.getByRole('button',{name:'Done',exact:true}).click();
    await page.getByRole('button',{name:`Configure ${name}`,exact:true}).click();
    assert.equal(await page.locator('#reviewer-instructions').inputValue(),'Check boundaries and compatibility. <script>alert(1)</script>');
    await page.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.getByText('Saved · new jobs will use this workflow.',{exact:true}).waitFor();
    assert.equal(payloads[0].agents.length,4);
    assert.equal(payloads[0].agents[3].key,null);
    assert.equal(payloads[0].agents[1].enabled,false);
    assert.match(await page.locator('#reviewer-library').innerText(),/Architecture/);
    // Editing after save keeps the server-assigned opaque key, even after closing the inspector.
    await page.locator('#reviewer-name').fill('Architecture revised');
    await page.getByRole('button',{name:'Done',exact:true}).click();
    await page.getByRole('button',{name:'Configure Architecture revised',exact:true}).click();
    conflict();
    await page.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.getByText(/Draft preserved/).waitFor();
    assert.equal(await page.locator('#reviewer-name').inputValue(),'Architecture revised');
    assert.equal(await page.getByRole('button',{name:'Save changes',exact:true}).isEnabled(),true);
    assert.match(payloads[1].agents[3].key,/^agent_[0-9a-f]{12}$/);
    await page.evaluate(() => window.workflowPoll());
    assert.equal(await page.locator('#reviewer-name').inputValue(),'Architecture revised');
    await page.getByRole('button',{name:'Remove from library',exact:true}).click();
    assert.equal(await page.locator('.workflow-branch').count(),1);
    assert.equal(await page.locator('#reviewer-library > div').count(),3);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

test('new model drafts, responsive paper layout and reduced motion', async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const {page,errors,payloads} = await setup(browser);
    await page.getByRole('button',{name:'+ New CLI model',exact:true}).click();
    await page.locator('#model-name').fill('Local fallback');
    await page.locator('#model-id').fill('gpt-5.4');
    await page.locator('#model-effort').selectOption('xhigh');
    await page.getByRole('button',{name:'Done',exact:true}).click();
    await page.getByRole('button',{name:'Edit Local fallback',exact:true}).click();
    assert.equal(await page.locator('#model-effort').inputValue(),'xhigh');
    await page.getByRole('button',{name:'Add Local fallback',exact:true}).click();
    await page.getByRole('button',{name:'Save changes',exact:true}).click();
    await page.getByText('Saved · new jobs will use this workflow.',{exact:true}).waitFor();
    assert.equal(payloads[0].models.at(-1).effort,'xhigh');
    assert.equal(await page.locator('#model-kind').isDisabled(),true);
    assert.equal(await page.locator('#model-discard').isVisible(),false);
    await page.getByRole('button',{name:'Done',exact:true}).click();
    const screens = join(root,'.claude/verification/workflow-ui'); mkdirSync(screens,{recursive:true});
    await page.screenshot({path:join(screens,'desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.getByRole('button',{name:'Move Local fallback up',exact:true}).click();
    assert.equal(await page.locator('.workflow-node').first().evaluate(n => n.getAnimations().length),0);
    assert.equal(await page.locator('.workflow-branch').first().evaluate(n => getComputedStyle(n).animationName),'none');
    assert.equal(await page.locator('.workflow-branch').first().evaluate(n => getComputedStyle(n).transitionDuration),'0s');
    await page.getByRole('tab',{name:'Reviewers',exact:true}).click();
    await page.getByRole('button',{name:'Disable Security',exact:true}).click();
    assert.equal(await page.locator('.workflow-branch').count(),2);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.screenshot({path:join(screens,'mobile.png'),fullPage:true});
    await page.getByRole('button',{name:'+ Custom reviewer',exact:true}).click();
    await page.locator('#reviewer-name').fill('Architecture'.repeat(6));
    await page.locator('#reviewer-instructions').fill('Check compatibility.');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.getByRole('button',{name:'Done',exact:true}).click();
    await page.getByRole('button',{name:'Configure Code',exact:true}).click();
    assert.equal(await page.locator('#reviewer-instructions').isVisible(),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});
