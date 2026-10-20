// Opt-in, pinned-native protocol check. No account credentials or inference.
// docker run --rm --network none -v "$PWD:/workspace:ro" -w /workspace \
//   -e CODEX_NATIVE_BINARY=/usr/local/bin/codex --entrypoint node \
//   auto-reviewer-cli:parallel-candidate --test cli/auth-native.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createServer} from 'node:http';
import {createCodexAuth} from './auth.mjs';

const binary=process.env.CODEX_NATIVE_BINARY;
function jwt(exp){
  const claims={sub:'synthetic-user',email:'synthetic@example.invalid',exp,
    'https://api.openai.com/auth':{chatgpt_account_id:'synthetic-account',chatgpt_plan_type:'plus'}};
  return Buffer.from('{"alg":"none"}').toString('base64url')+'.'+Buffer.from(JSON.stringify(claims)).toString('base64url')+'.synthetic';
}
function native(t,auth,root){
  const child=spawn(binary,['app-server'],{cwd:root,env:{PATH:process.env.PATH,HOME:root,
    CODEX_HOME:auth.home,CODEX_REFRESH_TOKEN_URL_OVERRIDE:auth.refreshUrl},stdio:['pipe','pipe','pipe']});
  let next=0,stderr='';const pending=new Map();
  child.stderr.on('data',chunk=>{stderr+=chunk;});
  const lines=createInterface({input:child.stdout});
  lines.on('line',line=>{
    let msg;try{msg=JSON.parse(line);}catch{return;}
    if(!pending.has(msg.id))return;
    const {resolve,reject}=pending.get(msg.id);pending.delete(msg.id);
    if(msg.error)reject(new Error(JSON.stringify(msg.error)));else resolve(msg.result);
  });
  child.on('error',error=>{for(const {reject} of pending.values())reject(error);pending.clear();});
  child.on('close',code=>{for(const {reject} of pending.values())reject(new Error(`native exit ${code}: ${stderr}`));pending.clear();});
  t.after(async()=>{
    child.stdin.end();child.kill('SIGTERM');
    await new Promise(resolve=>{if(child.exitCode!==null||child.signalCode!==null)resolve();else child.once('close',resolve);});
    lines.close();
  });
  const request=(method,params)=>new Promise((resolve,reject)=>{
    const id=++next;pending.set(id,{resolve,reject});child.stdin.write(JSON.stringify({id,method,params})+'\n');
  });
  return {request,notify:(method,params)=>child.stdin.write(JSON.stringify({method,params})+'\n')};
}

test('pinned native Codex sessions coordinate one rotation and persist only private state',
  {skip:!binary,timeout:30000},async t=>{
    const root=await mkdtemp(join(tmpdir(),'review-native-auth-'));
    t.after(()=>rm(root,{recursive:true,force:true}));
    const sharedHome=join(root,'shared');await mkdir(sharedHome);
    const initial={auth_mode:'chatgpt',OPENAI_API_KEY:null,last_refresh:new Date().toISOString(),tokens:{
      id_token:jwt(4102444800),access_token:jwt(4102444800),refresh_token:'synthetic-initial',account_id:'synthetic-account'}};
    await writeFile(join(sharedHome,'auth.json'),JSON.stringify(initial),{mode:0o600});
    let exchanges=0;
    const upstream=createServer(async(req,res)=>{
      let raw='';for await(const chunk of req)raw+=chunk;
      exchanges++;
      assert.equal(req.method,'POST');
      const request=JSON.parse(raw);
      assert.equal(request.grant_type,'refresh_token');
      assert.equal(request.client_id,'app_EMoamEEZ73f0CkXaXp7hrann');
      if(request.refresh_token!=='synthetic-initial'){res.writeHead(400);res.end('{}');return;}
      await new Promise(resolve=>setTimeout(resolve,50));
      res.setHeader('content-type','application/json');
      res.end(JSON.stringify({id_token:jwt(4102444800),access_token:jwt(4102444800),refresh_token:'synthetic-rotated'}));
    });
    await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
    t.after(()=>new Promise(resolve=>upstream.close(resolve)));
    const sessions=await Promise.all(['one','two'].map(name=>createCodexAuth({sharedHome,
      privateHome:join(root,name),refreshEndpoint:`http://127.0.0.1:${upstream.address().port}/token`})));
    for(const session of sessions)t.after(()=>session.close());
    const clients=sessions.map(session=>native(t,session,root));
    await Promise.all(clients.map(async client=>{
      await client.request('initialize',{clientInfo:{name:'synthetic_auth_test',version:'1'},capabilities:{experimentalApi:true}});
      client.notify('initialized',{});
    }));
    // account/read does routing discovery after auth. Network is deliberately absent;
    // this specific post-auth failure is expected, and persisted native tokens below
    // establish that the actual native refresh completed before discovery.
    const results=await Promise.allSettled(clients.map(client=>client.request('account/read',{refreshToken:true})));
    for(const result of results){
      if(result.status==='rejected')assert.match(result.reason.message,/workspace routing discovery failed/);
      else assert.equal(result.value.account?.type,'chatgpt');
    }
    assert.equal(exchanges,1,'both native sessions must share exactly one upstream rotation');
    const shared=JSON.parse(await readFile(join(sharedHome,'auth.json'),'utf8'));
    assert.equal(shared.tokens.refresh_token,'synthetic-rotated');
    for(const session of sessions){
      const local=JSON.parse(await readFile(join(session.home,'auth.json'),'utf8'));
      assert.equal(local.tokens.refresh_token,'synthetic-rotated','native must persist refreshed credentials in private home');
    }
    // A later native/local write cannot replace the authoritative shared file.
    await writeFile(join(sessions[0].home,'auth.json'),JSON.stringify(initial));
    assert.equal(JSON.parse(await readFile(join(sharedHome,'auth.json'),'utf8')).tokens.refresh_token,'synthetic-rotated');
  });
