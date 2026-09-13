import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { test } from "node:test";

import { MAX_JSON_BYTES } from "../lib/config.js";
import health from "../api/health.js";
import configure from "../api/configure.js";
import repositories from "../api/repositories.js";
import providerKey from "../api/provider-key.js";
import device from "../api/oauth/device.js";
import poll from "../api/oauth/token.js";
import refresh from "../api/oauth/refresh.js";
import dispatch from "../api/runs/dispatch.js";
import status from "../api/runs/status.js";
import details from "../api/runs/details.js";
import steer from "../api/runs/steer.js";

const requestId = "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
const bearer = "Bearer gho_test_session_12345678";
const routes = [
  [health, "GET", "/api/health", false],
  [configure, "POST", "/api/configure", true],
  [repositories, "GET", "/api/repositories", true],
  [providerKey, "GET", "/api/provider-key", true],
  [device, "POST", "/api/oauth/device", false],
  [poll, "POST", "/api/oauth/token", false],
  [refresh, "POST", "/api/oauth/refresh", false],
  [dispatch, "POST", "/api/runs/dispatch", true],
  [status, "GET", "/api/runs/status", true],
  [details, "GET", "/api/runs/details", true],
  [steer, "POST", "/api/runs/steer", true],
];

function invoke(handler, options = {}) {
  const request = Readable.from(options.chunks || []);
  Object.assign(request, {
    method: "POST", url: "/api/configure", query: {},
    ...options,
    headers: { "x-forwarded-proto": "https", ...(options.headers || {}) },
  });
  const response = {
    headers: {}, statusCode: 0, value: undefined,
    setHeader(name, value) { this.headers[name.toLowerCase()] = value; },
    status(value) { this.statusCode = value; return this; },
    json(value) { this.value = value; return this; },
    send(value) { this.value = value; return this; },
  };
  return handler(request, response).then(() => response);
}

function secure(response) {
  assert.equal(response.headers["cache-control"], "no-store");
  assert.equal(response.headers["content-security-policy"], "default-src 'none'; frame-ancestors 'none'");
  assert.equal(response.headers["referrer-policy"], "no-referrer");
  assert.equal(response.headers["x-content-type-options"], "nosniff");
  assert.equal(response.headers["x-frame-options"], "DENY");
  assert.match(response.headers["x-request-id"], /^[0-9a-f-]{36}$/);
}

function mockUpstream(t, implementation) {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return implementation(String(url), options);
  });
  return calls;
}

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), { status, headers });
}

function deviceReply() {
  return json({ device_code: "device_test_code", user_code: "ABCD-EFGH",
    verification_uri: "https://github.com/login/device", expires_in: 900, interval: 5 });
}

test("actual health responses include the configured HSTS policy", async () => {
  const response = await invoke(health, { method: "GET" });
  assert.equal(response.statusCode, 200);
  assert.equal(response.headers["strict-transport-security"], "max-age=63072000; includeSubDomains; preload");
});

test("all actual API handlers reject other methods before contacting GitHub", async (t) => {
  const calls = mockUpstream(t, () => { throw new Error("Unexpected upstream call"); });
  for (const [handler, method, url] of routes) {
    const result = await invoke(handler, { method: "DELETE", url });
    assert.equal(result.statusCode, 405, url);
    assert.equal(result.value.error, "method_not_allowed", url);
    assert.equal(result.headers.allow, method, url);
    secure(result);
  }
  assert.equal(calls.length, 0);
});

test("all actual API handlers reject forwarded HTTP with security headers", async (t) => {
  const calls = mockUpstream(t, () => { throw new Error("Unexpected upstream call"); });
  for (const [handler, method, url] of routes) {
    const result = await invoke(handler, { method, url, headers: { "x-forwarded-proto": "http" } });
    assert.equal(result.statusCode, 400, url);
    assert.equal(result.value.error, "https_required", url);
    secure(result);
  }
  assert.equal(calls.length, 0);
});

test("production handlers require verified HTTPS metadata and accept direct TLS", async (t) => {
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  t.after(() => { if (previous === undefined) delete process.env.NODE_ENV; else process.env.NODE_ENV = previous; });
  for (const [handler, method, url] of routes) {
    const result = await invoke(handler, { method, url, headers: { "x-forwarded-proto": undefined } });
    assert.equal(result.statusCode, 400, url);
    assert.equal(result.value.error, "https_required", url);
  }
  const direct = await invoke(health, { method: "GET", socket: { encrypted: true },
    headers: { "x-forwarded-proto": undefined } });
  assert.equal(direct.statusCode, 200);
  secure(direct);
});

