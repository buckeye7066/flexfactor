import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sodium from 'libsodium-wrappers';
import { withMailboxGithub, githubKey } from '../test_support/mailbox-github.js';
import { dispatch, submitSteering, runStatus } from '../lib/service.js';
import { mobileWorkflow } from '../lib/workflow.js';
import { steeringSecretName } from '../lib/steering-mailbox.js';
import { pollOnce, submitToEngine } from '../../.github/scripts/mobile_steering_poll.mjs';

const REQUEST = { request_id:'4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8',
  repository:'owner/disposable',ref:'main',mode:'audit',provider:'auto',max_cost:1,threshold:90,max_iterations:1 };
const CLAIM = 'FLEXFACTOR_RUN_4D32C8E56F2B4A98A7F5';
const OWNER_TOKEN = 'owner-session-must-never-reach-the-runner';
const JOB_TOKEN = 'scoped-job-token-without-variables-permission';
const response = (status, value) => new Response(status === 204 ? null : JSON.stringify(value), {status});
function server() {
  const variables=new Map();
  const state={variables,run:null,dispatches:0};
  const fallback=async (url,options={}) => {
    const path=new URL(url).pathname.replace('/repos/'+REQUEST.repository,'');
    const method=options.method||'GET', body=options.body ? JSON.parse(options.body) : undefined;
    if(path==='/actions/variables' && method==='POST') {
      if(variables.has(body.name)) return response(422,{});
      variables.set(body.name,body.value);return response(201,{});
    }
    if(path.startsWith('/actions/variables/')) {
      const name=path.split('/').at(-1);
      if(method==='GET') return response(variables.has(name)?200:404,{value:variables.get(name)});
      if(method==='PATCH') {variables.set(name,body.value);return response(204);}
      if(method==='DELETE') {variables.delete(name);return response(204);}
    }
    if(path==='') return response(200,{default_branch:'main'});
    if(path==='/commits/main') return response(200,{sha:'a'.repeat(40)});
    if(path==='/actions/runs') return response(200,{workflow_runs:state.run?[state.run]:[]});
    if(path==='/actions/runs/99') return response(200,state.run);
    if(path==='/actions/runs/99/jobs') return response(200,{jobs:[]});
    if(path==='/contents/.github/workflows/flexfactor-mobile.yml')
      return response(200,{sha:'workflow',content:Buffer.from(mobileWorkflow()).toString('base64')});
    if(path==='/actions/workflows/flexfactor-mobile.yml/dispatches') {
      state.dispatches+=1;state.inputs=body.inputs;
      state.run={id:99,event:'workflow_dispatch',path:'.github/workflows/flexfactor-mobile.yml',
        status:'in_progress',display_title:`FlexFactor audit \u00b7 ${REQUEST.request_id}`,
        html_url:'https://github.com/'+REQUEST.repository+'/actions/runs/99'};
      return response(200,{workflow_run_id:99,html_url:state.run.html_url});
    }
    throw new Error('Unexpected test endpoint: '+method+' '+path);
  };
  state.fetch=withMailboxGithub(fallback,{variables});
  state.mailbox=state.fetch.mailbox;
  return state;
}
async function started() {
  const api=server();
  await dispatch(OWNER_TOKEN,REQUEST,{},api.fetch);
  return api;
}
function readerConfig(api, directory) {
  const claim=JSON.parse(api.variables.get(CLAIM));
  const sealed=api.mailbox.secrets.get(steeringSecretName(REQUEST.request_id));
  const privateKey=sodium.to_string(sodium.crypto_box_seal_open(
    Buffer.from(sealed.encrypted_value,'base64'),githubKey.publicKey,githubKey.privateKey));
  return {...claim.steering,request_id:REQUEST.request_id,repository:REQUEST.repository,
    privateKey,token:JOB_TOKEN,engineRoot:resolve(fileURLToPath(new URL('../../',import.meta.url))),
    targetPath:join(directory,'target'),steeringRoot:join(directory,'journal')};
}
function jobReader(api) {
  return async (url,options={}) => {
    assert.equal(options.headers.Authorization,`Bearer ${JOB_TOKEN}`);
    assert.ok(!String(url).includes('/actions/variables/'),'Job has no Variables permission');
    return api.fetch(url,options);
  };
}
function testDirectory(t) {
  const directory=mkdtempSync(join(tmpdir(),'flexfactor steering '));
  mkdirSync(join(directory,'target'));
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  return directory;
}
function journalRows(root) {
  return readdirSync(root,{recursive:true}).filter((name)=>name.endsWith('.jsonl'))
    .flatMap((name)=>readFileSync(join(root,name),'utf8').trim().split('\n').filter(Boolean).map(JSON.parse));
}
test('owner app to encrypted mailbox to real engine journal to terminal cleanup',async(t)=>{
  const api=await started(), directory=testDirectory(t), config=readerConfig(api,directory);
  const text='Private instruction: preserve the arithmetic API and strengthen regression tests.';
  assert.equal(api.inputs.steering_secret_name,steeringSecretName(REQUEST.request_id));
  assert.equal(JSON.stringify(api.inputs).includes(config.privateKey),false);
  assert.equal(JSON.stringify(api.inputs).includes(OWNER_TOKEN),false);
  await submitSteering(OWNER_TOKEN,REQUEST.repository,REQUEST.request_id,text,api.fetch);
  const release=api.mailbox.releases.get(config.release_id);
  const message=structuredClone(release.assets[0]);
  assert.equal(JSON.stringify(release).includes(text),false);
  assert.equal(JSON.stringify(api.mailbox.allCalls).includes(text),false);
  const ids=await pollOnce(config,jobReader(api),submitToEngine);
  assert.equal(ids.length,1);
  assert.equal(release.assets.length,0);
  let rows=journalRows(config.steeringRoot).filter((row)=>row.kind==='submission');
  assert.equal(rows.length,1);assert.equal(rows[0].comment,text);
  // Simulate a lost deletion response and a restarted reader, not a new run.
  release.assets.push(message);
  await pollOnce(config,jobReader(api),submitToEngine);
  rows=journalRows(config.steeringRoot).filter((row)=>row.kind==='submission');
  assert.equal(rows.length,1,'replayed transport asset duplicated the engine instruction');
  api.run.status='completed';api.run.conclusion='success';
  await runStatus(OWNER_TOKEN,REQUEST.repository,99,REQUEST.request_id,api.fetch);
  assert.equal(api.mailbox.releases.size,0);
  assert.equal(api.mailbox.secrets.size,0);
  assert.equal(api.variables.size,0);
  assert.equal(api.dispatches,1);
});
test('concurrent steering submissions retain both instructions',async(t)=>{
  const api=await started(), config=readerConfig(api,testDirectory(t));
  await Promise.all(['first instruction','second instruction'].map((text)=>
    submitSteering(OWNER_TOKEN,REQUEST.repository,REQUEST.request_id,text,api.fetch)));
  const received=[];
  await pollOnce(config,jobReader(api),async(message)=>received.push(message.comment));
  assert.deepEqual(received.sort(),['first instruction','second instruction']);
});
for(const scenario of ['published','changed owner','changed request','foreign asset','tampered message']) {
  test(`reader rejects ${scenario} without executing or deleting it`,async(t)=>{
    const api=await started(),config=readerConfig(api,testDirectory(t));
    await submitSteering(OWNER_TOKEN,REQUEST.repository,REQUEST.request_id,'Original instruction',api.fetch);
    const release=api.mailbox.releases.get(config.release_id), asset=release.assets[0];
    if(scenario==='published') release.draft=false;
    if(scenario==='changed owner') release.author.id=8;
    if(scenario==='changed request') release.body=release.body.replace(REQUEST.request_id,'b'.repeat(36));
    if(scenario==='foreign asset') asset.uploader.id=8;
    if(scenario==='tampered message') asset.data.ciphertext='A'.repeat(100);
    let submitted=false;
    await assert.rejects(pollOnce(config,jobReader(api),async()=>{submitted=true;}));
    assert.equal(submitted,false);assert.equal(release.assets.length,1);
    if(scenario!=='tampered message') {
      api.run.status='completed';api.run.conclusion='success';
      await assert.rejects(runStatus(OWNER_TOKEN,REQUEST.repository,99,REQUEST.request_id,api.fetch));
      assert.equal(api.mailbox.releases.size,1);assert.ok(api.variables.has(CLAIM));
    }
  });
}
test('malformed reader repository is rejected before any authenticated network call',async(t)=>{
  const api=await started(),config=readerConfig(api,testDirectory(t));
  config.repository='owner/../../unrelated';
  let contacted=false;
  await assert.rejects(pollOnce(config,async()=>{contacted=true;throw new Error('network');}));
  assert.equal(contacted,false);
});
test('an asset without a valid byte size cannot be delivered',async(t)=>{
  const api=await started(),config=readerConfig(api,testDirectory(t));
  await submitSteering(OWNER_TOKEN,REQUEST.repository,REQUEST.request_id,'Keep evidence gates.',api.fetch);
  delete api.mailbox.releases.get(config.release_id).assets[0].size;
  let delivered=false;
  await assert.rejects(pollOnce(config,jobReader(api),async()=>{delivered=true;}));
  assert.equal(delivered,false);
});
test('ambiguous draft creation preserves its claim and stale preparation recovers safely',async()=>{
  const api=server();let interrupted=false;
  api.mailbox.hook=async(call)=>{
    if(!interrupted && call.method==='POST' && call.path.endsWith('/releases')) {
      interrupted=true;
      api.mailbox.releases.set(600,{...call.body,id:600,author:{id:7},assets:[]});
      throw new Error('Response lost after GitHub created the private draft');
    }
  };
  await assert.rejects(dispatch(OWNER_TOKEN,REQUEST,{},api.fetch));
  assert.ok(api.variables.has(CLAIM));assert.equal(api.mailbox.releases.size,1);
  assert.equal(api.dispatches,0);
  const saved=JSON.parse(api.variables.get(CLAIM));
  saved.created_at=new Date(Date.now()-16*60*1000).toISOString();
  api.variables.set(CLAIM,JSON.stringify(saved));
  await dispatch(OWNER_TOKEN,REQUEST,{},api.fetch);
  assert.equal(api.mailbox.releases.has(600),false);
  assert.equal(api.mailbox.releases.size,1);assert.equal(api.mailbox.secrets.size,1);
  assert.equal(api.dispatches,1);
});
test('caller cannot inject an internal steering credential',async()=>{
  const api=server();
  await assert.rejects(dispatch(OWNER_TOKEN,REQUEST,{STEERING_KEY:{key_id:'fake',encrypted_value:'AAAA'}},api.fetch),
    (error)=>error.code==='invalid_secret_name');
  assert.equal(api.mailbox.releases.size,0);assert.equal(api.mailbox.secrets.size,0);
  assert.equal(api.dispatches,0);
});

