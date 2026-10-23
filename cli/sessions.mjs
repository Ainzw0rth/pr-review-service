// Only native conversation files persist. Auth/config/runtime state stays private per run.
import {mkdir,readdir,symlink,stat} from 'node:fs/promises';
import {join} from 'node:path';

const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);

export async function prepareSession(kind,session,{root='/auth/review-sessions',home}={}) {
  if(!session)return null;
  if(!uuid(session.key)||(session.id!==null&&!uuid(session.id)))throw Error('invalid_session');
  home??=kind==='codex_cli'?'/tmp/codex':'/tmp/claude';
  const folder=kind==='codex_cli'?'sessions':'projects';
  const directory=join(root,session.key,folder);
  await mkdir(directory,{recursive:true,mode:0o700});
  await mkdir(home,{recursive:true,mode:0o700});
  await symlink(directory,join(home,folder),'dir');
  if(!session.id)return null;
  if(kind==='claude_cli') {
    try {return (await stat(join(directory,'-tmp-review',session.id+'.jsonl'))).size>0?session.id:null;}
    catch(error) {if(error.code==='ENOENT')return null;throw error;}
  }
  // Codex rebuilds its private index from persisted rollout files on resume.
  for(const entry of await readdir(directory,{recursive:true,withFileTypes:true})) {
    if(entry.isFile()&&entry.name.endsWith('-'+session.id+'.jsonl')&&
       (await stat(join(entry.parentPath,entry.name))).size>0)return session.id;
  }
  return null;
}
