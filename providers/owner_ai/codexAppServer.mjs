import {spawn} from 'node:child_process'
import path from 'node:path'
import {existsSync} from 'node:fs'
const safeModel=value=>typeof value==='string'&&/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}$/.test(value)
const inertItems=new Set(['userMessage','reasoning','agentMessage'])
const MAX_WIRE_BYTES=8*1024*1024
const MAX_LINE_BYTES=1024*1024
const MAX_OUTPUT_BYTES=512*1024

// Dedicated official stdio session; no shell, filesystem tool, or public listener.
export function runCodexSession(job,{env,cwd,model,features=[],signal,spawnImpl=spawn,platform=process.platform,onDiagnostic=()=>{}}={}) {
  if(signal?.aborted||!safeModel(model)||!job||typeof job.prompt!=='string'||typeof job.system!=='string'||
    !['text','json'].includes(job.format)||!Number.isFinite(job.timeoutMs)||job.timeoutMs<=0||job.timeoutMs>120000||
    !Number.isInteger(job.maxTokens)||job.maxTokens<2||Buffer.byteLength(job.prompt+job.system)>1024*1024)return Promise.resolve(null)
  // App-server has no exec-only ignore-user-config flag. Require the dedicated
  // authentication-only home instead of inheriting arbitrary owner settings.
  if (!env?.CODEX_HOME || !path.isAbsolute(env.CODEX_HOME) || ['config.toml','config.d','AGENTS.md','hooks.json'].some(name=>existsSync(path.join(env.CODEX_HOME,name)))) return Promise.resolve(null)
  const executable=platform==='win32'?'codex.exe':'codex'
  const args=['app-server','--strict-config','--listen','stdio://',
    '-c','forced_login_method=chatgpt','-c','web_search="disabled"','-c','mcp_servers={}',
    ...features.flatMap(f=>['--disable',f])]
  return new Promise(resolve=>{
    let child;let buffer='';let bytes=0;let stopped=false;let closed=false;let response=null
    let threadId=null;let turnId=null;let usage=null;let finalTurn=null;let nextId=1
    const rpc=new Map();const messages=new Map();const timer=setTimeout(()=>stop(null),job.timeoutMs)
    let forceTimer;let forcedTermination=false
    const diagnostic=stage=>{try{onDiagnostic(stage)}catch{}}
    const abort=()=>stop(null)
    // Terminate only the child owned by this one inference request.
    function terminateChild(){
      if(!child)return
      forcedTermination=true
      if(platform==='win32'&&Number.isInteger(child.pid)){
        try{const killer=spawnImpl(path.join(env.SystemRoot||'C:\\Windows','System32','taskkill.exe'),['/PID',String(child.pid),'/T','/F'],{shell:false,windowsHide:true,stdio:'ignore'});killer.on?.('error',()=>{try{child.kill()}catch{}});killer.on?.('close',code=>{if(code!==0){try{child.kill()}catch{}}})}catch{}
      }else if(platform!=='win32'&&Number.isInteger(child.pid)){
        try{process.kill(-child.pid,'SIGKILL')}catch{try{child.kill('SIGKILL')}catch{}}
      }else{try{child.kill('SIGKILL')}catch{}}
    }
    function settle(){
      if(!closed)return
      clearTimeout(timer);clearTimeout(forceTimer);signal?.removeEventListener('abort',abort)
      resolve(signal?.aborted?null:response)
    }
    function stop(value){
      if(stopped){if(value===null)response=null;return}
      stopped=true;response=value
      for(const pending of rpc.values())pending.reject(new Error('Session ended'))
      rpc.clear()
      try{child?.stdin?.end()}catch{}
      if(!child){closed=true;settle();return}
      forceTimer=setTimeout(terminateChild,200)
    }
    function send(message){if(!stopped)child.stdin.write(JSON.stringify(message)+'\n')}
    function request(method,params){
      if(stopped)return Promise.reject(new Error('Session ended'))
      return new Promise((resolveReply,reject)=>{const id=nextId++;rpc.set(id,{resolve:resolveReply,reject});send({id,method,params})})
    }
    function finishTurn(){
      if(!finalTurn||!usage||stopped)return
      if(finalTurn.status!=='completed'||finalTurn.error)return stop(null)
      const candidates=[...messages.values()].filter(item=>!item.phase||item.phase==='final_answer')
      const last=candidates.at(-1);const raw=last?.text
      if(typeof raw!=='string'||!raw.trim()||Buffer.byteLength(raw)>MAX_OUTPUT_BYTES)return stop(null)
      if(job.format==='json'){try{const parsed=JSON.parse(raw);if(!parsed||typeof parsed!=='object')return stop(null)}catch{return stop(null)}}
      diagnostic('completed');return stop({ok:true,complete:true,provider:'subscription:codex',billing_mode:'subscription',model,
        model_source:'app_server_configuration',raw,usage})
    }
    function notification(message){
      const p=message.params||{}
      if(p.threadId&&p.threadId!==threadId)return stop(null)
      if(p.turnId&&turnId&&p.turnId!==turnId)return stop(null)
      if(p.turnId&&!turnId)turnId=p.turnId
      if(message.method==='error')return stop(null)
      if(message.method==='account/updated')return stop(null)
      if(message.method==='model/rerouted'&&p.toModel!==model)return stop(null)
      if(message.method==='item/started'||message.method==='item/updated'||message.method==='item/completed'){
        if(!inertItems.has(p.item?.type))return stop(null)
        if(message.method==='item/completed'&&p.item.type==='agentMessage'){
          if(typeof p.item.id!=='string'||typeof p.item.text!=='string')return stop(null)
          messages.set(p.item.id,p.item)
        }
      }
      if(message.method==='thread/tokenUsage/updated'){
        const current=p.tokenUsage?.last
        if(!current||!['inputTokens','cachedInputTokens','outputTokens'].every(k=>Number.isSafeInteger(current[k])&&current[k]>=0)||current.outputTokens===0)return stop(null)
        usage={input_tokens:current.inputTokens,cached_input_tokens:current.cachedInputTokens,output_tokens:current.outputTokens}
        finishTurn()
      }
      if(message.method==='turn/completed'){
        if(!p.turn||typeof p.turn.id!=='string'||(turnId&&p.turn.id!==turnId))return stop(null)
        turnId=p.turn.id
        for(const item of p.turn.items||[]){if(!inertItems.has(item.type))return stop(null);if(item.type==='agentMessage')messages.set(item.id,item)}
        finalTurn=p.turn;finishTurn()
      }
    }
    function receive(line){
      if(stopped)return
      let message;try{message=JSON.parse(line)}catch{return stop(null)}
      if(message.id!==undefined){
        if(message.method)return stop(null)
        const pending=rpc.get(message.id);if(!pending)return stop(null);rpc.delete(message.id)
        if(message.error)pending.reject(new Error('Official protocol rejected request'))
        else pending.resolve(message.result)
      }else if(message.method)notification(message)
      else stop(null)
    }
    try {
      child=spawnImpl(executable,args,{cwd,env,shell:false,windowsHide:true,detached:platform!=='win32',stdio:['pipe','pipe','pipe']})
      child.on('error',()=>{closed=true;stop(null);settle()})
      child.stdin.on('error',()=>stop(null))
      child.on('close',(code)=>{closed=true;if(!stopped||(code!==0&&!forcedTermination))response=null;settle()})
      child.stdout.setEncoding('utf8');child.stdout.on('data',chunk=>{
        bytes+=Buffer.byteLength(chunk);buffer+=chunk
        if(bytes>MAX_WIRE_BYTES||Buffer.byteLength(buffer)>MAX_LINE_BYTES)return stop(null)
        let index;while(!stopped&&(index=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,index).trim();buffer=buffer.slice(index+1);if(line)receive(line)}
      })
      let errorBytes=0;child.stderr.on('data',chunk=>{errorBytes+=chunk.length;if(errorBytes>MAX_LINE_BYTES)stop(null)})
      signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)return stop(null)
      ;(async()=>{
        diagnostic('initialize');await request('initialize',{clientInfo:{name:'owner_subscription_bridge',version:'1.0.0'},capabilities:{experimentalApi:true}})
        send({method:'initialized',params:{}})
        diagnostic('account');const account=await request('account/read',{refreshToken:false})
        if(account?.account?.type!=='chatgpt')return stop(null)
        diagnostic('thread');const started=await request('thread/start',{model,modelProvider:'openai',cwd,approvalPolicy:'never',sandbox:'read-only',ephemeral:true,
          developerInstructions:job.system,config:{web_search:'disabled',model_reasoning_effort:'low'}})
        if(started?.model!==model||started.modelProvider!=='openai'||started.approvalPolicy!=='never'||
          started.sandbox?.type!=='readOnly'||started.sandbox.networkAccess===true||started.thread?.ephemeral!==true||
          !Array.isArray(started.instructionSources)||started.instructionSources.length!==0||typeof started.thread?.id!=='string')return stop(null)
        threadId=started.thread.id
        const prompt=JSON.stringify({task:job.prompt,response_format:job.format,requested_max_output_tokens:job.maxTokens})
        diagnostic('turn');const turn=await request('turn/start',{threadId,model,effort:'low',summary:'none',input:[{type:'text',text:prompt}]})
        if(typeof turn?.turn?.id!=='string'||(turnId&&turn.turn.id!==turnId))return stop(null)
        turnId=turn.turn.id
      })().catch(()=>stop(null))
    } catch {closed=true;stop(null);settle()}
  })
}