test("authenticated handlers reject absent and malformed bearer credentials before GitHub", async (t) => {
  const calls = mockUpstream(t, () => { throw new Error("Unexpected upstream call"); });
  for (const [handler, method, url, authenticated] of routes) {
    if (!authenticated) continue;
    for (const authorization of [undefined, "Basic invalid", "Bearer short", "Bearer a\nbcdefghijk"]) {
      const result = await invoke(handler, { method, url, headers: { authorization } });
      assert.equal(result.statusCode, 401, url);
      assert.equal(result.value.error, "authentication_required", url);
      secure(result);
    }
  }
  assert.equal(calls.length, 0);
});

test("configure accepts existing OAuth, app, fine-grained and legacy bearer token formats", async (t) => {
  const calls = mockUpstream(t, () => json({ login: "owner" }, 200, { "x-oauth-scopes": "repo, workflow" }));
  const tokens = ["gho_example12345678", "ghu_example12345678", "ghs_example12345678",
    "ghp_example12345678", "github_pat_example12345678", "a".repeat(40)];
  for (const token of tokens) {
    const result = await invoke(configure, { body: {}, headers: { authorization: `Bearer ${token}` } });
    assert.equal(result.statusCode, 200);
    assert.deepEqual(result.value, { login: "owner" });
    secure(result);
    assert.equal(calls.at(-1).options.headers.Authorization, `Bearer ${token}`);
  }
});

test("POST handlers enforce declared, parsed, string, Buffer and streamed body size bounds", async (t) => {
  const calls = mockUpstream(t, () => { throw new Error("Oversized body reached GitHub"); });
  const oversized = "x".repeat(MAX_JSON_BYTES + 1);
  const representations = [
    { body: {}, headers: { "content-length": String(MAX_JSON_BYTES + 1) } },
    { body: { value: oversized } },
    { body: oversized },
    { body: Buffer.from(oversized) },
    { chunks: [Buffer.alloc(MAX_JSON_BYTES), Buffer.from("x")] },
    { body: { value: "é".repeat(MAX_JSON_BYTES / 2) } },
  ];
  for (const [handler, method, url] of routes.filter((route) => route[1] === "POST")) {
    for (const representation of representations) {
      const result = await invoke(handler, { method, url, ...representation,
        headers: { authorization: bearer, ...representation.headers } });
      assert.equal(result.statusCode, 413, `${url}: ${Object.keys(representation)}`);
      assert.equal(result.value.error, "request_too_large", url);
      secure(result);
    }
  }
  assert.equal(calls.length, 0);
});

test("actual configure handler requires an object for parsed and unparsed JSON bodies", async (t) => {
  const calls = mockUpstream(t, () => json({ login: "owner" }));
  for (const body of [[], [1], null, 42, true, "[]", "null", "42", "true", "{", Buffer.from("[]")]) {
    const result = await invoke(configure, { body, headers: { authorization: bearer } });
    assert.equal(result.statusCode, 400, `body type ${typeof body}`);
    assert.equal(result.value.error, "invalid_json");
    secure(result);
  }
  assert.equal(calls.length, 0);
});

test("body limits accept objects at the exact byte boundary", async (t) => {
  mockUpstream(t, () => json({ login: "owner" }));
  const raw = JSON.stringify({ value: "x".repeat(MAX_JSON_BYTES - 12) });
  assert.equal(Buffer.byteLength(raw), MAX_JSON_BYTES);
  for (const representation of [{ body: raw }, { body: Buffer.from(raw) },
    { body: JSON.parse(raw) }, { chunks: [Buffer.from(raw.slice(0, 100)), Buffer.from(raw.slice(100))] }]) {
    const result = await invoke(configure, { ...representation, headers: { authorization: bearer } });
    assert.equal(result.statusCode, 200);
  }
});

test("string-body limits reject oversized whitespace before treating an empty body as an object", async (t) => {
  const calls = mockUpstream(t, () => json({ login: "owner" }));
  for (const body of ["", " \t\r\n", " ".repeat(MAX_JSON_BYTES)]) {
    const result = await invoke(configure, { body, headers: { authorization: bearer } });
    assert.equal(result.statusCode, 200);
    assert.deepEqual(result.value, { login: "owner" });
  }
  assert.equal(calls.length, 3);
  const oversized = await invoke(configure, { body: " ".repeat(MAX_JSON_BYTES + 1),
    headers: { authorization: bearer } });
  assert.equal(oversized.statusCode, 413);
  assert.equal(oversized.value.error, "request_too_large");
  assert.equal(calls.length, 3);
  secure(oversized);
});

