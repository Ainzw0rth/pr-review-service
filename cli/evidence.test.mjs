import test from 'node:test';
import assert from 'node:assert/strict';
import {artifacts,readEvidence} from './evidence.mjs';
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
