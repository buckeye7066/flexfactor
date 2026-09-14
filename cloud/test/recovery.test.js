import assert from "node:assert/strict";
import { test } from "node:test";
import { dispatch, runArtifact, runStatus, ServiceError } from "../lib/service.js";
import { mobileWorkflow } from "../lib/workflow.js";

const requestId = "4d32c8e5-6f2b-4a98-a7f5-99594c49b2f8";
const claimName = "FLEXFACTOR_RUN_4D32C8E56F2B4A98A7F5";
const steeringName = "FLEXFACTOR_STEERING_4D32C8E56F2B4A98";
const runRequest = {
  request_id: requestId, repository: "owner/project", ref: "main", mode: "audit",
  provider: "auto", max_cost: 50, threshold: 90, max_iterations: 6,
};
const claim = (overrides = {}) => ({ schema: 1, request_id: requestId,
  state: "claimed", run_id: 0, ephemeral_secrets: ["OPENAI_API_KEY"],
  created_at: new Date().toISOString(), ...overrides });
const run = (overrides = {}) => ({ event: "workflow_dispatch", path: ".github/workflows/flexfactor-mobile.yml", id: 99, status: "completed", conclusion: "success",
  display_title: `FlexFactor audit · ${requestId}`,
  html_url: "https://github.com/owner/project/actions/runs/99", ...overrides });
const response = (body, status = 200, headers = {}) => new Response(
  status === 204 ? null : Buffer.isBuffer(body) ? body : JSON.stringify(body), { status, headers });

// Only GitHub transport is substituted. Variable create conflicts, deletes,
// run visibility, credentials, and dispatch acceptance retain their side effects.
function github({ storedClaim, runs = [], history = runs, hook } = {}) {
  const variables = new Map([[steeringName, "[]"]]);
  if (storedClaim) variables.set(claimName, JSON.stringify(storedClaim));
  const secrets = new Set(storedClaim?.ephemeral_secrets || []);
  const calls = [];
  let dispatches = 0;
  const fetcher = async (url, options = {}) => {
    const parsed = new URL(url);
    const path = parsed.pathname.replace("/repos/owner/project", "");
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    const call = { url: String(url), path, method, body };
    calls.push(call);
    const overridden = await hook?.(call, { variables, secrets, calls });
    if (overridden) return overridden;
    if (path === "/actions/variables" && method === "POST") {
      if (variables.has(body.name)) return response({}, 422);
      variables.set(body.name, body.value);
      return response({}, 201);
    }
    if (path.startsWith("/actions/variables/")) {
      const name = path.split("/").at(-1);
      if (method === "GET") return variables.has(name)
        ? response({ name, value: variables.get(name) }) : response({}, 404);
      if (method === "DELETE") { variables.delete(name); return response(null, 204); }
      if (method === "PATCH") {
        if (!variables.has(name)) return response({}, 404);
        variables.set(name, body.value); return response(null, 204);
      }
    }
    if (path.startsWith("/actions/secrets/")) {
      const name = path.split("/").at(-1);
      if (method === "GET") return response({}, secrets.has(name) ? 200 : 404);
      if (method === "PUT") { secrets.add(name); return response(null, 204); }
      if (method === "DELETE") { secrets.delete(name); return response(null, 204); }
    }
    if (path === "/actions/runs") return response({ workflow_runs: history });
    if (/^\/actions\/runs\/\d+$/.test(path)) {
      const found = runs.find((item) => item.id === Number(path.split("/").at(-1)));
      return response(found || {}, found ? 200 : 404);
    }
    if (path.endsWith("/jobs")) return response({ jobs: [] });
    if (path === "") return response({ default_branch: "main" });
    if (path === "/commits/main") return response({ sha: "a".repeat(40) });
    if (path === "/contents/.github/workflows/flexfactor-mobile.yml") {
      return response({ sha: "workflow-sha", content: Buffer.from(mobileWorkflow()).toString("base64") });
    }
    if (path === "/actions/workflows/flexfactor-mobile.yml/dispatches") {
      dispatches += 1;
      return response({ workflow_run_id: 99, html_url: run().html_url });
    }
    throw new Error(`Unexpected GitHub transport: ${method} ${path}`);
  };
  return { fetcher, variables, secrets, calls, get dispatches() { return dispatches; } };
}

for (const stored of [claim(), claim({ state: "dispatched", run_id: 99 })]) {
  test(`404 does not authorize resource deletion for ${stored.state} requests`, async () => {
    const api = github({ storedClaim: stored });
    await assert.rejects(() => runStatus("test_token", "owner/project", 99, requestId, api.fetcher),
      (error) => error instanceof ServiceError && error.status === 404);
    assert.deepEqual([...api.secrets], ["OPENAI_API_KEY"]);
    assert.ok(api.variables.has(claimName));
    assert.equal(api.calls.filter((call) => call.method === "DELETE").length, 0);
  });
}

