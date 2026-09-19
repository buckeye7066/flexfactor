import test from 'node:test'
import assert from 'node:assert/strict'
import {EventEmitter} from 'node:events'
import {PassThrough,Writable} from 'node:stream'
import {runCodexSession} from '../providers/owner_ai/codexAppServer.mjs'
const base={system:'Trusted scientific-honesty rules.',prompt:'Untrusted task text.',format:'text',maxTokens:10,timeoutMs:1000}
function protocol({account='chatgpt',model='gpt-6-astra',tool=false,stall=false,reroute=false,reasoning=false}={}) {
  const requests=[];const launches=[];let child
  const spawnImpl=(_exe,args,options)=>{
    launches.push({args,options})
    child=new EventEmitter();child.pid=undefined;child.stdout=new PassThrough();child.stderr=new PassThrough();child.killed=false
    child.kill=()=>{if(!child.killed){child.killed=true;queueMicrotask(()=>child.emit('close',0,null))}return true}
    const emit=value=>child.stdout.write(JSON.stringify(value)+'\n')
    child.stdin=new Writable({write(chunk,_encoding,done){for(const line of chunk.toString().trim().split('\n')){const r=JSON.parse(line);requests.push(r);queueMicrotask(()=>{
      if(r.method==='initialize')emit({id:r.id,result:{userAgent:'fixture'}})
      if(r.method==='account/read')emit({id:r.id,result:{account:{type:account},requiresOpenaiAuth:true}})
      if(r.method==='thread/start')emit({id:r.id,result:{thread:{id:'thread-fixture',ephemeral:true},model,modelProvider:'openai',approvalPolicy:'never',sandbox:{type:'readOnly',networkAccess:false},instructionSources:[]}})
      if(r.method==='turn/start'){
        emit({id:r.id,result:{turn:{id:'turn-fixture',status:'inProgress',items:[]}}})
        if(stall)return
        if(reroute)emit({method:'model/rerouted',params:{threadId:'thread-fixture',turnId:'turn-fixture',fromModel:model,toModel:'another-model'}})
        if(reasoning){for(const type of ['item/started','item/updated','item/completed'])emit({method:type,params:{threadId:'thread-fixture',turnId:'turn-fixture',item:{id:'thinking',type:'reasoning',text:'not user output'}}})}
        const item=tool?{id:'tool',type:'commandExecution',command:'not permitted'}:{id:'answer',type:'agentMessage',phase:'final_answer',text:'Complete answer'}
        emit({method:'item/started',params:{threadId:'thread-fixture',turnId:'turn-fixture',item}})
        emit({method:'item/completed',params:{threadId:'thread-fixture',turnId:'turn-fixture',item}})
        emit({method:'thread/tokenUsage/updated',params:{threadId:'thread-fixture',turnId:'turn-fixture',tokenUsage:{last:{inputTokens:20,cachedInputTokens:0,outputTokens:30,reasoningOutputTokens:20}}}})
        emit({method:'turn/completed',params:{threadId:'thread-fixture',turn:{id:'turn-fixture',status:'completed',items:[item],error:null}}})
      }
    })}done()},final(done){done();child.kill()}})
    return child
  }
  return {requests,launches,spawnImpl,get child(){return child}}
}
const opts=fixture=>({env:{CODEX_HOME:process.cwd()+'/fixture-empty-home',PATH:process.env.PATH},cwd:process.cwd(),model:'gpt-6-astra',features:['shell_tool','unified_exec'],spawnImpl:fixture.spawnImpl,platform:'linux'})
test('privileged instructions and actual selected model remain separate from user text',async()=>{
 const fixture=protocol();const answer=await runCodexSession(base,opts(fixture))
 assert.equal(answer?.model,'gpt-6-astra');assert.equal(answer?.model_source,'app_server_configuration')
 assert.equal(answer?.raw,'Complete answer');assert.equal(answer?.usage.output_tokens,30)
 const thread=fixture.requests.find(r=>r.method==='thread/start');const turn=fixture.requests.find(r=>r.method==='turn/start')
 assert.equal(thread.params.developerInstructions,base.system)
 assert.equal(turn.params.input[0].text.includes(base.prompt),true)
 assert.equal(turn.params.input[0].text.includes(base.system),false)
 assert.equal(thread.params.ephemeral,true);assert.equal(thread.params.sandbox,'read-only')
 assert.equal(fixture.child.killed,true)
})
for(const [name,settings]of [['API authentication',{account:'apiKey'}],['different selected model',{model:'another-model'}],['unexpected tool execution',{tool:true}],['model reroute',{reroute:true}]]){
 test('rejects '+name+' without reporting a valid subscription result',async()=>{
  const fixture=protocol(settings);assert.equal(await runCodexSession(base,opts(fixture)),null)
  if(settings.account||settings.model)assert.equal(fixture.requests.some(r=>r.method==='turn/start'),false)
 })
}
test('pre-cancelled request starts no child',async()=>{
 const fixture=protocol();assert.equal(await runCodexSession(base,{...opts(fixture),signal:AbortSignal.abort()}),null)
 assert.equal(fixture.child,undefined)
})
test('deadline ends an unresponsive worker and yields no success',async()=>{
 const fixture=protocol({stall:true});assert.equal(await runCodexSession({...base,timeoutMs:20},opts(fixture)),null)
 assert.equal(fixture.child.killed,true)
})

