// Preserve JSONL message boundaries when CLI output and wrapper events share stdout.
// Never close the destination: final coverage/error/exit events still need it.
export function createEventOutput(destination, {maxBytes=2_000_000}={}) {
  let tail=Promise.resolve();
  let destinationError;
  destination.on('error',error=>{destinationError=error;});
  const failure=code=>Object.assign(new Error(code),{code});
  const write=line=>{
    tail=tail.then(()=>new Promise((resolve,reject)=>{
      if(destinationError) {reject(destinationError);return;}
      destination.write(Buffer.concat([line,Buffer.from('\n')]),error=>error?reject(error):resolve());
    }));
    return tail;
  };
  async function forward(source) {
    let pieces=[],bytes=0;
    async function complete(final=false) {
      const line=Buffer.concat(pieces,bytes); pieces=[];bytes=0;
      if(!line.length)return;
      try {
        const text=new TextDecoder('utf-8',{fatal:true}).decode(line);
        if(!text.trim())return;
        const event=JSON.parse(text);
        if(!event||typeof event!=='object'||Array.isArray(event))throw Error();
      } catch {
        throw failure(final?'cli_stream_truncated':'cli_stream_malformed');
      }
      await write(line);
    }
    for await(const chunk of source) {
      const buffer=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);
      let offset=0;
      while(offset<buffer.length) {
        const newline=buffer.indexOf(10,offset);
        const end=newline<0?buffer.length:newline;
        const part=buffer.subarray(offset,end);
        bytes+=part.length;
        if(bytes>maxBytes)throw failure('cli_stream_too_large');
        pieces.push(part);
        offset=end+1;
        if(newline>=0)await complete();
      }
    }
    await complete(true);
  }
  return {
    emit(event) {
      const line=Buffer.from(JSON.stringify(event));
      if(line.length>maxBytes)return Promise.reject(failure('cli_stream_too_large'));
      return write(line);
    },
    forward,
    flush:()=>tail,
  };
}
