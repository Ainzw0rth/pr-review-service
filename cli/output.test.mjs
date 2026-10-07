import test from 'node:test';
import assert from 'node:assert/strict';
import {PassThrough, Writable, Readable} from 'node:stream';
import {setImmediate as tick} from 'node:timers/promises';
import {createEventOutput} from './output.mjs';

function fixture(options={}) {
  const chunks=[];
  const sink=new Writable({write(chunk,encoding,done){chunks.push(Buffer.from(chunk));done();}});
  const output=createEventOutput(sink,options);
  return {sink,output,text:()=>Buffer.concat(chunks).toString('utf8'),events:()=>Buffer.concat(chunks).toString('utf8').trim().split('\n').filter(Boolean).map(JSON.parse)};
}

test('progress cannot split a large child event, and child EOF does not close shared output',async()=>{
  const f=fixture(); const child=new PassThrough();
  const forwarding=f.output.forward(child);
  const event={type:'item.completed',payload:'x'.repeat(90000)};
  const raw=Buffer.from(JSON.stringify(event)+'\n');
  child.write(raw.subarray(0,65536)); await tick();
  await f.output.emit({type:'runner_coverage',read:22});
  child.end(raw.subarray(65536)); await forwarding;
  await f.output.emit({type:'runner_exit',code:0}); await f.output.flush();
  assert.deepEqual(f.events(),[{type:'runner_coverage',read:22},event,{type:'runner_exit',code:0}]);
  assert.equal(f.sink.writableEnded,false);
});

test('UTF-8 survives one-byte chunks; handles CRLF, blank lines, and final JSON without newline',async()=>{
  const f=fixture(); const event={type:'message',text:'Jakarta 🦀 日本語'};
  const raw=Buffer.from('\n'+JSON.stringify(event)+'\r\n'+JSON.stringify(event));
  await f.output.forward(Readable.from([...raw].map(byte=>Buffer.from([byte]))));
  assert.deepEqual(f.events(),[event,event]);
});

test('malformed JSON and truncated EOF fail explicitly without forwarding raw contents',async()=>{
  for(const [raw,code] of [['private secret\n','cli_stream_malformed'],['{"secret":','cli_stream_truncated']]) {
    const f=fixture();
    await assert.rejects(f.output.forward(Readable.from([Buffer.from(raw)])),e=>e.code===code&&!e.message.includes('secret'));
    await f.output.emit({type:'runner_error',code});
    assert.deepEqual(f.events(),[{type:'runner_error',code}]);
  }
});

test('line byte limit applies with and without newline, including UTF-8 bytes',async()=>{
  for(const raw of ['{"x":"'+'a'.repeat(80)+'"}\n','a'.repeat(80),JSON.stringify({x:'🦀'.repeat(20)})]) {
    const f=fixture({maxBytes:64});
    await assert.rejects(f.output.forward(Readable.from([Buffer.from(raw)])),{code:'cli_stream_too_large'});
    assert.equal(f.text(),'');
  }
});

test('slow output respects backpressure and serializes writes',async()=>{
  const lines=[]; let inFlight=0,maxInFlight=0;
  const sink=new Writable({highWaterMark:1,write(chunk,encoding,done){
    inFlight++; maxInFlight=Math.max(maxInFlight,inFlight);
    setImmediate(()=>{lines.push(chunk.toString());inFlight--;done();});
  }});
  const output=createEventOutput(sink);
  await Promise.all([output.forward(Readable.from([Buffer.from('{"type":"a"}\n{"type":"b"}\n')])),output.emit({type:'progress'})]);
  await output.emit({type:'runner_exit',code:0}); await output.flush();
  assert.equal(maxInFlight,1);
  const parsed=lines.join('').trim().split('\n').map(JSON.parse);
  assert.deepEqual(parsed.filter(x=>x.type!=='progress'),[{type:'a'},{type:'b'},{type:'runner_exit',code:0}]);
});

test('destination errors reject the writer instead of hanging',async()=>{
  const sink=new Writable({write(chunk,encoding,done){done(new Error('closed'));}});
  const output=createEventOutput(sink);
  await assert.rejects(output.emit({type:'test'}));
  await assert.rejects(output.flush());
});
