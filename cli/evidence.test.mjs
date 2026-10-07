import test from 'node:test';
import assert from 'node:assert/strict';
import {artifacts,readEvidence,missingEvidence} from './evidence.mjs';
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
