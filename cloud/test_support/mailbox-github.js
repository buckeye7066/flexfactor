import sodium from 'libsodium-wrappers';
await sodium.ready;
export const githubKey = sodium.crypto_box_keypair();
export const githubPublicKey = Buffer.from(githubKey.publicKey).toString('base64');
const reply = (status, value) => new Response(status === 204 ? null : JSON.stringify(value), { status });

// Stateful GitHub endpoints for the added transport. Existing API responses
// remain in the original fixture's control. allCalls records both surfaces.
export function withMailboxGithub(fallback, options = {}) {
  const state = { releases: new Map(), secrets: new Map(), mailboxCalls: [], allCalls: [],
    active: false, nextRelease: 400, nextAsset: 800, hook: null };
  const fetcher = async (url, request = {}) => {
    const parsed = new URL(url), path = parsed.pathname, method = request.method || 'GET';
    let body;
    try { body = request.body ? JSON.parse(request.body) : undefined; } catch { body = undefined; }
    const call = { url: String(url), path, method, body };
    state.allCalls.push(call);
    const overridden = await state.hook?.(call);
    if (overridden) return overridden;
    const internalSecret = /\/actions\/secrets\/FLEXFACTOR_[A-F0-9]{32}_STEERING_KEY$/.test(path);
    const mailboxPath = /\/releases(?:\/|$)/.test(path);
    const internalClaim = method === 'PATCH' && body?.value
      && (() => { try { const c = JSON.parse(body.value); return c.steering && c.state !== 'dispatched'; } catch { return false; } })();
    if (internalSecret || mailboxPath || internalClaim
        || ((state.active || state.releases.size) && path === '/user') || (state.active && path.endsWith('/actions/secrets/public-key'))) {
      state.mailboxCalls.push(call);
    } else {
      const response = await fallback(url, request);
      if (method === 'POST' && path.endsWith('/actions/variables') && body?.name.startsWith('FLEXFACTOR_RUN_')) state.active = true;
      return response;
    }
    if (internalClaim) {
      options.variables?.set(body.name, body.value);
      return reply(204);
    }
    if (path === '/user') return reply(200, { id: 7, login: 'owner' });
    if (path.endsWith('/actions/secrets/public-key'))
      return reply(200, { key_id: 'mailbox-test-key', key: githubPublicKey });
    if (internalSecret) {
      const name = path.split('/').at(-1);
      if (method === 'PUT') { state.secrets.set(name, body); return reply(204); }
      if (method === 'DELETE') { state.secrets.delete(name); return reply(204); }
      return reply(404, {});
    }
    if (path.endsWith('/releases') && method === 'POST') {
      const release = { ...body, id: ++state.nextRelease, author: { id: 7 }, assets: [] };
      state.releases.set(release.id, release);
      return reply(201, release);
    }
    if (path.endsWith('/releases') && method === 'GET') return reply(200, [...state.releases.values()]);
    const match = /\/releases\/(\d+)(\/assets)?$/.exec(path);
    if (match) {
      const release = state.releases.get(Number(match[1]));
      if (!release) return reply(404, {});
      if (!match[2] && method === 'GET') return reply(200, release);
      if (!match[2] && method === 'DELETE') { state.releases.delete(release.id); return reply(204); }
      if (match[2] && method === 'GET') return reply(200, release.assets.slice(0, 100).map(({ data, ...asset }) => asset));
      if (match[2] && method === 'POST') {
        const asset = { id: ++state.nextAsset, name: parsed.searchParams.get('name'),
          size: Buffer.byteLength(request.body), uploader: { id: 7 }, data: body };
        release.assets.push(asset); await state.afterUpload?.(); return reply(201, asset);
      }
    }
    const assetMatch = /\/releases\/assets\/(\d+)$/.exec(path);
    if (assetMatch) {
      for (const release of state.releases.values()) {
        const index = release.assets.findIndex((asset) => asset.id === Number(assetMatch[1]));
        if (index < 0) continue;
        if (method === 'GET') return reply(200, release.assets[index].data);
        if (method === 'DELETE') { release.assets.splice(index, 1); return reply(204); }
      }
      return reply(404, {});
    }
    throw new Error(`Unexpected mailbox request: ${method} ${path}`);
  };
  fetcher.mailbox = state;
  return fetcher;
}
