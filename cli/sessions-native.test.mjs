// Offline integration with pinned binaries. No real credentials or external network.
// docker run --rm --network none -v "$PWD:/workspace:ro" -w /workspace \
//   -e REVIEW_NATIVE_SESSIONS=1 --entrypoint node auto-reviewer-cli:session-candidate \
//   --test cli/sessions-native.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {prepareSession} from './sessions.mjs';

async function run(binary,args,env,prompt) {
  const child=spawn(binary,args,{cwd:'/tmp/review',env:{PATH:process.env.PATH,HOME:'/tmp/review',...env},stdio:['pipe','pipe','pipe']});
  let stdout='',stderr='';
  child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);
  child.stdin.end(prompt);
  const timeout=setTimeout(()=>child.kill('SIGKILL'),20000);
  const code=await new Promise((resolve,reject)=>{child.on('error',reject);child.on('close',resolve);});
  clearTimeout(timeout);
  assert.equal(code,0,stderr+'\n'+stdout);
  return stdout.trim().split('\n').map(JSON.parse);
}
for(const kind of ['codex_cli','claude_cli'])test(`${kind} resumes history in a new private home with the pinned native CLI`,
  {skip:!process.env.REVIEW_NATIVE_SESSIONS,timeout:50000},async t=>{
    await mkdir('/tmp/review',{recursive:true});
    const root=await mkdtemp(join(tmpdir(),'native-history-'));
    t.after(()=>rm(root,{recursive:true,force:true}));
    const requests=[];
    const upstream=createServer(async(req,res)=>{
      let raw='';for await(const chunk of req)raw+=chunk;
      if(!req.url.includes('responses')&&!req.url.includes('messages')) {res.writeHead(200,{'content-type':'application/json'});res.end('{}');return;}
      const body=JSON.parse(raw);requests.push(body);
      res.writeHead(200,{'content-type':'text/event-stream'});
      const send=(event,data)=>res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      if(kind==='codex_cli') {
        const item={id:'msg_test',type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:'Recorded.',annotations:[]}]};
        send('response.created',{type:'response.created',response:{id:'resp_test',status:'in_progress',output:[]}});
        send('response.output_item.added',{type:'response.output_item.added',output_index:0,item:{...item,status:'in_progress',content:[]}});
        send('response.output_text.delta',{type:'response.output_text.delta',item_id:item.id,output_index:0,content_index:0,delta:'Recorded.'});
        send('response.output_item.done',{type:'response.output_item.done',output_index:0,item});
        send('response.completed',{type:'response.completed',response:{id:'resp_test',status:'completed',output:[item],usage:{input_tokens:10,output_tokens:2,total_tokens:12}}});
      } else {
        send('message_start',{type:'message_start',message:{id:'msg_test',type:'message',role:'assistant',model:body.model,content:[],stop_reason:null,usage:{input_tokens:10,output_tokens:0}}});
        send('content_block_start',{type:'content_block_start',index:0,content_block:{type:'text',text:''}});
        send('content_block_delta',{type:'content_block_delta',index:0,delta:{type:'text_delta',text:'Recorded.'}});
        send('content_block_stop',{type:'content_block_stop',index:0});
        send('message_delta',{type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:2}});
        send('message_stop',{type:'message_stop'});
      }
      res.end();
    });
    await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
    t.after(()=>{upstream.closeAllConnections();return new Promise(resolve=>upstream.close(resolve));});
    const url=`http://127.0.0.1:${upstream.address().port}`;
    const key=randomUUID();let id=null;
    for(const cycle of ['first','second']) {
      const home=join(root,cycle);
      const resumed=await prepareSession(kind,{key,id},{home,root:join(root,'persistent')});
      assert.equal(resumed,id);
      let args,env;
      if(kind==='codex_cli') {
        args=['exec','--ignore-user-config','--ignore-rules','--skip-git-repo-check','--sandbox','read-only','--json','--color','never','--model','gpt-5.4',
          '-c','model_provider="synthetic"','-c','model_providers.synthetic.name="Synthetic"',
          '-c',`model_providers.synthetic.base_url="${url}/v1"`,'-c','model_providers.synthetic.wire_api="responses"',
          '-c','model_providers.synthetic.requires_openai_auth=false','-c','model_providers.synthetic.supports_websockets=false',
          '-c','approval_policy="never"','-c','web_search="disabled"','--disable','shell_tool','--disable','unified_exec',
          ...(id?['resume',id]:[]),'-'];
        env={CODEX_HOME:home};
      } else {
        args=['-p','--restricted','--setting-sources','','--tools','','--permission-mode','dontAsk',
          '--permission-prompts','none','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
          '--system-prompt','Answer briefly.','--model','claude-sonnet-4-6','--output-format','stream-json','--verbose',...(id?['--resume',id]:[])];
        env={CLAUDE_CONFIG_DIR:home,ANTHROPIC_API_KEY:'synthetic',ANTHROPIC_BASE_URL:url,CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1'};
      }
      const events=await run(kind==='codex_cli'?'codex':'claude',args,env,cycle==='first'?'Remember synthetic-PR-marker-73519.':'Continue this review.');
      const start=events.find(e=>kind==='codex_cli'?e.type==='thread.started':e.type==='system'&&e.subtype==='init');
      assert.ok(start,JSON.stringify(events));
      const found=start.thread_id??start.session_id;
      if(id)assert.equal(found,id);else id=found;
      await rm(home,{recursive:true,force:true});
    }
    assert.ok(requests.length>=2);
    assert.match(JSON.stringify(requests.at(-1)),/synthetic-PR-marker-73519/);
    assert.match(JSON.stringify(requests.at(-1)),/Continue this review/);
  });
