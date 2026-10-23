import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {prepareSession} from './sessions.mjs';

test('history survives a new private home; missing IDs start fresh, never resume another session',async t=>{
  const root=await mkdtemp(join(tmpdir(),'review-sessions-'));
  t.after(()=>rm(root,{recursive:true,force:true}));
  for(const kind of ['codex_cli','claude_cli']) {
    const key=randomUUID(), id=randomUUID();
    const home=join(root,kind,'one'), nextHome=join(root,kind,'two');
    const options={root:join(root,'persistent'),home};
    assert.equal(await prepareSession(kind,{key,id},options),null);
    const relative=kind==='codex_cli'?`sessions/2026/10/23/rollout-test-${id}.jsonl`:`projects/-tmp-review/${id}.jsonl`;
    await mkdir(join(home,relative,'..'),{recursive:true});
    await writeFile(join(home,relative),'retained history');
    await rm(home,{recursive:true,force:true});
    assert.equal(await prepareSession(kind,{key,id},{...options,home:nextHome}),id);
    assert.equal(await readFile(join(nextHome,relative),'utf8'),'retained history');
    assert.equal(await prepareSession(kind,{key,id:randomUUID()},{...options,home:join(root,kind,'missing')}),null);
    assert.equal(await prepareSession(kind,{key:randomUUID(),id},{...options,home:join(root,kind,'isolated')}),null);
  }
});

test('invalid scope and session IDs cannot become filesystem paths',async()=>{
  for(const session of [{key:'../other',id:null},{key:randomUUID(),id:'../../other'},{key:randomUUID(),id:[]}]) {
    await assert.rejects(prepareSession('codex_cli',session),/invalid_session/);
  }
});
