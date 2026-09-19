import { rm } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { existsSync, mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const moduleUrl=new URL('../../.github/scripts/mobile_steering_launch.mjs',import.meta.url);
test('the persistent reader receives its private key by pipe, never argv or environment',async(t)=>{
  assert.ok(existsSync(moduleUrl),'Short-lived key handoff is required');
  const {launchReader}=await import(moduleUrl.href);
  const directory=mkdtempSync(join(tmpdir(),'ff-handoff-'));
  const script=join(directory,'reader.mjs');
  const secret=Buffer.alloc(32,11).toString('base64');
  writeFileSync(script,`import {writeFileSync} from 'node:fs';
let input=''; for await(const chunk of process.stdin) input+=chunk;
writeFileSync('observed.json',JSON.stringify({keyPresent:'STEERING_PRIVATE_KEY' in process.env,
  argvHasKey:process.argv.some(arg=>arg.includes(input.trim())),received:input.trim().length===44}));
writeFileSync('mobile-steering-ready','ready');setInterval(()=>{},1000);`);
  let pid;
  t.after(async()=>{if(pid)try{process.kill(pid);}catch{};await new Promise(resolve=>setTimeout(resolve,200));await rm(directory,{recursive:true,force:true,maxRetries:10,retryDelay:100});});
  pid=await launchReader({cwd:directory,script,env:{...process.env,STEERING_PRIVATE_KEY:secret},timeoutMs:5000});
  assert.deepEqual(JSON.parse(readFileSync(join(directory,'observed.json'),'utf8')),
    {keyPresent:false,argvHasKey:false,received:true});
  if(process.platform==='linux') assert.equal(readFileSync(`/proc/${pid}/environ`).includes(Buffer.from(secret)),false);
  const workflow=readFileSync(new URL('../../.github/workflows/mobile-run.yml',import.meta.url),'utf8');
  const run=workflow.split('- name: Run FlexFactor')[1].split('- name: Emit bounded')[0];
  assert.ok(!run.includes('STEERING_PRIVATE_KEY:'),'Target execution shell must not inherit the key');
});