test("terminal status rejects an upstream run ID that differs from the requested ID", async () => {
  const api = github({ storedClaim: claim(), hook: (call) =>
    call.path === "/actions/runs/99" ? response(run({ id: 100 })) : undefined });
  await assert.rejects(() => runStatus("test_token", "owner/project", 99, requestId, api.fetcher),
    (error) => error.code === "run_identity_mismatch");
  assert.ok(api.variables.has(claimName));
  assert.ok(api.secrets.has("OPENAI_API_KEY"));
});

test("terminal cleanup removes steering before secrets and deletes the claim last", async () => {
  const api = github({ storedClaim: claim({ state: "dispatched", run_id: 99 }), runs: [run()] });
  const state = await runStatus("test_token", "owner/project", 99, requestId, api.fetcher);
  assert.equal(state.conclusion, "success");
  assert.equal(api.variables.size, 0);
  assert.equal(api.secrets.size, 0);
  assert.deepEqual(api.calls.filter((call) => call.method === "DELETE").map((call) => call.path), [
    `/actions/variables/${steeringName}`, "/actions/secrets/OPENAI_API_KEY", `/actions/variables/${claimName}`,
  ]);
});

for (const resource of [steeringName, "OPENAI_API_KEY", claimName]) {
  test(`terminal cleanup retries after a partial ${resource} failure`, async () => {
    let fail = true;
    const api = github({ storedClaim: claim({ state: "dispatched", run_id: 99 }), runs: [run()],
      hook: (call) => {
        if (fail && call.method === "DELETE" && call.path.endsWith(`/${resource}`)) {
          fail = false; return response({}, 500);
        }
      } });
    await assert.rejects(() => runStatus("test_token", "owner/project", 99, requestId, api.fetcher));
    assert.ok(api.variables.has(claimName), "retain cleanup manifest until all resources are deleted");
    const state = await runStatus("test_token", "owner/project", 99, requestId, api.fetcher);
    assert.equal(state.status, "completed");
    assert.equal(api.variables.size, 0);
    assert.equal(api.secrets.size, 0);
  });
}

test("terminal status cleans legacy steering even after the old service removed the claim", async () => {
  const api = github({ runs: [run()] });
  await runStatus("test_token", "owner/project", 99, requestId, api.fetcher);
  assert.equal(api.variables.size, 0);
});

test("a recorded dispatch recovers directly before scanning saturated history or resolving a deleted ref", async () => {
  const api = github({ storedClaim: claim({ state: "dispatched", run_id: 99 }),
    runs: [run({ status: "in_progress", conclusion: null })],
    hook: (call) => call.path === "/actions/runs" ? response({}, 503) : undefined });
  const state = await dispatch("test_token", { ...runRequest, ref: "deleted" }, {}, api.fetcher);
  assert.equal(state.id, 99);
  assert.equal(api.calls.length, 2);
  assert.equal(api.dispatches, 0);
});

test("stale ambiguous claims surface actionable recovery without deleting or dispatching", async () => {
  const original = claim({ created_at: "2020-01-01T00:00:00Z" });
  const api = github({ storedClaim: original });
  await assert.rejects(() => dispatch("test_token", runRequest, {}, api.fetcher), (error) => {
    assert.equal(error.code, "dispatch_recovery_required");
    assert.match(error.message, /GitHub Actions/);
    assert.ok(error.message.includes(requestId));
    assert.ok(error.message.length <= 300, "recovery guidance must fit the released Android error display");
    assert.match(error.message, /request/i);
    return true;
  });
  assert.equal(api.variables.get(claimName), JSON.stringify(original));
  assert.equal(api.dispatches, 0);
  assert.equal(api.calls.some((call) => call.method !== "GET"), false);
});

test("ambiguous claims recover visible runs without replacing their durable claim", async () => {
  const original = claim({ created_at: "2020-01-01T00:00:00Z" });
  const api = github({ storedClaim: original, runs: [run()] });
  assert.equal((await dispatch("test_token", runRequest, {}, api.fetcher)).id, 99);
  assert.equal(api.dispatches, 0);
  assert.equal(api.variables.get(claimName), JSON.stringify(original));
  const history = api.calls.find((call) => call.path === "/actions/runs");
  assert.ok(new URL(history.url).searchParams.has("created"), "bound recovery to claim creation");
});

test("history recovery does not mistake GitHub's 1000 filtered results for complete absence", async () => {
  const api = github({ hook: (call) => {
    if (call.path !== "/actions/runs") return;
    const parsed = new URL(call.url);
    if (parsed.searchParams.has("event")) return response({ total_count: 1001, workflow_runs: [] });
    if (parsed.searchParams.get("page") === "1") {
      return response({ workflow_runs: [] }, 200, { link: '<https://api.github.com/next>; rel="next"' });
    }
    return response({ workflow_runs: [run()] });
  } });
  const state = await dispatch("test_token", runRequest, {}, api.fetcher);
  assert.equal(state.id, 99);
  assert.equal(api.dispatches, 0);
});

