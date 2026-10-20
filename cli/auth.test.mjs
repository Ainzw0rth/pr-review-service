import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {createCodexAuth} from './auth.mjs';

const jwt=sub=>'header.'+Buffer.from(JSON.stringify({sub})).toString('base64url')+'.signature';
const credentials=(refresh='initial',access='access',sub='user')=>({auth_mode:'chatgpt',OPENAI_API_KEY:null,tokens:{id_token:jwt(sub),account_id:'account',access_token:access,refresh_token:refresh}});
async function fixture(t,respond){
  const root=await mkdtemp(join(tmpdir(),'review-auth-'));
  t.after(()=>rm(root,{recursive:true,force:true}));
  const shared=join(root,'auth.json');
  await writeFile(shared,JSON.stringify(credentials()),{mode:0o600});
  const upstream=createServer(respond);
  await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>upstream.close(resolve)));
  const make=async name=>{
    const auth=await createCodexAuth({sharedHome:root,privateHome:join(root,name),refreshEndpoint:`http://127.0.0.1:${upstream.address().port}/token`});
    t.after(()=>auth.close());
    return auth;
  };
  return {root,shared,make};
}
async function refresh(auth,token='initial'){
  return fetch(auth.refreshUrl,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',client_id:'app_EMoamEEZ73f0CkXaXp7hrann',refresh_token:token})});
}

test('concurrent sessions exchange a rotated refresh token once and never share native writes',async t=>{
  let exchanges=0;
  const f=await fixture(t,async(req,res)=>{
    exchanges++;
    let raw='';for await(const chunk of req)raw+=chunk;
    assert.equal(JSON.parse(raw).refresh_token,'initial');
    await new Promise(resolve=>setTimeout(resolve,50));
    res.setHeader('content-type','application/json');
    res.end(JSON.stringify({access_token:'new-access',refresh_token:'rotated',id_token:jwt('user')}));
  });
  const [one,two]=await Promise.all([f.make('one'),f.make('two')]);
  assert.notEqual(one.home,two.home);
  const responses=await Promise.all([refresh(one),refresh(two)]);
  for(const response of responses){assert.equal(response.status,200);assert.equal((await response.json()).refresh_token,'rotated');}
  assert.equal(exchanges,1);
  assert.equal(JSON.parse(await readFile(f.shared,'utf8')).tokens.refresh_token,'rotated');
  await writeFile(join(one.home,'auth.json'),JSON.stringify(credentials('stale-local-write')));
  assert.equal(JSON.parse(await readFile(f.shared,'utf8')).tokens.refresh_token,'rotated');
  const late=await refresh(one);
  assert.equal((await late.json()).refresh_token,'rotated');
  assert.equal(exchanges,1);
});

test('account switches and unknown refresh tokens fail closed without upstream requests',async t=>{
  let exchanges=0;
  const f=await fixture(t,(req,res)=>{exchanges++;res.end('{}');});
  const auth=await f.make('one');
  assert.equal((await refresh(auth,'unrecognized')).status,401);
  await writeFile(f.shared,JSON.stringify(credentials('other','other-access','other-user')));
  assert.equal((await refresh(auth)).status,401);
  assert.equal(exchanges,0);
  assert.equal(JSON.parse(await readFile(f.shared,'utf8')).tokens.refresh_token,'other');
});

test('failed or malformed refresh never alters stored credentials or leaks provider text',async t=>{
  let mode='rejected';
  const f=await fixture(t,(req,res)=>{
    res.statusCode=mode==='rejected'?401:200;
    res.end(mode==='rejected'?'private-provider-token':JSON.stringify({access_token:'new',id_token:jwt('wrong-account'),refresh_token:'changed'}));
  });
  const auth=await f.make('one');
  let response=await refresh(auth);
  assert.equal(response.status,401);
  assert.doesNotMatch(await response.text(),/private-provider-token/);
  mode='mismatch';
  response=await refresh(auth);
  assert.equal(response.status,401);
  assert.equal(JSON.parse(await readFile(f.shared,'utf8')).tokens.refresh_token,'initial');
});

test('refresh bridge rejects malformed, oversized and non-POST requests',async t=>{
  const f=await fixture(t,()=>assert.fail('invalid request reached upstream'));
  const auth=await f.make('one');
  assert.equal((await fetch(auth.refreshUrl)).status,405);
  for(const body of ['{',JSON.stringify({refresh_token:'initial'}),'x'.repeat(70000)]){
    const response=await fetch(auth.refreshUrl,{method:'POST',body});
    assert.ok([400,413].includes(response.status));
  }
});

test('transient refresh failures are classified without persisting raw errors',async t=>{
  const f=await fixture(t,(req,res)=>{res.statusCode=503;res.end('private upstream error');});
  const failures=[];
  const auth=await createCodexAuth({sharedHome:f.root,privateHome:join(f.root,'private'),refreshEndpoint:`http://127.0.0.1:1/token`,onFailure:code=>{failures.push(code);}});
  t.after(()=>auth.close());
  const response=await refresh(auth);
  assert.equal(response.status,503);
  assert.deepEqual(failures,['auth_refresh_unavailable']);
  assert.equal(JSON.parse(await readFile(f.shared,'utf8')).tokens.refresh_token,'initial');
});


test('process death releases an in-flight refresh lock for another session',{timeout:8000},async t=>{
  let entered;
  const incoming=new Promise(resolve=>{entered=resolve;});
  const f=await fixture(t,(req,res)=>{entered();req.on('close',()=>res.end('{}'));});
  const upstream=createServer((req,res)=>{entered();req.on('close',()=>res.end('{}'));});
  await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
  t.after(()=>{upstream.closeAllConnections();return new Promise(resolve=>upstream.close(resolve));});
  const source=`import {createCodexAuth} from ${JSON.stringify(new URL('./auth.mjs',import.meta.url).href)};
    const auth=await createCodexAuth(JSON.parse(process.argv[1]));
    await fetch(auth.refreshUrl,{method:'POST',body:JSON.stringify({grant_type:'refresh_token',client_id:'app_EMoamEEZ73f0CkXaXp7hrann',refresh_token:'initial'})});`;
  const child=spawn(process.execPath,['--input-type=module','-e',source,JSON.stringify({sharedHome:f.root,privateHome:join(f.root,'doomed'),refreshEndpoint:`http://127.0.0.1:${upstream.address().port}/token`})],{stdio:'ignore'});
  const exited=new Promise(resolve=>child.once('close',resolve));
  t.after(()=>child.kill('SIGKILL'));
  await incoming;
  child.kill('SIGKILL');await exited;
  const next=await f.make('survivor');
  assert.equal(JSON.parse(await readFile(join(next.home,'auth.json'),'utf8')).tokens.refresh_token,'initial');
});
