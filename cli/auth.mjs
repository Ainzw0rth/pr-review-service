// Trusted Codex credential coordinator. Native sessions write only private state;
// the stable shared flock covers OAuth rotation, never model inference.
import {spawn} from 'node:child_process';
import {mkdir,readFile,open,rename,unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createServer} from 'node:http';

const CLIENT_ID='app_EMoamEEZ73f0CkXaXp7hrann';
const TOKEN_ENDPOINT='https://auth.openai.com/oauth/token';
const MAX_BYTES=65536;
const failure=(code='auth_missing')=>Object.assign(new Error(code),{code});

async function lock(path){
  // flock releases on process death too; stdin EOF ends its small lock holder.
  const child=spawn('flock',['--exclusive','--timeout','30',path,process.execPath,'-e',
    "process.stdout.write('locked\\n');process.stdin.resume();"],{stdio:['pipe','pipe','ignore']});
  const closed=new Promise(resolve=>child.once('close',resolve));
  child.stdin.on('error',()=>{});
  try{
    await new Promise((resolve,reject)=>{
      child.once('error',()=>reject(failure('auth_refresh_unavailable')));
      child.once('close',()=>reject(failure('auth_refresh_unavailable')));
      child.stdout.once('data',()=>resolve());
    });
  }catch(error){child.stdin.end();await closed;throw error;}
  return async()=>{child.stdin.end();await closed;};
}

async function atomicJSON(path,value){
  const temporary=path+'.'+randomUUID()+'.tmp';
  let handle;
  try{
    handle=await open(temporary,'wx',0o600);
    await handle.writeFile(JSON.stringify(value));
    await handle.sync();
    await handle.close();handle=null;
    await rename(temporary,path);
  }finally{
    if(handle)await handle.close();
    await unlink(temporary).catch(()=>{});
  }
}

function identity(auth){
  const tokens=auth?.tokens;
  if(auth?.auth_mode!=='chatgpt'||!tokens||typeof tokens.account_id!=='string'||!tokens.account_id||
     !['id_token','access_token','refresh_token'].every(key=>typeof tokens[key]==='string'&&tokens[key]&&tokens[key].length<=16000))throw failure();
  let claims;
  try{claims=JSON.parse(Buffer.from(tokens.id_token.split('.')[1],'base64url').toString());}catch{throw failure();}
  if(typeof claims?.sub!=='string'||!claims.sub)throw failure();
  return JSON.stringify([tokens.account_id,claims.sub]);
}

async function readAuth(path){
  let raw;try{raw=await readFile(path,'utf8');}catch{throw failure();}
  if(Buffer.byteLength(raw)>MAX_BYTES)throw failure();
  let value;try{value=JSON.parse(raw);}catch{throw failure();}
  identity(value);
  return value;
}

function tokenResponse(auth){
  const {access_token,refresh_token,id_token}=auth.tokens;
  return {access_token,refresh_token,id_token,token_type:'Bearer'};
}

export async function createCodexAuth({sharedHome='/auth',privateHome='/tmp/codex',refreshEndpoint=TOKEN_ENDPOINT,onFailure=async()=>{}}={}){
  const path=join(sharedHome,'auth.json'),lockPath=join(sharedHome,'.codex-refresh.lock');
  await mkdir(privateHome,{recursive:true,mode:0o700});
  const release=await lock(lockPath);
  let initial;
  try{
    initial=await readAuth(path);
    // Exclude arbitrary shared configuration and API credentials from each session.
    await atomicJSON(join(privateHome,'auth.json'),{auth_mode:'chatgpt',OPENAI_API_KEY:null,tokens:initial.tokens,last_refresh:initial.last_refresh});
  }finally{await release();}
  const account=identity(initial),knownTokens=new Set([initial.tokens.refresh_token]);
  const route='/token/'+randomUUID();
  const server=createServer(async(req,res)=>{
    const reply=(status,value)=>{
      res.writeHead(status,{'content-type':'application/json','cache-control':'no-store'});
      res.end(JSON.stringify(value));
    };
    if(req.method!=='POST'||req.url!==route){reply(405,{error:'invalid_request'});return;}
    let releaseRefresh;
    try{
      let raw='';
      for await(const chunk of req){
        raw+=chunk;
        if(Buffer.byteLength(raw)>MAX_BYTES){reply(413,{error:'invalid_request'});return;}
      }
      let request;try{request=JSON.parse(raw);}catch{reply(400,{error:'invalid_request'});return;}
      if(request?.grant_type!=='refresh_token'||request.client_id!==CLIENT_ID||typeof request.refresh_token!=='string'){
        reply(400,{error:'invalid_request'});return;
      }
      if(!knownTokens.has(request.refresh_token))throw failure();
      releaseRefresh=await lock(lockPath);
      const current=await readAuth(path);
      if(identity(current)!==account)throw failure();
      let updated=current;
      if(request.refresh_token===current.tokens.refresh_token){
        const response=await fetch(refreshEndpoint,{
          method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({grant_type:'refresh_token',client_id:CLIENT_ID,refresh_token:current.tokens.refresh_token}),
          redirect:'error',signal:AbortSignal.timeout(20000),
        });
        if(!response.ok){await response.body?.cancel();throw failure([400,401,403].includes(response.status)?'auth_missing':'auth_refresh_unavailable');}
        let rawResponse='';
        for await(const chunk of response.body){
          rawResponse+=Buffer.from(chunk).toString();
          if(Buffer.byteLength(rawResponse)>MAX_BYTES)throw failure('auth_refresh_unavailable');
        }
        let tokens;try{tokens=JSON.parse(rawResponse);}catch{throw failure('auth_refresh_unavailable');}
        if(!tokens||typeof tokens!=='object'||!['access_token','refresh_token','id_token'].some(key=>tokens[key]!=null))throw failure('auth_refresh_unavailable');
        updated={...current,tokens:{...current.tokens,access_token:tokens.access_token??current.tokens.access_token,
          refresh_token:tokens.refresh_token??current.tokens.refresh_token,
          id_token:tokens.id_token??current.tokens.id_token},last_refresh:new Date().toISOString()};
        if(identity(updated)!==account)throw failure();
        // Persist before replying: no sibling may reuse the old rotating token.
        try{await atomicJSON(path,updated);}catch{throw failure();}
      }
      knownTokens.add(updated.tokens.refresh_token);
      reply(200,tokenResponse(updated));
    }catch(error){
      const authFailure=error.code==='auth_missing';
      await onFailure(authFailure?'auth_missing':'auth_refresh_unavailable');
      reply(authFailure?401:503,{error:authFailure?'invalid_grant':'temporarily_unavailable'});
    }finally{if(releaseRefresh)await releaseRefresh();}
  });
  server.requestTimeout=30000;
  server.headersTimeout=10000;
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  return {home:privateHome,refreshUrl:`http://127.0.0.1:${server.address().port}${route}`,
    close:()=>new Promise(resolve=>{server.close(resolve);server.closeIdleConnections();})};
}