test("OAuth poll handles Buffer and streamed JSON and preserves pending/success semantics", async (t) => {
  const calls = mockUpstream(t, (_url, options) => options.body.get("device_code") === "pending_code"
    ? json({ error: "authorization_pending", error_description: "Waiting for device authorization." })
    : json({ access_token: "gho_example12345678", refresh_token: "ghr_example12345678",
      expires_in: 3600, refresh_token_expires_in: 7200, token_type: "bearer", scope: "repo, workflow" }));
  const pending = await invoke(poll, { body: Buffer.from('{"device_code":"pending_code"}') });
  assert.equal(pending.statusCode, 202);
  assert.equal(pending.value.error, "authorization_pending");
  secure(pending);
  const granted = await invoke(poll, { chunks: [Buffer.from('{"device_'), Buffer.from('code":"approved_code"}')] });
  assert.equal(granted.statusCode, 200);
  assert.equal(granted.value.access_token, "gho_example12345678");
  assert.equal(granted.value.refresh_token, "ghr_example12345678");
  secure(granted);
  assert.equal(calls.length, 2);
  assert.ok(calls.every((call) => call.url === "https://github.com/login/oauth/access_token"));
});

test("OAuth handlers bound concurrent upstream requests and release capacity after completion", async (t) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const calls = mockUpstream(t, async (url) => {
    await held;
    return url.endsWith("/device/code") ? deviceReply() : json({ access_token: "gho_example12345678" });
  });
  const running = Array.from({ length: 8 }, () => invoke(device, { body: {} }));
  let blocked;
  try {
    await new Promise((resolve) => setImmediate(resolve));
    // A ninth request may only reach GitHub on the broken baseline. Race it
    // against the next event-loop turn so the regression cannot deadlock.
    blocked = await Promise.race([
      invoke(refresh, { body: { refresh_token: "ghr_example12345678" } }),
      new Promise((resolve) => setImmediate(() => resolve(null))),
    ]);
    assert.ok(blocked, "ninth OAuth request was sent upstream instead of rejected");
    assert.equal(blocked.statusCode, 429);
    assert.equal(blocked.value.error, "oauth_rate_limited");
    assert.ok(Number(blocked.headers["retry-after"]) >= 1);
    secure(blocked);
    assert.equal(calls.length, 8);
  } finally {
    release();
    await Promise.all(running);
    await new Promise((resolve) => setImmediate(resolve));
  }
  const next = await invoke(refresh, { body: { refresh_token: "ghr_example12345678" } });
  assert.equal(next.statusCode, 200);
});

test("OAuth per-instance budget rejects bursts across routes and recovers after its window", async (t) => {
  let now = Date.now() + 120_000;
  t.mock.method(Date, "now", () => now);
  const calls = mockUpstream(t, (url) => url.endsWith("/device/code")
    ? deviceReply() : json({ error: "authorization_pending" }));
  for (let index = 0; index < 120; index++) {
    const response = await invoke(index % 2 ? poll : device, { body: { device_code: "pending_code" } });
    assert.equal(response.statusCode, index % 2 ? 202 : 200);
  }
  const blocked = await invoke(refresh, { body: { refresh_token: "ghr_example12345678" } });
  assert.equal(blocked.statusCode, 429);
  assert.equal(blocked.value.error, "oauth_rate_limited");
  assert.equal(calls.length, 120);
  secure(blocked);
  now += 60_001;
  assert.equal((await invoke(device, { body: {} })).statusCode, 200);
});

test("handler failures never log credentials, body, query or upstream exception details", async (t) => {
  const logs = [];
  t.mock.method(console, "error", (value) => logs.push(value));
  mockUpstream(t, () => { throw new Error("sensitive-upstream-exception"); });
  const result = await invoke(configure, { url: "/api/configure?credential=sensitive-query",
    body: { credential: "sensitive-body" }, headers: { authorization: "Bearer sensitive-bearer" } });
  assert.equal(result.statusCode, 503);
  assert.equal(result.value.error, "upstream_unavailable");
  assert.equal(logs.length, 1);
  const logged = JSON.parse(logs[0]);
  assert.equal(logged.request_id, result.headers["x-request-id"]);
  assert.equal(logged.status, 503);
  assert.doesNotMatch(JSON.stringify({ logs, value: result.value }), /sensitive-/);
  secure(result);
});

