import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

// Execute the real wrapper/output module. Replace only the CLI process and its
// filesystem boundary; no /auth access, /tmp writes, Docker, or model calls.
async function wrapper(scenario, options={}) {
  const fakeFs=`
    const files=new Map();let reads=0;
    export async function mkdir(){}
    export async function readdir(){return ${JSON.stringify(options.session?.id && options.exists ? [{name:"rollout-test-"+options.session.id+".jsonl",parentPath:"/synthetic"}] : [])}.map(e=>({...e,isFile:()=>true}));}
    export async function symlink(){}
    export async function stat(){if(${!!options.exists})return {size:10};throw Object.assign(Error(),{code:"ENOENT"});}
    export async function writeFile(path,data){files.set(path,data);}
    export async function readFile(path){
      if(path==='/auth/.credentials.json')return JSON.stringify({claudeAiOauth:{synthetic:true}});
      if(path==='/auth/auth.json')return JSON.stringify({auth_mode:'chatgpt',tokens:{synthetic:true}});
      if(path.endsWith('coverage.json') && ++reads>1) {
        const ctx=JSON.parse(files.get('/tmp/review/context.json'));
        return JSON.stringify({files_0:[[0,JSON.stringify(ctx.files[0]).length]]});
      }
      return files.get(path);
    }
  `;
  const fakeProcess=`
    import {EventEmitter} from 'node:events';
    import {PassThrough} from 'node:stream';
    export function spawn(command,args){
      const child=new EventEmitter();
      child.stdout=new PassThrough();child.stderr=new PassThrough();child.stdin=new PassThrough();
      let prompt='';child.stdin.on('data',b=>prompt+=b);
      child.stdin.on('end',()=>child.stdout.write(JSON.stringify({type:'test_command',command,args,prompt})+'\\n'));
      let ended=false;
      function finish(code,signal=null){if(ended)return;ended=true;child.stdout.end();child.stderr.end();setImmediate(()=>child.emit('close',code,signal));}
      child.kill=signal=>{finish(null,signal);return true;};
      setTimeout(()=>{
        if(${JSON.stringify(scenario)}==='spawn') {child.emit('error',new Error('synthetic'));finish(-2);return;}
        if(${JSON.stringify(scenario)}==='truncated') {child.stdout.write('{"type":');finish(0);return;}
        if(${JSON.stringify(scenario)}==='nonzero') {child.stderr.write('synthetic transport failure');finish(2);return;}
        const event=JSON.stringify({type:'item.completed',item:{type:'agent_message',text:'x'.repeat(90000)}})+'\\n';
        child.stdout.write(event.slice(0,65536));
        setTimeout(()=>{child.stdout.write(event.slice(65536));child.stdout.write('{"type":"turn.completed"}\\n');finish(0);},1200);
      },20);
      return child;
    }
  `;
  const hook=`import {registerHooks} from 'node:module';
    registerHooks({load(url,context,next){
      if(url.endsWith('/auth.mjs'))return {format:'module',source:"export async function createCodexAuth(){return {home:'/tmp/codex',refreshUrl:'http://127.0.0.1:1234/token',close:async()=>{}};}",shortCircuit:true};
      if(url==='node:fs/promises')return {format:'module',source:${JSON.stringify(fakeFs)},shortCircuit:true};
      if(url==='node:child_process')return {format:'module',source:${JSON.stringify(fakeProcess)},shortCircuit:true};
      return next(url,context);
    }});`;
  const child=spawn(process.execPath,['--import','data:text/javascript,'+encodeURIComponent(hook),fileURLToPath(new URL('./entry.mjs',import.meta.url))],{stdio:['pipe','pipe','pipe']});
  let stdout='',stderr='';
  child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);
  child.stdin.end(JSON.stringify({kind:options.kind??'codex_cli',session:options.session,model:'synthetic-model',context:{files:[{content:'x'.repeat(1500)}]},schema:{},system:'You have no tools or credentials.'}));
  const code=await new Promise((resolve,reject)=>{child.on('error',reject);child.on('close',resolve);});
  return {code,stderr,events:stdout.trim().split('\n').filter(Boolean).map(JSON.parse)};
}

test('real wrapper keeps fragmented CLI output separate from timer progress and flushes final events',{timeout:10000},async()=>{
  const {code,events,stderr}=await wrapper('success');
  assert.equal(code,0,stderr);
  assert.equal(events.find(e=>e.type==='item.completed').item.text.length,90000);
  assert.ok(events.some(e=>e.type==='runner_coverage'&&e.read===1));
  assert.equal(events.at(-1).type,'runner_exit');
  assert.equal(events.at(-1).code,0);
  assert.ok(events.findIndex(e=>e.type==='turn.completed')<events.length-1);
});

test('real wrapper reports truncated CLI output, spawn failure, and nonzero exit before final exit',{timeout:10000},async()=>{
  for(const [scenario,error,exit] of [['truncated','cli_stream_truncated',1],['spawn','spawn_failed',1],['nonzero','cli_failed',2]]) {
    const {code,events}=await wrapper(scenario);
    assert.equal(code,exit);
    assert.equal(events.at(-2).code,error);
    assert.equal(events.at(-1).type,'runner_exit');
    assert.equal(events.at(-1).code,exit);
  }
});


test('wrapper resumes explicit IDs only and preserves restricted CLI flags',async()=>{
  const session={key:'11111111-1111-4111-8111-111111111111',id:'22222222-2222-4222-8222-222222222222'};
  for(const kind of ['codex_cli','claude_cli'])for(const exists of [true,false]) {
    const {code,events,stderr}=await wrapper('nonzero',{kind,session,exists});
    assert.equal(code,2,stderr);
    const command=events.find(e=>e.type==='test_command');
    assert.ok(command,stderr);
    assert.equal(command.args.includes(session.id),exists);
    assert.equal(command.args.includes('--ephemeral'),false);
    assert.equal(command.args.includes('--no-session-persistence'),false);
    assert.equal(events.find(e=>e.type==='runner_session').resumed,exists);
    assert.equal(command.prompt.includes('Prior conversation is historical'),exists);
    if(kind==='codex_cli') {
      assert.ok(command.args.includes('approval_policy="never"'));
      assert.ok(command.args.includes('--sandbox'));
      assert.ok(command.args.includes('--output-schema'));
    } else {
      assert.ok(command.args.includes('--restricted'));
      assert.ok(command.args.includes('--strict-mcp-config'));
      assert.ok(command.args.includes('--json-schema'));
    }
  }
});