test("a GitHub dispatch 500 is ambiguous and never releases the idempotency claim", async () => {
  let accepted = 0;
  const api = github({ hook: (call) => {
    if (call.path.endsWith("/dispatches")) { accepted += 1; return response({}, 500); }
  } });
  await assert.rejects(() => dispatch("test_token", runRequest, {}, api.fetcher));
  assert.ok(api.variables.has(claimName));
  await assert.rejects(() => dispatch("test_token", runRequest, {}, api.fetcher));
  assert.equal(accepted, 1, "do not dispatch again after an ambiguous upstream failure");
});

test("simultaneous retries cannot reclaim an in-flight dispatch during run-ID recording", async () => {
  let releaseDispatch;
  const pending = new Promise((resolve) => { releaseDispatch = resolve; });
  let reachedDispatch;
  const reached = new Promise((resolve) => { reachedDispatch = resolve; });
  let attempts = 0;
  const api = github({ hook: async (call) => {
    if (call.path.endsWith("/dispatches") && ++attempts === 1) { reachedDispatch(); await pending; }
  } });
  const first = dispatch("test_token", runRequest, {}, api.fetcher);
  await reached;
  await assert.rejects(() => runStatus("test_token", "owner/project", 1234, requestId, api.fetcher));
  const retry = dispatch("test_token", runRequest, {}, api.fetcher);
  try {
    await assert.rejects(retry, (error) => ["dispatch_pending", "dispatch_recovery_required"].includes(error.code));
    assert.ok(api.variables.has(claimName));
  } finally { releaseDispatch(); }
  assert.equal((await first).id, 99);
  assert.equal(api.dispatches, 1);
});

test("concurrent fresh dispatch requests use one atomic create winner", async () => {
  const api = github();
  const results = await Promise.allSettled([
    dispatch("test_token", runRequest, {}, api.fetcher),
    dispatch("test_token", runRequest, {}, api.fetcher),
  ]);
  assert.ok(results.some((result) => result.status === "fulfilled"));
  assert.equal(api.dispatches, 1);
});

for (const badId of ["not-a-uuid", requestId.replaceAll("-", "")]) {
  test(`artifact rejects invalid request identity ${String(badId)} before upstream access`, async (t) => {
    let calls = 0;
    t.mock.method(globalThis, "fetch", async () => { calls += 1; return response({}); });
    await assert.rejects(() => runArtifact("test_token", "owner/project", 99, badId,
      async () => { calls += 1; return response({}); }),
    (error) => error.code === "invalid_request_id");
    assert.equal(calls, 0);
  });
}

test("artifact rejects mismatched run identity before listing or downloading", async () => {
  let calls = 0;
  await assert.rejects(() => runArtifact("test_token", "owner/project", 99, requestId,
    async (url) => {
      calls += 1;
      assert.match(url, /\/actions\/runs\/99$/);
      return response(run({ display_title: `Unrelated · ${requestId}` }));
    }), (error) => error.code === "run_identity_mismatch");
  assert.equal(calls, 1);
});

test("artifact rejects a same-title run from another workflow", async () => {
  await assert.rejects(() => runArtifact("test_token", "owner/project", 99, requestId,
    async () => response(run({ path: ".github/workflows/unrelated.yml" }))),
  (error) => error.code === "run_identity_mismatch");
});

test("artifact selects only the exact UUID artifact and never forwards bearer to signed storage", async () => {
  const calls = [];
  const archive = Buffer.from("PK\u0003\u0004test");
  const result = await runArtifact("test_token", "owner/project", 99, requestId,
    async (url, options) => {
      calls.push({ url: String(url), options });
      if (url.endsWith("/runs/99")) return response(run());
      if (url.includes("/runs/99/artifacts")) return response({ artifacts: [
        { id: 1, name: "mobile-phone-other-request", expired: false },
        { id: 2, name: `mobile-phone-${requestId}`, expired: false },
      ] });
      if (url.endsWith("/artifacts/2/zip")) return response(null, 302,
        { location: "https://results.blob.core.windows.net/run/result.zip" });
      assert.equal(url, "https://results.blob.core.windows.net/run/result.zip");
      assert.equal(options.headers.Authorization, undefined);
      return response(archive);
    });
  assert.deepEqual(result, archive);
  assert.equal(calls.length, 4);
});

for (const hasDispatchedRun of [true, false]) {
  test(`unfiltered history ignores matching push runs (prior dispatch: ${hasDispatchedRun})`, async () => {
    const history = [run({ id: 88, event: "push" })];
    if (hasDispatchedRun) history.push(run());
    const api = github({ history });
    const state = await dispatch("test_token", runRequest, {}, api.fetcher);
    assert.equal(state.id, 99);
    assert.equal(api.dispatches, hasDispatchedRun ? 0 : 1);
  });
}

test("history recovery ignores a same-title run from another workflow", async () => {
  const api = github({ history: [run({ path: ".github/workflows/unrelated.yml" })] });
  const state = await dispatch("test_token", runRequest, {}, api.fetcher);
  assert.equal(state.id, 99);
  assert.equal(api.dispatches, 1);
});