test("failure logs omit raw paths that can contain credentials", async (t) => {
  const logs = [];
  t.mock.method(console, "error", (value) => logs.push(value));
  mockUpstream(t, () => { throw new Error("Upstream unreachable"); });
  const result = await invoke(configure, { url: "/api/configure/sensitive-path-value",
    body: {}, headers: { authorization: bearer } });
  assert.equal(result.statusCode, 503);
  assert.equal(logs.length, 1);
  assert.doesNotMatch(JSON.stringify(logs), /sensitive-path-value/);
});

test("GitHub failures do not reflect arbitrary upstream response text", async (t) => {
  t.mock.method(console, "error", () => {});
  mockUpstream(t, () => json({ message: "sensitive-upstream-response" }, 503));
  const configured = await invoke(configure, { body: {}, headers: { authorization: bearer } });
  assert.equal(configured.statusCode, 502);
  assert.doesNotMatch(JSON.stringify(configured.value), /sensitive-/);
});

test("unknown OAuth errors do not reflect arbitrary upstream codes or descriptions", async (t) => {
  mockUpstream(t, () => json({ error: "sensitive-upstream-code", error_description: "sensitive-upstream-description" }));
  const exchanged = await invoke(refresh, { body: { refresh_token: "ghr_example12345678" } });
  assert.equal(exchanged.statusCode, 401);
  assert.equal(exchanged.value.error, "oauth_exchange_failed");
  assert.doesNotMatch(JSON.stringify(exchanged.value), /sensitive-/);
});

test("recognized OAuth errors preserve Android polling states using fixed safe messages", async (t) => {
  let upstreamCode;
  mockUpstream(t, () => json({ error: upstreamCode, error_description: "sensitive-upstream-description" }));
  for (const [code, expectedStatus] of [["authorization_pending", 202], ["slow_down", 202],
    ["expired_token", 401], ["access_denied", 401], ["bad_refresh_token", 401]]) {
    upstreamCode = code;
    const result = await invoke(poll, { body: { device_code: "test_code" } });
    assert.equal(result.statusCode, expectedStatus);
    assert.equal(result.value.error, code);
    assert.doesNotMatch(JSON.stringify(result.value), /sensitive-/);
  }
});

test("details handler requires request identity before listing or downloading artifacts", async (t) => {
  const calls = mockUpstream(t, () => json({ artifacts: [] }));
  const result = await invoke(details, { method: "GET", url: "/api/runs/details",
    query: { repository: "owner/project", run_id: "77" }, headers: { authorization: bearer } });
  assert.equal(result.statusCode, 400);
  assert.equal(calls.length, 0);
  secure(result);
});

test("details handler returns only an artifact from the supplied matching request", async (t) => {
  const archive = Buffer.from("PK\u0003\u0004artifact-test");
  const calls = mockUpstream(t, (url) => {
    if (url.endsWith("/actions/runs/77")) return json({ id: 77, name: "FlexFactor Mobile",
      path: ".github/workflows/flexfactor-mobile.yml", event: "workflow_dispatch",
      display_title: `FlexFactor audit · ${requestId}`, status: "completed", conclusion: "success" });
    if (url.includes("/artifacts?")) return json({ artifacts: [{ id: 12, name: `mobile-phone-${requestId}`, expired: false, size_in_bytes: archive.length }] });
    if (url.endsWith("/artifacts/12/zip")) return new Response(null, { status: 302,
      headers: { location: "https://productionresultssa0.blob.core.windows.net/archive?sig=test" } });
    if (url.startsWith("https://productionresultssa0.blob.core.windows.net/")) return new Response(archive);
    throw new Error(`Unexpected test URL ${url}`);
  });
  const result = await invoke(details, { method: "GET", url: "/api/runs/details",
    query: { repository: "owner/project", run_id: "77", request_id: requestId }, headers: { authorization: bearer } });
  assert.equal(result.statusCode, 200);
  assert.deepEqual(result.value, archive);
  assert.equal(result.headers["content-type"], "application/zip");
  assert.equal(result.headers["content-length"], String(archive.length));
  secure(result);
  assert.equal(calls[0].url, "https://api.github.com/repos/owner/project/actions/runs/77");
  assert.equal(calls.at(-1).options.headers?.Authorization, undefined);
});
