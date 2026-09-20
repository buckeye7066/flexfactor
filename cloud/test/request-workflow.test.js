import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createHash} from 'node:crypto';
import {mobileWorkflow} from '../lib/workflow.js';
import {dispatch, runStatus} from '../lib/service.js';
import {withMailboxGithub} from '../test_support/mailbox-github.js';
const ID='4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8';
const REPO='owner/project';
const REF='flexfactor-run-'+ID;
const CLAIM='FLEXFACTOR_RUN_4D32C8E56F2B4A98A7F5';
const REQUEST={request_id:ID,repository:REPO,ref:'main',mode:'scout',provider:'auto',max_cost:1,threshold:90,max_iterations:1};
const response=(status,value)=>new Response(status===204?null:JSON.stringify(value),{status});
const digest=value=>createHash('sha1').update(JSON.stringify(value)).digest('hex');
function fixture(){
 const state={variables:new Map(),refs:new Map(),trees:new Map(),commits:new Map(),calls:[],run:null,hook:null};
 const fallback=async(url,options={})=>{
  const path=decodeURIComponent(new URL(url).pathname).replace('/repos/'+REPO,'');
  const method=options.method||'GET',body=options.body?JSON.parse(options.body):undefined;
  const call={path,method,body};state.calls.push(call);
  const overridden=await state.hook?.(call);if(overridden)return overridden;
  if(path==='/actions/variables'&&method==='POST'){
   if(state.variables.has(body.name))return response(422,{});
   state.variables.set(body.name,body.value);return response(201,{});
  }
  if(path.startsWith('/actions/variables/')){
   const key=path.split('/').at(-1);
   if(method==='GET')return response(state.variables.has(key)?200:404,{value:state.variables.get(key)});
   if(method==='PATCH'){state.variables.set(key,body.value);return response(204);}
   if(method==='DELETE'){state.variables.delete(key);return response(204);}
  }
  if(path==='')return response(200,{default_branch:'main'});
  if(path==='/commits/main')return response(200,{sha:'a'.repeat(40)});
  if(path==='/actions/runs')return response(200,{workflow_runs:state.run?[state.run]:[]});
  if(path==='/contents/.github/workflows/flexfactor-mobile.yml')return response(200,{sha:'b'.repeat(40),content:Buffer.from(mobileWorkflow()).toString('base64')});
  if(path==='/git/trees'&&method==='POST'){
   const sha=digest(body);state.trees.set(sha,body);return response(201,{sha});
  }
  if(path==='/git/commits'&&method==='POST'){
   const sha=digest(body);state.commits.set(sha,body);return response(201,{sha,tree:{sha:body.tree},parents:body.parents});
  }
  if(path==='/git/refs'&&method==='POST'){
   if(state.refs.has(body.ref))return response(422,{});
   state.refs.set(body.ref,body.sha);return response(201,{ref:body.ref,object:{sha:body.sha}});
  }
  if(path.startsWith('/git/ref/tags/')&&method==='GET'){
   const ref='refs/tags/'+path.slice('/git/ref/tags/'.length);
   return response(state.refs.has(ref)?200:404,{ref,object:{sha:state.refs.get(ref)}});
  }
  if(path.startsWith('/git/refs/tags/')&&method==='DELETE'){
   state.refs.delete('refs/tags/'+path.slice('/git/refs/tags/'.length));return response(204);
  }
  if(path==='/actions/workflows/flexfactor-mobile.yml/dispatches'){
   state.run={id:99,event:'workflow_dispatch',path:'.github/workflows/flexfactor-mobile.yml',status:'in_progress',head_branch:body.ref,display_title:`FlexFactor scout \u00b7 ${ID}`,html_url:`https://github.com/${REPO}/actions/runs/99`};
   return response(200,{workflow_run_id:99,html_url:state.run.html_url});
  }
  if(path==='/actions/runs/99')return response(200,state.run);
  if(path==='/actions/runs/99/jobs')return response(200,{jobs:[]});
  throw new Error(`Unexpected synthetic GitHub endpoint ${method} ${path}`);
 };
 state.fetch=withMailboxGithub(fallback,{variables:state.variables,requestWorkflows:false});return state;
}

