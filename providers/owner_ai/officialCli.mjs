import { spawn } from 'node:child_process'
import {runCodexSession} from './codexAppServer.mjs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

const codexDisabledFeatures = 'shell_tool unified_exec apps plugins remote_plugin browser_use browser_use_external image_generation view_image multi_agent tool_suggest skill_search skill_mcp_dependency_install in_app_browser memories sleep_tool'.split(' ')
const safeModel = value => typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,119}$/.test(value)
const codexModel = env => {
  const model = env.OWNER_AI_CODEX_MODEL ?? 'gpt-6-astra'
  if (!safeModel(model)) throw new Error('unavailable')
  return model
}

export function childEnvironment(provider, env = process.env) {
  const clean = {}
  for (const key of ['PATH', 'Path', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'LOCALAPPDATA', 'USERPROFILE', 'HOME']) if (env[key]) clean[key] = env[key]
  if (!env.LOCALAPPDATA && !(provider === 'codex' && env.OWNER_AI_CODEX_HOME)) throw new Error('unavailable')
  const subscriptionHome = provider === 'codex' && env.OWNER_AI_CODEX_HOME ? env.OWNER_AI_CODEX_HOME : path.join(env.LOCALAPPDATA, 'FactoryDeck', 'subscriptions', provider)
  if (!path.isAbsolute(subscriptionHome)) throw new Error('unavailable')
  clean[provider === 'codex' ? 'CODEX_HOME' : 'CLAUDE_CONFIG_DIR'] = subscriptionHome
  clean.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
  clean.CLAUDE_CODE_SAFE_MODE = '1'
  return clean
}
export function subscriptionAuth(provider, raw) {
  if (provider === 'codex') return /^Logged in using ChatGPT\s*$/i.test(raw.trim())
  if (provider !== 'claude') return false
  try {
    const s = JSON.parse(raw)
    return s.loggedIn === true && s.authMethod === 'claude.ai' && ['pro', 'max'].includes(s.subscriptionType?.toLowerCase()) && !s.apiKeySource
  } catch { return false }
}
export function cliArguments(provider, env = process.env) {
  if (provider === 'codex') return ['exec', '--sandbox', 'read-only', '--ephemeral', '--ignore-user-config', '--strict-config', '--skip-git-repo-check', '--json', '--model', codexModel(env),
    '-c', 'forced_login_method=chatgpt', '-c', 'model_reasoning_effort="low"', '-c', 'web_search="disabled"', '-c', 'agents.enabled=false', '-c', 'mcp_servers={}', '-c', 'tools.update_plan.enabled=false',
    ...codexDisabledFeatures.flatMap(feature => ['--disable', feature])]
  if (provider !== 'claude') throw new Error('unavailable')
  return ['--print', '--output-format', 'json', '--safe', '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '', '--disable-slash-commands', '--no-session-persistence', '--no-chrome', '--permission-mode', 'dontAsk', '--permission-prompts', 'none']
}
export function parseResult(provider, raw, requestedModel) {
  if (typeof raw !== 'string' || !raw.trim() || Buffer.byteLength(raw) > 524288) return null
  // Legacy CLI JSONL lacks authoritative model identity. Codex runs now use
  // the official app-server's selected model and reroute notifications.
  if (provider === 'codex') return null
  if (provider !== 'claude') return null
  try {
    const r = JSON.parse(raw)
    const models = Object.keys(r.modelUsage || {})
    if (r.type !== 'result' || r.subtype !== 'success' || r.is_error !== false || r.stop_reason !== 'end_turn' ||
        typeof r.result !== 'string' || !r.result.trim() || models.length !== 1 || !Number.isFinite(r.usage?.output_tokens) || r.usage.output_tokens <= 0) return null
    return { ok: true, complete: true, provider: 'subscription:claude', billing_mode: 'subscription', model: models[0], raw: r.result, usage: { output_tokens: r.usage.output_tokens } }
  } catch { return null }
}
export function runChild(executable, args, { cwd, env, input = '', signal, captureAuthMetadata = false, spawnImpl = spawn, platform = process.platform } = {}) {
  if (signal?.aborted) return Promise.resolve(null)
  return new Promise(resolve => {
    let child
    const output = []
    let bytes = 0
    let failed = false
    const kill = () => {
      failed = true
      if (platform === 'win32' && Number.isInteger(child?.pid)) {
        const killer = spawnImpl(path.join(env.SystemRoot || 'C:\\Windows', 'System32', 'taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: 'ignore' })
        killer.on('error', () => child.kill())
        killer.on('close', code => { if (code !== 0 && (child.exitCode === null || child.exitCode === undefined)) child.kill() })
      } else child?.kill('SIGKILL')
    }
    try {
      child = spawnImpl(executable, args, { cwd, env, shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
      signal?.addEventListener('abort', kill, { once: true })
      child.on('error', () => { failed = true; signal?.removeEventListener('abort', kill); resolve(null) })
      child.on('close', code => { signal?.removeEventListener('abort', kill); resolve(!failed && code === 0 ? Buffer.concat(output).toString('utf8') : null) })
      // Only explicit native auth metadata probes merge stderr, never inference.
      const mergeAuth = captureAuthMetadata && !input && ((/^codex(?:\.exe)?$/.test(path.basename(executable)) && args.join(' ') === 'login status') ||
        (/^claude(?:\.exe)?$/.test(path.basename(executable)) && args.join(' ') === 'auth status --json'))
      const collect = (chunk, stdout) => { bytes += chunk.length; if (bytes > (mergeAuth ? 16384 : 524288)) kill(); else if (stdout || mergeAuth) output.push(Buffer.from(chunk)) }
      child.stdout.on('data', chunk => collect(chunk, true))
      child.stderr.on('data', chunk => collect(chunk, false))
      child.stdin.on('error', () => { failed = true })
      child.stdin.end(input)
      if (signal?.aborted) kill()
    } catch { resolve(null) }
  })
}
export async function probeProvider(provider, { signal, env = process.env, run = runChild } = {}) {
  if (!['codex', 'claude'].includes(provider) || signal?.aborted) return 'unavailable'
  try {
    const clean = childEnvironment(provider, env)
    if (provider === 'codex') {
      const args = cliArguments(provider, env)
      const help = await run(providerExecutable('codex'), [...args, '--help'], { env: clean, signal })
      const required = ['--sandbox', '--ephemeral', '--ignore-user-config', '--strict-config', '--skip-git-repo-check', '--json', '--model', '--config', '--disable']
      const flags = new Set(help?.match(/--[a-z][a-z-]*/g) || [])
      if (!required.every(flag => flags.has(flag)) || signal?.aborted) return 'unavailable'
      const features = await run(providerExecutable('codex'), ['features', 'list'], { env: clean, signal })
      const supported = new Set((features || '').split(/\r?\n/).filter(line => /\s(?:true|false)\s*$/.test(line) && !/\bremoved\b/i.test(line)).map(line => line.trim().split(/\s+/)[0]))
      if (!codexDisabledFeatures.every(feature => supported.has(feature)) || signal?.aborted) return 'unavailable'
      const auth = await run(providerExecutable('codex'), ['login', 'status'], { env: clean, signal, captureAuthMetadata: true })
      if (signal?.aborted) return 'unavailable'
      if (auth && subscriptionAuth(provider, auth)) return 'ready'
      return auth && /^(?:Logged in using (?:an? )?API key.*|Not logged in)\s*$/i.test(auth.trim()) ? 'auth_required' : 'unavailable'
    }
    const help = await run(providerExecutable('claude'), [...cliArguments(provider), '--help'], { env: clean, signal })
    if (!help || !['--safe-mode', '--tools', '--strict-mcp-config', '--setting-sources', '--permission-prompts'].every(flag => help.includes(flag))) return 'unavailable'
    const auth = await run(providerExecutable('claude'), ['auth', 'status', '--json'], { env: clean, signal, captureAuthMetadata: true })
    if (signal?.aborted || auth === null) return 'unavailable'
    return auth && subscriptionAuth(provider, auth) ? 'ready' : 'auth_required'
  } catch { return 'unavailable' }
}
export async function executeJob(job, { signal, env = process.env, run = runChild, session = runCodexSession } = {}) {
  let cwd
  let deadlineTimer
  try {
    if (signal?.aborted || !Number.isFinite(job.timeoutMs) || job.timeoutMs <= 0 || job.timeoutMs > 120000 ||
        !Number.isInteger(job.maxTokens) || job.maxTokens < 2 || !Array.isArray(job.providers)) return null
    const deadline = Date.now() + job.timeoutMs
    const controller = new AbortController()
    deadlineTimer = setTimeout(() => controller.abort(), job.timeoutMs)
    const wholeSignal = AbortSignal.any([controller.signal, ...(signal ? [signal] : [])])
    const providers = [...new Set(job.providers.filter(provider => ['codex', 'claude'].includes(provider)))]
    cwd = await mkdtemp(path.join(tmpdir(), 'grantflow-owner-ai-'))
    for (const [index, provider] of providers.entries()) {
      const remaining = deadline - Date.now()
      if (wholeSignal.aborted || remaining <= 0) break
      const slice = new AbortController()
      // A newly ready fallback must not halve the primary's proven runtime.
      const fallbackReserve = index + 1 < providers.length ? Math.min(2000, remaining / 5) : 0
      const timer = setTimeout(() => slice.abort(), Math.max(1, Math.floor(remaining - fallbackReserve)))
      const attemptSignal = AbortSignal.any([wholeSignal, slice.signal])
      try {
        if (await probeProvider(provider, { signal: attemptSignal, env, run }) !== 'ready' || attemptSignal.aborted) continue
        const clean = childEnvironment(provider, env)
        if (provider === 'claude') clean.CLAUDE_CODE_MAX_OUTPUT_TOKENS = String(Math.min(job.maxTokens, 32000))
        if (provider === 'codex') {
          const result = await session({...job,timeoutMs:Math.max(1,deadline-Date.now())},{env:clean,cwd,model:codexModel(env),features:codexDisabledFeatures,signal:attemptSignal})
          if (result && !attemptSignal.aborted && result.usage.output_tokens < job.maxTokens && (job.format !== 'json' || validJsonObject(result.raw))) return result
          continue
        }
        const args = cliArguments(provider, env)
        const raw = await run(providerExecutable(provider), args, { cwd, env: clean, signal: attemptSignal,
          input: JSON.stringify({ system: job.system, prompt: job.prompt, format: job.format }) })
        if (attemptSignal.aborted || Date.now() >= deadline) continue
        const result = raw ? parseResult(provider, raw, provider === 'codex' ? args[args.indexOf('--model') + 1] : undefined) : null
        if (result && result.usage.output_tokens < job.maxTokens && (job.format !== 'json' || validJsonObject(result.raw))) return result
      } catch { /* Native failure permits the next subscription, never an API call. */ }
      finally { clearTimeout(timer); slice.abort() }
    }
    return null
  } catch { return null }
  finally { clearTimeout(deadlineTimer); if (cwd) await rm(cwd, { recursive: true, force: true }) }
}
function validJsonObject(raw) {
  try { const value = JSON.parse(raw); return value !== null && typeof value === 'object' && !Array.isArray(value) } catch { return false }
}

export function providerExecutable(provider,platform=process.platform){return provider+(platform==='win32'?'.exe':'')}
