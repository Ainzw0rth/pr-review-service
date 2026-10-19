import test from 'node:test';
import assert from 'node:assert/strict';
import {artifacts,readEvidence,missingEvidence,evidenceManifest,toolRead} from './evidence.mjs';

test('compact index preserves evidence identities, inline text and aliases',()=>{
 const file={path:'a.py',content:'x'.repeat(1500)};
 const entries=artifacts({pr:{title:'Fix "login"\nflow'},files:[file],sources:[file],issues:[]});
 const {index,inline,coverage}=evidenceManifest(entries);
 assert.deepEqual(index.columns,['id','title','characters','alias_of']);
 assert.deepEqual(index.rows.map(row=>row[0]),['pr','files_0','sources_0']);
 assert.equal(index.rows[2][3],'files_0');
 assert.equal(inline.pr,JSON.stringify({title:'Fix "login"\nflow'}));
 assert.deepEqual(missingEvidence(entries,coverage),['files_0','sources_0']);
 readEvidence(entries,coverage,{reads:[{id:'files_0'}]});
 assert.deepEqual(missingEvidence(entries,coverage),[]);
 const legacy=entries.map(item=>({id:item.id,title:item.title,characters:item.text.length,...(item.canonical_id?{alias_of:item.canonical_id}:{})}));
 assert.ok(JSON.stringify(index).length<JSON.stringify(legacy).length);
});

test('continuation hints reconstruct the full original evidence without granting early coverage',()=>{
 const entries=artifacts({source:'quoted "text"\n'.repeat(5000)}),coverage={};
 let read={id:'source',length:8000},content='';
 while(read){
   const result=readEvidence(entries,coverage,{reads:[read]})[0];
   assert.ok(Buffer.byteLength(JSON.stringify([result]),'utf8')<=24000);
   content+=result.content;
   read=result.next_read;
   if(read) {
     assert.ok(read.length<=8000);
     assert.deepEqual(missingEvidence(entries,coverage),['source']);
   }
 }
 assert.equal(content,entries[0].text);
 assert.deepEqual(missingEvidence(entries,coverage),[]);
});

test('invalid batches return actionable errors without partial coverage or echoed input',()=>{
 const entries=artifacts({source:'abc'}),coverage={};
 const result=toolRead(entries,coverage,{reads:[{id:'source'},{id:'/auth/private-token'}]});
 assert.equal(result.isError,true);
 const error=JSON.parse(result.content[0].text).error;
 assert.equal(error.code,'UNKNOWN_ARTIFACT');
 assert.ok(error.hint.includes('manifest'));
 assert.ok(!JSON.stringify(result).includes('private-token'));
 assert.deepEqual(coverage,{});
 for(const args of [{reads:[{id:'source',filter:'invented'}]},{reads:[null]},{reads:[{id:'source'}],invented:true}]) {
   assert.equal(toolRead(entries,coverage,args).isError,true);
   assert.deepEqual(coverage,{});
 }
 const success=toolRead(entries,coverage,{reads:[{id:'source'}]});
 assert.equal(success.isError,undefined);
 assert.equal(JSON.parse(success.content[0].text)[0].content,'"abc"');
});
test('evidence round-trip and complete-range accounting',()=>{
 const entries=artifacts({sources:[{path:'a.py',content:'secret project source'}]});
 const coverage={};
 const a=readEvidence(entries,coverage,{reads:[{id:'sources_0',offset:0,length:10}]})[0];
 const b=readEvidence(entries,coverage,{reads:[{id:'sources_0',offset:10}]})[0];
 assert.equal(a.content+b.content,JSON.stringify({path:'a.py',content:'secret project source'}));
 assert.deepEqual(coverage.sources_0,[[0,entries[0].text.length]]);
});
test('arbitrary paths, oversized ranges and gaps cannot count as covered',()=>{
 const entries=artifacts({source:'abcdefghijk'});const coverage={};
 assert.throws(()=>readEvidence(entries,coverage,{reads:[{id:'/auth/auth.json'}]}));
 assert.throws(()=>readEvidence(entries,coverage,{reads:[{id:'source',length:999999}]}));
 readEvidence(entries,coverage,{reads:[{id:'source',offset:4,length:2}]});
 assert.deepEqual(coverage.source,[[4,6]]);
});

