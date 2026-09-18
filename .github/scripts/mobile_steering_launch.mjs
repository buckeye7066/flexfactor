import { spawn } from 'node:child_process';
import { openSync, closeSync, existsSync, rmSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

// This launcher and its step shell must exit before target-controlled code runs.
// The persistent reader receives a single key on stdin with a clean exec environment.
export async function launchReader({cwd=process.cwd(), script=fileURLToPath(
  new URL('./mobile_steering_poll.mjs',import.meta.url)), env=process.env, timeoutMs=60000}={}) {
  const key=String(env.STEERING_PRIVATE_KEY || '').trim();
  if(!/^[A-Za-z0-9+/]{43}=$/.test(key)) throw new Error('Private steering key unavailable');
  const clean={...env};
  for(const name of Object.keys(clean)) if(['STEERING_PRIVATE_KEY','OPENAI_API_KEY',
    'ANTHROPIC_API_KEY','COPILOT_GITHUB_TOKEN'].includes(name.toUpperCase())) delete clean[name];
  const ready=join(cwd,'mobile-steering-ready');
  rmSync(ready,{force:true});
  const log=openSync(join(cwd,'mobile-steering.log'),'a',0o600);
  let child, failed=false;
  try {
    child=spawn(process.execPath,[script],{cwd,env:clean,detached:true,
      stdio:['pipe',log,log],windowsHide:true});
    child.on('error',()=>{failed=true;});
    child.stdin.on('error',()=>{failed=true;});
    child.stdin.end(key+'\n');
    const deadline=Date.now()+timeoutMs;
    while(!existsSync(ready) && !failed && child.exitCode===null && Date.now()<deadline) await delay(100);
    if(!existsSync(ready) || failed || !child.pid || child.exitCode!==null)
      throw new Error('Private steering reader did not initialize');
    writeFileSync(join(cwd,'mobile-steering.pid'),String(child.pid)+'\n',{mode:0o600});
    child.unref();
    return child.pid;
  } catch(error) {
    if(child?.pid) try{child.kill();}catch{}
    throw error;
  } finally {closeSync(log);}
}
if(process.argv[1] && resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  launchReader().then(()=>console.log('Private steering reader initialized through short-lived handoff.'))
    .catch(()=>{console.error('Private steering initialization failed.');process.exitCode=1;});
}
