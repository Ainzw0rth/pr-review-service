// Minimal read-only MCP server: fixed in-memory evidence, never arbitrary paths or commands.
import {readFile,writeFile} from 'node:fs/promises';
import {createInterface} from 'node:readline';
export function artifacts(context) {
  const result=[];
  for(const [section,value] of Object.entries(context)) {
    if(Array.isArray(value)) value.forEach((entry,index)=>result.push({id:`${section}_${index}`,title:entry?.filename||entry?.path||entry?.title||`${section} ${index+1}`,text:JSON.stringify(entry)}));
    else result.push({id:section,title:section,text:JSON.stringify(value)});
  }
  return result;
}
export function readEvidence(entries, coverage, args) {
  if(!args || !Array.isArray(args.reads) || !args.reads.length || args.reads.length>3)throw Error('Provide 1–3 reads.');
  return args.reads.map(read=>{
    const item=entries.find(x=>x.id===read.id);
    const offset=read.offset??0;
    const length=read.length??20000;
    if(!item||!Number.isInteger(offset)||offset<0||offset>item.text.length||!Number.isInteger(length)||length<1||length>20000)throw Error('Invalid evidence range.');
    const end=Math.min(offset+length,item.text.length);
    const ranges=[...(coverage[item.id]||[]),[offset,end]].sort((a,b)=>a[0]-b[0]);
    const merged=[];
    for(const range of ranges){const last=merged.at(-1);if(last&&range[0]<=last[1])last[1]=Math.max(last[1],range[1]);else merged.push([...range]);}
    coverage[item.id]=merged;
    return {id:item.id,offset,next_offset:end,eof:end===item.text.length,content:item.text.slice(offset,end)};
  });
}
if(process.argv[1]?.endsWith('/evidence.mjs')) {
  const entries=artifacts(JSON.parse(await readFile('/tmp/review/context.json','utf8')));
  let coverage={};
  try{coverage=JSON.parse(await readFile('/tmp/review/coverage.json','utf8'));}catch{}
  const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
  const tool={name:'read_evidence',annotations:{readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false},description:'Read exact ranges of commit-pinned PR evidence by manifest ID. Read every artifact fully before completing the review; use next_offset until eof. Up to 3 reads per call.',inputSchema:{type:'object',properties:{reads:{type:'array',minItems:1,maxItems:3,items:{type:'object',properties:{id:{type:'string'},offset:{type:'integer',minimum:0},length:{type:'integer',minimum:1,maximum:20000}},required:['id'],additionalProperties:false}}},required:['reads'],additionalProperties:false}};
  for await(const line of createInterface({input:process.stdin,crlfDelay:Infinity})) {
    let request;
    try{
      if(line.length>100000)throw Error('Request too large');
      request=JSON.parse(line);
      if(request.id===undefined)continue;
      let result;
      if(request.method==='initialize')result={protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'review-context',version:'1.0.0'}};
      else if(request.method==='tools/list')result={tools:[tool]};
      else if(request.method==='ping')result={};
      else if(request.method==='tools/call'&&request.params?.name==='read_evidence'){
        const values=readEvidence(entries,coverage,request.params.arguments);
        await writeFile('/tmp/review/coverage.json',JSON.stringify(coverage),{mode:0o600});
        result={content:[{type:'text',text:JSON.stringify(values)}]};
      }else throw Error('Unsupported evidence operation');
      send({jsonrpc:'2.0',id:request.id,result});
    }catch(error){if(request?.id!==undefined)send({jsonrpc:'2.0',id:request.id,error:{code:-32602,message:error.message}});}
  }
}