test('recovery identifies omitted and partially read entries without granting coverage',()=>{
 const entries=artifacts({files:[{path:'a.py',text:'a'.repeat(100)},{path:'b.py',text:'b'.repeat(100)}]});
 const coverage={};
 readEvidence(entries,coverage,{reads:[{id:'files_0',offset:0,length:10}]});
 assert.deepEqual(missingEvidence(entries,coverage),['files_0','files_1']);
 readEvidence(entries,coverage,{reads:[{id:'files_0',offset:10}]});
 assert.deepEqual(missingEvidence(entries,coverage),['files_1']);
 assert.deepEqual(missingEvidence(entries,{}),['files_0','files_1']);
});

test('identical artifacts share reads without losing their original identities',()=>{
 const file={path:'docs/rules.md',content:'x'.repeat(1200)};
 const entries=artifacts({sources:[file],docs:[file]});
 const coverage={};
 assert.equal(entries[1].canonical_id,'sources_0');
 readEvidence(entries,coverage,{reads:[{id:'sources_0'}]});
 assert.deepEqual(missingEvidence(entries,coverage),[]);
 const changed=artifacts({sources:[file],docs:[{...file,content:'changed agreement'}]});
 assert.equal(changed[1].canonical_id,undefined);
 assert.deepEqual(missingEvidence(changed,coverage),['docs_0']);
});
test('small artifacts batch efficiently while total output remains bounded',()=>{
 const entries=artifacts({files:Array.from({length:8},(_,n)=>({path:`${n}.py`,content:'small'}))});
 assert.equal(readEvidence(entries,{}, {reads:entries.map(x=>({id:x.id}))}).length,8);
 const big=artifacts({files:Array.from({length:4},(_,n)=>({path:`${n}.py`,content:'x'.repeat(20000)}))});
 assert.throws(()=>readEvidence(big,{}, {reads:big.map(x=>({id:x.id}))}));
});

test('escaped and non-ASCII replies exceed byte bounds without granting coverage',()=>{
 for(const source of ['"'.repeat(10000),'界'.repeat(9000)]) {
   const entries=artifacts({source}),coverage={};
   const result=toolRead(entries,coverage,{reads:[{id:'source'}]});
   assert.equal(result.isError,true);
   const error=JSON.parse(result.content[0].text).error;
   assert.equal(error.code,'REPLY_TOO_LARGE');
   assert.match(error.hint,/split/i);
   assert.match(error.hint,/smaller|reduce/i);
   assert.deepEqual(coverage,{});
   let read={id:'source',length:4000},content='';
   while(read) {
     const reply=toolRead(entries,coverage,{reads:[read]});
     assert.equal(reply.isError,undefined);
     assert.ok(Buffer.byteLength(reply.content[0].text,'utf8')<=24000);
     const [part]=JSON.parse(reply.content[0].text);
     content+=part.content;
     read=part.next_read;
   }
   assert.equal(content,entries[0].text);
   assert.deepEqual(missingEvidence(entries,coverage),[]);
 }
});

test('late-batch reply overflow preserves previous coverage and accepts split retries',()=>{
 const entries=artifacts({files:[{path:'a.py',content:'a'.repeat(14000)},{path:'b.py',content:'b'.repeat(14000)}]}),coverage={};
 readEvidence(entries,coverage,{reads:[{id:'files_0',length:10}]});
 const before=structuredClone(coverage);
 const result=toolRead(entries,coverage,{reads:[{id:'files_0',offset:10},{id:'files_1'}]});
 assert.equal(result.isError,true);
 assert.equal(JSON.parse(result.content[0].text).error.code,'REPLY_TOO_LARGE');
 assert.deepEqual(coverage,before);
 assert.deepEqual(missingEvidence(entries,coverage),['files_0','files_1']);
 for(const read of [{id:'files_0',offset:10},{id:'files_1'}]) {
   const reply=toolRead(entries,coverage,{reads:[read]});
   assert.equal(reply.isError,undefined);
   assert.ok(Buffer.byteLength(reply.content[0].text,'utf8')<=24000);
 }
 assert.deepEqual(missingEvidence(entries,coverage),[]);
});
