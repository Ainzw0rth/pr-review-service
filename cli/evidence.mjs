// Minimal read-only MCP server: fixed in-memory evidence, never arbitrary paths or commands.
import {readFile,writeFile} from 'node:fs/promises';
import {createInterface} from 'node:readline';
export function artifacts(context) {
  const result=[];
  for(const [section,value] of Object.entries(context)) {
    if(Array.isArray(value)) value.forEach((entry,index)=>result.push({id:`${section}_${index}`,title:entry?.filename||entry?.path||entry?.title||`${section} ${index+1}`,text:JSON.stringify(entry)}));
    else result.push({id:section,title:section,text:JSON.stringify(value)});
  }
  const seen=new Map();
  for(const item of result) {
    if(seen.has(item.text))item.canonical_id=seen.get(item.text);
    else seen.set(item.text,item.id);
  }
  return result;
}
export function missingEvidence(entries, coverage) {
  return entries.filter(item => {
    const ranges=coverage[item.canonical_id||item.id];
    return !(ranges?.length===1 && ranges[0][0]===0 && ranges[0][1]===item.text.length);
  }).map(item=>item.id);
}
export function evidenceManifest(entries) {
  const inline={},coverage={};
  const rows=entries.map(item=>{
    if(!item.canonical_id && item.text.length<=1000){
      inline[item.id]=item.text;
      coverage[item.id]=[[0,item.text.length]];
    }
    return [item.id,item.title,item.text.length,item.canonical_id||null];
  });
  return {index:{columns:['id','title','characters','alias_of'],rows},inline,coverage};
}

class EvidenceError extends Error {
  constructor(code,message,hint){super(message);this.code=code;this.hint=hint;}
}

export function readEvidence(entries, coverage, args) {
  if(!args || typeof args!=='object' || Array.isArray(args) || Object.keys(args).some(key=>key!=='reads') || !Array.isArray(args.reads) || !args.reads.length || args.reads.length>12)
    throw new EvidenceError('INVALID_READS','Provide 1–12 reads.','Use {reads:[{id:"manifest ID",offset:0,length:20000}]}; no other fields.');
  // Validate the entire batch before updating coverage: failed calls deliver no evidence.
  const batch=args.reads.map(read=>{
    if(!read || typeof read!=='object' || Array.isArray(read) || Object.keys(read).some(key=>!['id','offset','length'].includes(key)))
      throw new EvidenceError('INVALID_READ','Invalid read object.','Each read accepts only id, offset and length.');
    const item=entries.find(x=>x.id===read.id);
    if(!item)throw new EvidenceError('UNKNOWN_ARTIFACT','Unknown evidence ID.','Use an exact ID from the evidence manifest; paths and URLs are not accepted.');
    const offset=read.offset===undefined?0:read.offset;
    const length=read.length===undefined?20000:read.length;
    if(!Number.isInteger(offset)||offset<0||offset>item.text.length||!Number.isInteger(length)||length<1||length>20000)
      throw new EvidenceError('INVALID_RANGE','Invalid evidence range.','Use offset from 0 to characters, and length from 1 to 20000; follow next_read when present.');
    return {item,offset,end:Math.min(offset+length,item.text.length)};
  });
  if(batch.reduce((total,read)=>total+read.end-read.offset,0)>60000)
    throw new EvidenceError('BATCH_TOO_LARGE','Batch exceeds 60000 characters.','Split the batch or reduce lengths; maximum 12 reads and 60000 content characters per call.');
  const result=batch.map(({item,offset,end})=>({id:item.id,offset,next_offset:end,eof:end===item.text.length,content:item.text.slice(offset,end),...(end<item.text.length?{next_read:{id:item.id,offset:end,length:Math.min(end-offset,item.text.length-end)}}:{})}));
  // Coverage must describe evidence delivered, not a reply the CLI may truncate.
  if(Buffer.byteLength(JSON.stringify(result),'utf8')>24000)
    throw new EvidenceError('REPLY_TOO_LARGE','Evidence reply exceeds 24000 UTF-8 bytes.','Split the batch or reduce read lengths; retry with length 4000, then follow next_read. JSON escaping and non-ASCII text count toward the reply-byte limit.');
  for(const {item,offset,end} of batch){
    const key=item.canonical_id||item.id;
    const ranges=[...(coverage[key]||[]),[offset,end]].sort((a,b)=>a[0]-b[0]);
    const merged=[];
    for(const range of ranges){const last=merged.at(-1);if(last&&range[0]<=last[1])last[1]=Math.max(last[1],range[1]);else merged.push([...range]);}
    coverage[key]=merged;
  }
  return result;
}

export function toolRead(entries,coverage,args) {
  try {
    return {content:[{type:'text',text:JSON.stringify(readEvidence(entries,coverage,args))}]};
  } catch(error) {
    if(!(error instanceof EvidenceError))throw error;
    return {isError:true,content:[{type:'text',text:JSON.stringify({error:{code:error.code,message:error.message,hint:error.hint}})}]};
  }
}
if(process.argv[1]?.endsWith('/evidence.mjs')) {
  const entries=artifacts(JSON.parse(await readFile('/tmp/review/context.json','utf8')));
  let coverage={};
  try{coverage=JSON.parse(await readFile('/tmp/review/coverage.json','utf8'));}catch{}
  const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
  const tool={name:'read_evidence',annotations:{readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false},description:'Read exact ranges of commit-pinned PR evidence by manifest ID. Read every artifact fully before completing the review; follow next_read until eof. Batch up to 12 reads per call, at most 60000 content characters total and 24000 UTF-8 bytes in the serialized reply. Split batches or use smaller lengths when the reply limit is exceeded. Errors include a code and corrective hint; failed batches grant no coverage.',inputSchema:{type:'object',properties:{reads:{type:'array',minItems:1,maxItems:12,items:{type:'object',properties:{id:{type:'string'},offset:{type:'integer',minimum:0},length:{type:'integer',minimum:1,maximum:20000}},required:['id'],additionalProperties:false}}},required:['reads'],additionalProperties:false}};
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
        result=toolRead(entries,coverage,request.params.arguments);
        if(!result.isError)await writeFile('/tmp/review/coverage.json',JSON.stringify(coverage),{mode:0o600});
      }else throw Error('Unsupported evidence operation');
      send({jsonrpc:'2.0',id:request.id,result});
    }catch(error){if(request?.id!==undefined)send({jsonrpc:'2.0',id:request.id,error:{code:-32602,message:error.message}});}
  }
}
