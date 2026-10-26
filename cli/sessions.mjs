// Only native conversation files persist. Auth/config/runtime state stays private per run.
import {mkdir,readdir,symlink,stat} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';

const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);

async function historyExists(kind,directory,id) {
  try {
    if(kind==='claude_cli')return (await stat(join(directory,'-tmp-review',id+'.jsonl'))).size>0;
    // Codex rebuilds its private index from persisted rollout files on resume.
    for(const entry of await readdir(directory,{recursive:true,withFileTypes:true})) {
      if(entry.isFile()&&entry.name.endsWith('-'+id+'.jsonl')&&
         (await stat(join(entry.parentPath,entry.name))).size>0)return true;
    }
    return false;
  } catch(error) {if(error.code==='ENOENT')return false;throw error;}
}

export async function prepareSession(kind,session,{root='/auth/review-sessions',home,onRelocated=()=>{}}={}) {
  if(!session)return null;
  if(!uuid(session.key)||(session.id!==null&&!uuid(session.id)))throw Error('invalid_session');
  home??=kind==='codex_cli'?'/tmp/codex':'/tmp/claude';
  const folder=kind==='codex_cli'?'sessions':'projects';
  let key=session.key,id=session.id;
  // Missing remote/local history is a new namespace, never the old session silently reused.
  if(id&&!await historyExists(kind,join(root,key,folder),id)) {
    key=randomUUID();
    id=null;
  }
  const directory=join(root,key,folder);
  await mkdir(directory,{recursive:true,mode:0o700});
  await mkdir(home,{recursive:true,mode:0o700});
  await symlink(directory,join(home,folder),'dir');
  if(key!==session.key)onRelocated({previous_key:session.key,key});
  return id;
}