test('concurrent capacity overflow remains drainable and terminal cleanup removes all pages',async(t)=>{
  const api=await started(),config=readerConfig(api,testDirectory(t));
  await submitSteering(OWNER_TOKEN,REQUEST.repository,REQUEST.request_id,'Original instruction',api.fetch);
  const release=api.mailbox.releases.get(config.release_id), seed=structuredClone(release.assets[0]);
  release.assets=Array.from({length:100},(_,i)=>({...seed,id:1000+i}));
  const delivered=await pollOnce(config,jobReader(api),async()=>{});
  assert.equal(delivered.length,100);assert.equal(release.assets.length,0);
  release.assets=Array.from({length:205},(_,i)=>({...seed,id:2000+i}));
  api.run.status='completed';api.run.conclusion='success';
  await runStatus(OWNER_TOKEN,REQUEST.repository,99,REQUEST.request_id,api.fetch);
  assert.equal(api.mailbox.releases.size,0);assert.equal(api.mailbox.secrets.size,0);
  assert.equal(api.variables.size,0);
});
test('a different collaborator is rejected before any mailbox write',async()=>{
  const api=await started();
  api.mailbox.hook=async(call)=>call.path==='/user'?response(200,{id:8,login:'collaborator'}):undefined;
  const offset=api.mailbox.allCalls.length;
  await assert.rejects(submitSteering('collaborator-session',REQUEST.repository,
    REQUEST.request_id,'Do not enqueue this',api.fetch),error=>error.code==='steering_owner_mismatch');
  assert.ok(api.mailbox.allCalls.slice(offset).every(call=>call.method==='GET'));
  assert.equal([...api.mailbox.releases.values()][0].assets.length,0);
});
