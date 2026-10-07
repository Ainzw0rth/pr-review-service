// Read account metadata only. Never start inference or return account identifiers/tokens.
import {readFile, mkdir} from 'node:fs/promises';
import {spawn} from 'node:child_process';

const window = (name, value, percent, reset) => ({name, used_percent:value?.[percent], resets_at:reset});

export async function quotaSnapshot(kind) {
  try {
    await mkdir('/tmp/home', {recursive:true});
    if (kind === 'claude_cli') {
      const auth=JSON.parse(await readFile('/auth/.credentials.json','utf8'));
      const token=auth.claudeAiOauth?.accessToken;
      if (typeof token !== 'string' || !token) return {error:'auth'};
      // Same endpoint used by the installed Claude Code /usage implementation.
      // This is not a stable public API: errors stay unknown, never fabricated percentages.
      const response=await fetch('https://api.anthropic.com/api/oauth/usage', {
        headers:{Authorization:`Bearer ${token}`, 'anthropic-beta':'oauth-2025-04-20'},
        redirect:'error', signal:AbortSignal.timeout(15000)
      });
      if (!response.ok) return {error:response.status===401||response.status===403?'auth':'unavailable'};
      let raw='';
      for await (const chunk of response.body) {
        raw+=Buffer.from(chunk).toString();
        if(raw.length>65536) return {error:'unavailable'};
      }
      const data=JSON.parse(raw);
      return {windows:['five_hour','seven_day','seven_day_opus'].filter(key=>data[key]).map(key=>
        window(key,data[key],'utilization',Math.floor(Date.parse(data[key].resets_at)/1000)))};
    }
    if (kind !== 'codex_cli') return {error:'unsupported'};
    return await new Promise(resolve=>{
      const child=spawn('codex',['app-server','--listen','stdio://'], {
        env:{PATH:process.env.PATH,HOME:'/tmp/home',CODEX_HOME:'/auth',LANG:'C.UTF-8'},
        cwd:'/tmp',stdio:['pipe','pipe','ignore']
      });
      let pending='',bytes=0,settled=false;
      const finish=value=>{if(settled)return;settled=true;clearTimeout(timer);child.kill('SIGKILL');resolve(value);};
      const timer=setTimeout(()=>finish({error:'unavailable'}),20000);
      const send=value=>child.stdin.write(JSON.stringify(value)+'\n');
      child.on('error',()=>finish({error:'unavailable'}));
      child.on('close',()=>finish({error:'unavailable'}));
      child.stdin.on('error',()=>finish({error:'unavailable'}));
      child.stdout.on('data',chunk=>{
        bytes+=chunk.length;
        if(bytes>1048576)return finish({error:'unavailable'});
        pending+=chunk.toString();
        while(pending.includes('\n')) {
          const i=pending.indexOf('\n'),line=pending.slice(0,i);pending=pending.slice(i+1);
          let msg;try{msg=JSON.parse(line);}catch{continue;}
          if(msg.id===1) {
            if(msg.error)return finish({error:'unavailable'});
            send({method:'initialized',params:{}});
            send({method:'account/rateLimits/read',id:2});
          }
          if(msg.id===2) {
            if(msg.error)return finish({error:'unavailable'});
            const limits=msg.result?.rateLimitsByLimitId?.codex ?? msg.result?.rateLimits;
            // Do not mislabel a model-specific bucket as a whole-account limit.
            if(!limits || (limits.limitId && limits.limitId!=='codex'))return finish({error:'unsupported'});
            finish({windows:['primary','secondary'].filter(key=>limits[key]).map(key=>
              window(key,limits[key],'usedPercent',limits[key].resetsAt))});
          }
        }
      });
      send({id:1,method:'initialize',params:{clientInfo:{name:'pr_review_quota',version:'1'},capabilities:{experimentalApi:true}}});
    });
  } catch { return {error:'unavailable'}; }
}
