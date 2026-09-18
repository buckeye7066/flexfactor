import { spawnSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { assertMailbox, assertMailboxAsset, openSteering, mailboxMetadata,
  MAX_ENVELOPE_BYTES, MAX_MAILBOX_ASSETS } from '../../cloud/lib/steering-mailbox.js';

async function boundedResponse(response, maximum) {
  if (Number(response.headers.get('content-length') || 0) > maximum)
    throw new Error('Steering response exceeds its byte limit');
  if (!response.body) return Buffer.alloc(0);
  const reader = response.body.getReader();
  const chunks = []; let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > maximum) { await reader.cancel(); throw new Error('Steering response exceeds its byte limit'); }
      chunks.push(Buffer.from(value));
    }
  } finally { reader.releaseLock(); }
  return Buffer.concat(chunks, total);
}
function apiHeaders(token, binary = false) {
  return { Authorization: `Bearer ${token}`, 'User-Agent': 'FlexFactor-Mobile-Steering',
    'X-GitHub-Api-Version': '2026-03-10',
    Accept: binary ? 'application/octet-stream' : 'application/vnd.github+json' };
}
export function submitToEngine(message, config) {
  // The existing journal owns deduplication and delivery, not a second queue.
  const code = `import json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport flexfactor_steering as s\nv=json.load(sys.stdin)\nr=s.submit_session_routing({'routes':[{'program':'target','project_dir':sys.argv[2],'instruction':v['comment']}],'evidence':[]},source='android',root=v.get('root'),session_id=v['request_id'].replace('-','')+v['id'].replace('-',''))\nprint(len(r['submission_ids']))`;
  const environment = { ...process.env };
  for (const name of ['STEERING_PRIVATE_KEY', 'GH_TOKEN', 'GITHUB_TOKEN',
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'COPILOT_GITHUB_TOKEN']) delete environment[name];
  const child = spawnSync(config.python || 'python',
    ['-c', code, config.engineRoot, config.targetPath], {
      input: JSON.stringify({ ...message, root: config.steeringRoot }),
      encoding: 'utf8', env: environment, timeout: 20000, maxBuffer: 4096,
    });
  if (child.error || child.status !== 0 || child.stdout.trim() !== '1')
    throw new Error('Engine steering journal did not confirm the message');
}
async function apiRead(config, path, maximum, fetchImpl, binary = false) {
  const response = await fetchImpl('https://api.github.com' + path, {
    redirect: 'manual', headers: apiHeaders(config.token, binary), signal: AbortSignal.timeout(20000),
  });
  if (response.status === 404) return null;
  if (binary && [302, 307].includes(response.status)) {
    const target = new URL(response.headers.get('location'));
    if (target.protocol !== 'https:' || target.username || target.password
        || !target.hostname.endsWith('.githubusercontent.com'))
      throw new Error('Untrusted encrypted steering asset location');
    const content = await fetchImpl(target.href, { redirect: 'error', signal: AbortSignal.timeout(20000) });
    if (content.status !== 200) throw new Error('Encrypted steering asset is unavailable');
    return boundedResponse(content, maximum);
  }
  if (response.status !== 200) throw new Error(`Steering read failed (HTTP ${response.status})`);
  return boundedResponse(response, maximum);
}
export async function pollOnce(config, fetchImpl = fetch, submit = submitToEngine) {
  mailboxMetadata(config.request_id, config.repository, config.public_key);
  if (!Number.isSafeInteger(config.release_id) || config.release_id <= 0
      || !Number.isSafeInteger(config.author_id) || config.author_id <= 0
      || typeof config.token !== 'string' || !config.token || /[\r\n]/.test(config.token))
    throw new Error('Invalid private steering reader configuration');
  const prefix = `/repos/${config.repository}/releases`;
  const raw = await apiRead(config, `${prefix}/${config.release_id}`, 256 * 1024, fetchImpl);
  if (!raw) throw new Error('Private steering mailbox is missing');
  assertMailbox(JSON.parse(raw), config);
  const bytes = await apiRead(config, `${prefix}/${config.release_id}/assets?per_page=100`,
    512 * 1024, fetchImpl);
  const assets = bytes && JSON.parse(bytes);
  if (!Array.isArray(assets) || assets.length > MAX_MAILBOX_ASSETS)
    throw new Error('Steering mailbox exceeds its asset limit');
  const delivered = [];
  for (const asset of assets) {
    assertMailboxAsset(asset, config);
    const encrypted = await apiRead(config, `${prefix}/assets/${asset.id}`,
      MAX_ENVELOPE_BYTES, fetchImpl, true);
    if (!encrypted) continue;
    const message = await openSteering(config.privateKey, JSON.parse(encrypted),
      config.request_id, config.repository);
    await submit(message, config);
    delivered.push(message.id);
    const removed = await fetchImpl(`https://api.github.com${prefix}/assets/${asset.id}`, {
      method: 'DELETE', redirect: 'error', headers: apiHeaders(config.token),
      signal: AbortSignal.timeout(20000),
    });
    if (![204, 404].includes(removed.status)) throw new Error('Steering receipt cleanup needs retry');
  }
  return delivered;
}
async function stdinKey() {
  if ('STEERING_PRIVATE_KEY' in process.env) throw new Error('Environment key transport is forbidden');
  let text='';
  for await (const chunk of process.stdin) {
    text+=chunk.toString('utf8');
    if(Buffer.byteLength(text)>45) throw new Error('Invalid steering handoff size');
  }
  if(!/^[A-Za-z0-9+/]{43}=\n?$/.test(text)) throw new Error('Invalid steering handoff');
  return text.trim();
}
async function main() {
  const { steeringPublicKey } = await import('../../cloud/lib/steering-mailbox.js');
  const config = {
    token: process.env.GH_TOKEN, privateKey: await stdinKey(),
    request_id: process.env.REQUEST_ID, repository: process.env.TARGET_REPOSITORY,
    release_id: Number(process.env.STEERING_RELEASE_ID), author_id: Number(process.env.GITHUB_ACTOR_ID),
    engineRoot: resolve(dirname(fileURLToPath(import.meta.url)), '../..'),
    targetPath: resolve('target'),
  };
  config.public_key = await steeringPublicKey(config.privateKey);
  if (!config.token || !Number.isSafeInteger(config.release_id) || config.release_id <= 0
      || !Number.isSafeInteger(config.author_id) || config.author_id <= 0)
    throw new Error('Required private steering identity is unavailable');
  for (const id of await pollOnce(config))
    console.log(`[steering] encrypted message ${id} entered the engine journal`);
  writeFileSync('mobile-steering-ready', 'ready\n', { mode: 0o600 });
  while (true) {
    await delay(20000);
    try {
      for (const id of await pollOnce(config)) {
        console.log(`[steering] encrypted message ${id} entered the engine journal`);
      }
    } catch { console.error('[steering] private channel check failed; retrying without exposing content'); }
  }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { console.error('[steering] private channel initialization failed'); process.exitCode = 1; });
}