import {childEnvironment,providerExecutable} from '../providers/owner_ai/officialCli.mjs'
test('an explicit absolute Codex home works without Windows environment',()=>{assert.equal(childEnvironment('codex',{OWNER_AI_CODEX_HOME:'/private/codex',HOME:'/private'}).CODEX_HOME,'/private/codex');assert.equal(providerExecutable('codex','linux'),'codex');assert.equal(providerExecutable('codex','win32'),'codex.exe')})

test('reasoning lifecycle is permitted but never included as the completed answer',async()=>{const f=protocol({reasoning:true});const r=await runCodexSession(base,opts(f));assert.equal(r.raw,'Complete answer');assert.equal(r.raw.includes('not user output'),false)})

import {parseResult} from '../providers/owner_ai/officialCli.mjs'
test('legacy JSONL without execution model metadata is never promoted to a proven model receipt',()=>{const rows=[{type:'thread.started'},{type:'turn.started'},{type:'item.completed',item:{type:'agent_message',text:'answer'}},{type:'turn.completed',usage:{input_tokens:1,cached_input_tokens:0,output_tokens:2}}];assert.equal(parseResult('codex',rows.map(x=>JSON.stringify(x)).join('\n'),'gpt-6-astra'),null)})

import {probeProvider,executeJob} from '../providers/owner_ai/officialCli.mjs'
const requiredHelp='--sandbox --ephemeral --ignore-user-config --strict-config --skip-git-repo-check --json --model --config --disable'
const supportedFeatures='shell_tool stable true\nunified_exec stable false\nview_image removed false\n'
function featureProbe(raw=supportedFeatures) {
 const calls=[]
 const run=async(_exe,args)=>{
  calls.push(args)
  if(args.includes('--help'))return requiredHelp
  if(args.join(' ')==='features list')return raw
  if(args.join(' ')==='login status')return 'Logged in using ChatGPT'
  throw new Error('Unexpected probe')
 }
 return {run,calls}
}
const probeEnv={OWNER_AI_CODEX_HOME:process.cwd()+'/fixture-empty-home',HOME:process.cwd()}
test('missing or removed optional features do not reject an authenticated supported CLI',async()=>{
 const f=featureProbe()
 assert.equal(await probeProvider('codex',{env:probeEnv,run:f.run}),'ready')
 assert.equal(f.calls.some(args=>args.includes('view_image')),false)
})
test('execution disables only features reported as supported',async()=>{
 const f=featureProbe();let admitted
 const result=await executeJob({...base,providers:['codex'],maxTokens:50},{env:probeEnv,run:f.run,session:async(_job,options)=>{
  admitted=options.features
  return {raw:'fixture',usage:{output_tokens:1}}
 }})
 assert.equal(result?.raw,'fixture')
 assert.deepEqual(admitted,['shell_tool','unified_exec'])
})
for(const raw of [null,'','not a feature listing']){
 test('failed or malformed feature discovery remains unavailable: '+String(raw),async()=>{
  const f=featureProbe(raw)
  assert.equal(await probeProvider('codex',{env:probeEnv,run:f.run}),'unavailable')
  assert.equal(f.calls.some(args=>args.join(' ')==='login status'),false)
 })
}
test('strict app-server arguments retain containment without unsupported config fields',async()=>{
 const f=protocol();const result=await runCodexSession(base,opts(f));assert.ok(result)
 const args=f.launches[0].args
 assert.equal(args.includes('tools.update_plan.enabled=false'),false)
 assert.equal(args.includes('agents.enabled=false'),false)
 assert.equal(args.includes('--strict-config'),true)
 assert.equal(args.includes('forced_login_method=chatgpt'),true)
 assert.equal(args.includes('web_search="disabled"'),true)
 assert.equal(args.includes('mcp_servers={}'),true)
 assert.equal(args.includes('shell_tool'),true)
})