test('registration and scoped callers never dynamically index the secret context',()=>{
 for(const text of [mobileWorkflow(),mobileWorkflow(ID)]){
  assert.doesNotMatch(text,/secrets\s*\[|toJSON\s*\(\s*secrets|secrets:\s*inherit/i);
 }
});
test('a scoped caller binds only its exact named credentials and canonical provider fallbacks',()=>{
 const text=mobileWorkflow(ID);
 const names=[...text.matchAll(/secrets\.([A-Z0-9_]+)/g)].map(match=>match[1]);
 assert.deepEqual([...new Set(names)].sort(),[
  'FLEXFACTOR_4D32C8E56F2B4A98A7F599594C49B2F8_STEERING_KEY',
  'FLEXFACTOR_4D32C8E56F2B4A98A7F599594C49B2F8_OPENAI_API_KEY',
  'FLEXFACTOR_4D32C8E56F2B4A98A7F599594C49B2F8_ANTHROPIC_API_KEY',
  'OPENAI_API_KEY','ANTHROPIC_API_KEY'].sort());
 assert.equal(mobileWorkflow(ID.toUpperCase()),text);
});
test('unbound registration caller references no secret and invalid identities cannot generate YAML',()=>{
 assert.doesNotMatch(mobileWorkflow(),/secrets\./);
 for(const id of ['main','../other','\nanything','',null])assert.throws(()=>mobileWorkflow(id));
});
test('public dispatch creates only a caller tree and uses its immutable request tag',async()=>{
 const api=fixture();await dispatch('fixture-token',REQUEST,{},api.fetch);
 const call=api.calls.find(item=>item.path.endsWith('/dispatches'));
 assert.equal(call.body.ref,REF);assert.equal(call.body.inputs.target_ref,'main');
 assert.equal(api.refs.size,1);
 const sha=api.refs.get('refs/tags/'+REF),commit=api.commits.get(sha),tree=api.trees.get(commit.tree);
 assert.deepEqual(commit.parents,[]);
 assert.deepEqual(tree.tree.map(row=>row.path),['.github/workflows/flexfactor-mobile.yml']);
 assert.equal(tree.tree[0].content,mobileWorkflow(ID));
 assert.deepEqual(JSON.parse(api.variables.get(CLAIM)).workflow,{ref:REF,sha});
 assert.ok(!api.calls.some(item=>item.path.startsWith('/contents/')&&item.method==='PUT'));
});
test('terminal cleanup removes only the request-owned tag',async()=>{
 const api=fixture();await dispatch('fixture-token',REQUEST,{},api.fetch);
 assert.ok(api.refs.has('refs/tags/'+REF));api.refs.set('refs/tags/user-release','f'.repeat(40));
 api.run.status='completed';api.run.conclusion='success';
 await runStatus('fixture-token',REPO,99,ID,api.fetch);
 assert.equal(api.refs.has('refs/tags/'+REF),false);
 assert.equal(api.refs.get('refs/tags/user-release'),'f'.repeat(40));
 assert.equal(api.variables.has(CLAIM),false);
});
test('changed tag identity is preserved instead of deleting another writer work',async()=>{
 const api=fixture();await dispatch('fixture-token',REQUEST,{},api.fetch);
 assert.ok(api.refs.has('refs/tags/'+REF));api.refs.set('refs/tags/'+REF,'f'.repeat(40));
 api.run.status='completed';
 await assert.rejects(runStatus('fixture-token',REPO,99,ID,api.fetch),/changed|identity/i);
 assert.equal(api.refs.get('refs/tags/'+REF),'f'.repeat(40));assert.ok(api.variables.has(CLAIM));
});
test('pre-existing unrelated request tag is never overwritten or dispatched',async()=>{
 const api=fixture();api.refs.set('refs/tags/'+REF,'f'.repeat(40));
 await assert.rejects(dispatch('fixture-token',REQUEST,{},api.fetch),/exist|conflict|reserved/i);
 assert.equal(api.refs.get('refs/tags/'+REF),'f'.repeat(40));
 assert.equal(api.calls.some(call=>call.path.endsWith('/dispatches')),false);
});
test('ambiguous dispatch retains the tag and claim rather than deleting live inputs',async()=>{
 const api=fixture();api.hook=call=>call.path.endsWith('/dispatches')?response(500,{}):null;
 await assert.rejects(dispatch('fixture-token',REQUEST,{},api.fetch));
 assert.ok(api.refs.has('refs/tags/'+REF));assert.ok(JSON.parse(api.variables.get(CLAIM)).workflow);
});
test('a definite pre-dispatch failure rolls back its new request tag',async()=>{
 const api=fixture();let attempted=false;
 api.hook=call=>{if(call.path.endsWith('/dispatches')){attempted=true;return response(401,{})}return null};
 await assert.rejects(dispatch('fixture-token',REQUEST,{},api.fetch));
 assert.ok(attempted);assert.equal(api.refs.has('refs/tags/'+REF),false);assert.equal(api.variables.has(CLAIM),false);
 assert.ok(api.calls.some(call=>call.method==='DELETE'&&call.path==='/git/refs/tags/'+REF));
});

test('an existing request tag is rejected before any credential or claim mutation',async()=>{
 const api=fixture();api.refs.set('refs/tags/'+REF,'f'.repeat(40));
 await assert.rejects(dispatch('fixture-token',REQUEST,{},api.fetch));
 assert.equal(api.fetch.mailbox.allCalls.filter(call=>call.method!=='GET').length,0);
});
